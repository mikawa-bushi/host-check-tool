"""Samplers that fail, and what ``check`` then says about the rules they feed.

A sampler that cannot read anything in a whole recording leaves its rules
"not observed" instead of healthy; a journal cursor the journal rejects does
not stop the kernel-log recording; a service the sampler first sees after its
first poll keeps the restarts it already had; a custom sampler's plain text
note and a new cgroup during a write outage do not cost a tick.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from test_observation import _check, _record, _uncovered, host  # noqa: F401
from test_recorder import FakeClock, FakeSampler, _AdvancingSleep, _make_config
from test_rules_v4 import _record_session, _unit
from test_sampler_cgroup_process import CGROUP_TREE, _make_ctx

from procdiag import kernlog, systemd
from procdiag.config import ProcdiagConfig
from procdiag.core import Capability
from procdiag.core.sampler import RunContext, SampleBatch, Sampler
from procdiag.recorder import Recorder
from procdiag.samplers.cgroup import CgroupSampler
from procdiag.samplers.kernel_log import KernelLogSampler
from procdiag.samplers.system import SystemSampler
from procdiag.storage import Storage

_HOUR = 3600.0


class _KernelLogThatCannotParse(KernelLogSampler):
    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        raise ValueError("journalctl の出力を解釈できません")


class _KernelLogWhoseSetupFails(KernelLogSampler):
    def setup(self, ctx: RunContext) -> None:
        raise RuntimeError("journalctl を起動できません")


@pytest.mark.usefixtures("host")
class TestASamplerThatNeverReads:
    def test_a_kernel_log_that_fails_every_run_is_not_read_as_no_error(self, tmp_path):
        db = tmp_path / "broken.db"
        _record(
            db,
            samplers={
                "system": SystemSampler(),
                "kernel_log": _KernelLogThatCannotParse(),
            },
        )

        data = _check(db, ["KERNEL-ERROR-01"])

        assert data["evaluated_rules"] == []
        assert _uncovered(data) == {"KERNEL-ERROR-01": ("not_observed", ["kernel_log"])}
        notes = "\n".join(data["quality"]["notes"])
        assert "journalctl の出力を解釈できません" in notes

    def test_a_journal_no_read_could_open_is_not_read_as_no_error(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            kernlog, "read_events", lambda **kw: (None, kw.get("after_cursor"))
        )
        db = tmp_path / "unreadable.db"
        _record(db)

        data = _check(db, ["KERNEL-ERROR-01"])

        assert data["evaluated_rules"] == []
        assert _uncovered(data) == {"KERNEL-ERROR-01": ("not_observed", ["kernel_log"])}

    def test_a_systemctl_that_fails_every_poll_is_not_read_as_no_restarts(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(systemd, "list_service_units", lambda: None)
        db = tmp_path / "nosystemctl.db"
        _record(db)

        data = _check(db, ["SERVICE-RESTART-01"])

        assert data["evaluated_rules"] == []
        assert _uncovered(data) == {"SERVICE-RESTART-01": ("not_observed", ["systemd"])}

    def test_a_sampler_whose_setup_raises_is_not_read_as_observed(self, tmp_path):
        db = tmp_path / "nosetup.db"
        _record(
            db,
            samplers={
                "system": SystemSampler(),
                "kernel_log": _KernelLogWhoseSetupFails(),
            },
        )

        data = _check(db, ["KERNEL-ERROR-01"])

        assert data["evaluated_rules"] == []
        assert _uncovered(data) == {"KERNEL-ERROR-01": ("not_observed", ["kernel_log"])}

    def test_a_read_that_fails_after_earlier_reads_is_noted(
        self, tmp_path, monkeypatch
    ):
        answers = iter([([], "c1")])

        def read_events(**kw):
            return next(answers, (None, kw.get("after_cursor")))

        monkeypatch.setattr(kernlog, "read_events", read_events)
        db = tmp_path / "late.db"
        _record(db)

        notes = _check(db, ["KERNEL-ERROR-01"])["quality"]["notes"]

        assert any("kernel_log" in n and "失敗" in n for n in notes), notes


_IO_ERROR_LINE = (
    "1731000100.5 host kernel: blk_update_request: I/O error, dev sda, sector 9\n"
)


def _journalctl(monkeypatch, *, accepts: set[str]) -> list[str]:
    """A journal that refuses every cursor but *accepts* (exit 1), as journalctl
    does for one it cannot seek to; returns the ``--since`` values it was given."""
    since: list[str] = []

    def run(args):
        if "--after-cursor" in args:
            if args[args.index("--after-cursor") + 1] not in accepts:
                return None
            return _IO_ERROR_LINE + "-- cursor: c20\n"
        if "--since" in args:
            since.append(args[args.index("--since") + 1])
            return _IO_ERROR_LINE + "-- cursor: c30\n"
        if "--show-cursor" in args:
            return "1731000000.1 host kernel: boot\n-- cursor: c10\n"
        return "1731000000.1 host kernel: boot\n"

    monkeypatch.setattr(kernlog, "run", run)
    return since


def _rows(batches: list[SampleBatch], table: str) -> list[tuple]:
    return [row for batch in batches if batch.table == table for row in batch.rows]


class TestKernelLogCursor:
    def test_a_stored_cursor_the_journal_rejects_does_not_stop_the_recording(
        self, monkeypatch
    ):
        _journalctl(monkeypatch, accepts={"c10"})
        sampler = KernelLogSampler(cursor="garbage")
        ctx = RunContext(ProcdiagConfig())
        sampler.setup(ctx)

        sampler.run(ctx)
        batches = sampler.run(ctx)

        assert any("読み取り位置" in n.text for n in ctx.notes)
        assert _rows(batches, "meta") == [("kernel_log_cursor", "c20")]
        assert [e[1] for e in _rows(batches, "kernel_events")] == ["io_error"]
        assert Capability.KERNEL_LOG not in ctx.unobserved

    def test_a_cursor_refused_later_is_replaced_by_the_time_of_the_last_read(
        self, monkeypatch
    ):
        since = _journalctl(monkeypatch, accepts={"c10", "c20"})
        sampler = KernelLogSampler(cursor="c10")
        ctx = RunContext(ProcdiagConfig())
        sampler.setup(ctx)
        sampler.run(ctx)
        read_at = time.time()
        monkeypatch.setattr(
            kernlog,
            "run",
            lambda args: (
                None if "--after-cursor" in args else _journalctl_since(args, since)
            ),
        )

        batches = sampler.run(ctx)

        assert len(since) == 1 and int(since[0][1:]) <= read_at
        assert _rows(batches, "meta") == [("kernel_log_cursor", "c30")]
        assert [e[1] for e in _rows(batches, "kernel_events")] == ["io_error"]
        assert Capability.KERNEL_LOG not in ctx.unobserved


def _journalctl_since(args, since: list[str]) -> str:
    since.append(args[args.index("--since") + 1])
    return _IO_ERROR_LINE + "-- cursor: c30\n"


_SERVICES = {"other.service": _unit(0)}


def _service_events(tmp_path: Path, monkeypatch, polls):
    store = Storage(tmp_path / "svc.db")
    start = time.time() - 3 * _HOUR
    _record_session(store, monkeypatch, polls, start=start)
    store.flush()
    events = [
        (r["unit"], r["kind"], r["n_restarts"])
        for r in store.rows("service_events")
        if r["unit"] == "late.service"
    ]
    store.close()
    return events


class TestServiceSampler:
    def test_a_unit_first_loaded_after_the_first_poll_keeps_its_restarts(
        self, tmp_path, monkeypatch
    ):
        late = {**_SERVICES, "late.service": _unit(3)}

        events = _service_events(tmp_path, monkeypatch, [_SERVICES, late, late])

        assert events == [("late.service", "restart", 3)]

    def test_a_failed_unit_that_was_collected_and_fails_again_is_seen_again(
        self, tmp_path, monkeypatch
    ):
        failed = {
            **_SERVICES,
            "late.service": _unit(0, state="failed", Result="exit-code"),
        }

        events = _service_events(
            tmp_path, monkeypatch, [failed, _SERVICES, failed, failed]
        )

        assert events == [
            ("late.service", "failed_at_start", 0),
            ("late.service", "failed", 0),
        ]

    def test_a_unit_that_comes_back_with_a_reset_counter_counts_from_zero(
        self, tmp_path, monkeypatch
    ):
        events = _service_events(
            tmp_path,
            monkeypatch,
            [
                {**_SERVICES, "late.service": _unit(5)},
                _SERVICES,
                {**_SERVICES, "late.service": _unit(7)},
            ],
        )

        assert events == [("late.service", "restart", 7)]

    def test_the_snapshot_does_not_say_every_restart_was_a_crash(self, monkeypatch):
        import procdiag.snapshot as snap

        monkeypatch.setattr(snap.systemd, "available", lambda: True)
        monkeypatch.setattr(snap.systemd, "failed_units", lambda: [])
        monkeypatch.setattr(snap.systemd, "list_service_units", lambda: ["a"])
        monkeypatch.setattr(
            snap.systemd,
            "show_units",
            lambda units: {"a": {"NRestarts": "9", "Restart": "always"}},
        )

        items = {
            i.name: i for i in snap._check_services(ProcdiagConfig())  # noqa: SLF001
        }

        hint = items["restarted_units"].hint
        assert hint and "異常終了して" not in hint


class TestCustomSampler:
    def test_a_plain_text_note_is_kept_once_per_sampler(self, tmp_path):
        class _TextNoteDisk(Sampler):
            name = "disk"

            def collect(self, ctx: RunContext) -> list[SampleBatch]:
                ctx.notes.append("disk: 設備のログを読めません")
                return []

        db = tmp_path / "text.db"
        clock = FakeClock(0.0)
        rec = Recorder(
            _make_config(system_interval=0.1, disk_interval=0.3),
            db_path=db,
            samplers={"system": FakeSampler(), "disk": _TextNoteDisk()},
            duration=2.0,
            clock=clock,
            sleep=_AdvancingSleep(clock),
        )

        assert rec.run() == 0

        with Storage(db, read_only=True) as store:
            kept = [
                r["detail"]
                for r in store.rows("ops_events", where="kind='sampler_failed'")
            ]
        assert kept == ["disk: 設備のログを読めません"]


class TestCgroupSampler:
    def test_a_unit_that_cannot_be_registered_costs_only_itself(self):
        def resolve(unit_path: str, ts: float) -> int:
            if unit_path.endswith("mysql.service"):
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return len(unit_path)

        sampler = CgroupSampler(resolve_cg=resolve)
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)

        batches = sampler.run(ctx)

        assert sorted(r[1] for b in batches for r in b.rows) == sorted(
            [len("system.slice/cron.service"), len("system.slice/nginx.service")]
        )
        assert ctx.notes == []
