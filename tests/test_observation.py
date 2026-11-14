"""What the recorder could not observe is never reported as "no problem".

A host without PSI, a recorder that cannot read the journal, a host without
systemd or sensors, a sampler that keeps failing, and the period an old
procdiag recorded: the rules that need what was missing are listed as not
evaluated with the reason, ``check`` says why in its quality notes, and
``compare`` says 判定不能 instead of 解消.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import psutil
import pytest
from click.testing import CliRunner
from synth import add_sys_baseline, make_db
from test_recorder import FakeClock, _AdvancingSleep
from test_rules_v4 import _make_v3_db

from procdiag import kernlog, systemd
from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, RulesConfig, SamplingConfig
from procdiag.core import CoverageGap
from procdiag.core.sampler import RunContext, SampleBatch, Sampler
from procdiag.diagnosis import Engine
from procdiag.recorder import Recorder
from procdiag.samplers.kernel_log import KernelLogSampler
from procdiag.samplers.service import ServiceSampler
from procdiag.samplers.system import SystemSampler
from procdiag.samplers.thermal import ThermalSampler
from procdiag.storage import Storage

_HOUR = 3600.0
_FIXTURES = Path(__file__).parent / "fixtures"
_PSI_RULES = [
    "CPU-SATURATION-01",
    "SWAP-THRASH-01",
    "IO-BOTTLENECK-01",
    "MEM-PRESSURE-01",
]
_INSTANT_RULES = [
    "CPU-SATURATION-01",
    "SWAP-THRASH-01",
    "KERNEL-ERROR-01",
    "SERVICE-RESTART-01",
    "THERMAL-01",
]
_V4_RULES = [
    "KERNEL-ERROR-01",
    "SERVICE-RESTART-01",
    "UNCLEAN-SHUTDOWN-01",
    "DISK-INODE-01",
    "FS-READONLY-01",
]


def _cfg() -> ProcdiagConfig:
    return ProcdiagConfig(rules=RulesConfig(min_observation_hours=0.0))


@pytest.fixture
def host(monkeypatch):
    """What the recorder can read on the simulated host (all of it by default)."""
    can = {"journal": True, "systemd": True, "sensors": True}
    monkeypatch.setattr(kernlog, "journal_readable", lambda: can["journal"])
    monkeypatch.setattr(
        kernlog, "newest_cursor", lambda: "c1" if can["journal"] else None
    )
    monkeypatch.setattr(kernlog, "read_events", lambda **kw: ([], "c1"))
    monkeypatch.setattr(systemd, "available", lambda: can["systemd"])
    monkeypatch.setattr(systemd, "list_service_units", lambda: [])
    monkeypatch.setattr(
        ThermalSampler,
        "_read_temp",
        staticmethod(lambda: 45.0 if can["sensors"] else None),
    )
    monkeypatch.setattr(
        ThermalSampler, "_read_freq", staticmethod(lambda: (None, None))
    )
    return can


def _record(db: Path, *, proc_root: Path = _FIXTURES / "proc_tree", samplers=None):
    """A short real recording (fake scheduler clock, real samplers)."""
    cfg = ProcdiagConfig(
        db_path=str(db),
        sampling=SamplingConfig(
            system_interval=0.1,
            cgroup_interval=0.3,
            process_interval=0.5,
            disk_interval=0.5,
            dir_scan_interval=5.0,
        ),
    )
    if samplers is None:
        samplers = {
            "system": SystemSampler(),
            "kernel_log": KernelLogSampler(),
            "service": ServiceSampler(),
            "thermal": ThermalSampler(),
        }
    clock = FakeClock(0.0)
    rec = Recorder(
        cfg,
        db_path=db,
        ctx=RunContext(cfg, proc_root=proc_root),
        samplers=samplers,
        duration=1.0,
        clock=clock,
        sleep=_AdvancingSleep(clock),
    )
    assert rec.run() == 0


def _check(db: Path, rules: list[str]) -> dict:
    result = CliRunner().invoke(
        cli, ["check", "--db", str(db), "--format", "json", "--only", ",".join(rules)]
    )
    return json.loads(result.output)


def _uncovered(data: dict) -> dict:
    return {r["rule_id"]: (r["reason"], r["missing"]) for r in data["uncovered_rules"]}


def _add_kernel_errors(store: Storage, *timestamps: float) -> None:
    store.conn.executemany(
        "INSERT INTO kernel_events (ts, kind, message) VALUES (?, 'io_error', ?)",
        [
            (ts, "blk_update_request: I/O error, dev sda, sector 123")
            for ts in timestamps
        ],
    )
    store.conn.commit()


class TestNoPsiValues:
    def test_psi_rules_are_not_evaluated_without_psi_values(self, tmp_path):
        store = make_db(tmp_path, hours=50)
        add_sys_baseline(store, hours=50)
        store.conn.execute(
            "UPDATE sys_samples SET psi_cpu_some10=NULL, psi_mem_some10=NULL,"
            " psi_mem_full10=NULL, psi_io_some10=NULL, psi_io_full10=NULL"
        )
        store.conn.execute(
            "UPDATE sys_rollup_1m SET psi_cpu_max=NULL, psi_mem_max=NULL,"
            " psi_io_max=NULL"
        )
        store.conn.commit()
        diag = Engine(_cfg()).run(store, only=_PSI_RULES)
        store.close()
        assert diag.evaluated_rules == []
        assert sorted(r.rule_id for r in diag.uncovered_rules) == sorted(_PSI_RULES)
        assert {r.reason for r in diag.uncovered_rules} == {CoverageGap.NOT_RECORDED}

    def test_psi_rules_run_when_psi_was_recorded(self, tmp_path):
        store = make_db(tmp_path, hours=50)
        add_sys_baseline(store, hours=50)
        diag = Engine(_cfg()).run(store, only=_PSI_RULES)
        store.close()
        assert sorted(diag.evaluated_rules) == sorted(_PSI_RULES)
        assert diag.uncovered_rules == []


class TestRecorderCapabilities:
    def test_rules_the_recorder_could_not_feed_are_not_evaluated(self, tmp_path, host):
        host.update(journal=False, systemd=False, sensors=False)
        db = tmp_path / "limited.db"
        _record(db, proc_root=_FIXTURES / "proc_tree_nopsi")

        data = _check(db, _INSTANT_RULES)

        assert data["evaluated_rules"] == []
        assert _uncovered(data) == {
            "CPU-SATURATION-01": ("not_observed", ["psi"]),
            "SWAP-THRASH-01": ("not_observed", ["psi"]),
            "KERNEL-ERROR-01": ("not_observed", ["kernel_log"]),
            "SERVICE-RESTART-01": ("not_observed", ["systemd"]),
            "THERMAL-01": ("not_observed", ["thermal"]),
        }
        notes = "\n".join(data["quality"]["notes"])
        for reason in ("PSI", "カーネルログ", "systemd", "温度センサ"):
            assert reason in notes
        text = CliRunner().invoke(
            cli, ["check", "--db", str(db), "--only", ",".join(_INSTANT_RULES)]
        )
        assert "すべてを評価" not in text.output
        assert "5 ルール中 0 件のみ評価" in text.output

    def test_a_host_that_can_read_everything_evaluates_every_rule(self, tmp_path, host):
        db = tmp_path / "full.db"
        _record(db)
        data = _check(db, _INSTANT_RULES)
        assert sorted(data["evaluated_rules"]) == sorted(_INSTANT_RULES)
        assert data["uncovered_rules"] == []

    def test_a_sampler_that_keeps_failing_is_reported(self, tmp_path, host):
        class BrokenDisk(Sampler):
            name = "disk"

            def collect(self, ctx: RunContext) -> list[SampleBatch]:
                raise OSError("statvfs が失敗しました")

        db = tmp_path / "broken.db"
        _record(db, samplers={"system": SystemSampler(), "disk": BrokenDisk()})
        notes = _check(db, ["CPU-SATURATION-01"])["quality"]["notes"]
        assert any("statvfs が失敗しました" in n for n in notes)

    def test_sessions_with_and_without_journal_access_note_the_gap(
        self, tmp_path, host
    ):
        db = tmp_path / "mixed.db"
        host["journal"] = False
        _record(db)
        host["journal"] = True
        _record(db)

        data = _check(db, ["KERNEL-ERROR-01"])

        assert data["evaluated_rules"] == ["KERNEL-ERROR-01"]
        assert any("カーネルログ" in n for n in data["quality"]["notes"])


class TestSpikeTriggerWithoutPsi:
    def test_unreadable_psi_files_fall_back_to_cpu_usage(
        self, tmp_path, host, monkeypatch
    ):
        proc = tmp_path / "proc"
        (proc / "pressure" / "cpu").mkdir(parents=True)  # present but unreadable
        (proc / "loadavg").write_text("0.50 0.40 0.30 1/100 1\n")
        monkeypatch.setattr(
            psutil,
            "cpu_percent",
            lambda *a, percpu=False, **k: [95.0, 95.0] if percpu else 95.0,
        )
        db = tmp_path / "busy.db"
        _record(db, proc_root=proc, samplers={"system": SystemSampler()})

        with sqlite3.connect(db) as conn:
            kinds = [
                r[0] for r in conn.execute("SELECT trigger_type FROM spike_events")
            ]
        assert "cpu" in kinds


def _compare(before: Path, after: Path, *rules: str):
    result = CliRunner().invoke(
        cli,
        ["compare", str(before), str(after), "--format", "json", "--only", *rules],
    )
    return result.exit_code, json.loads(result.output)


class TestCompareWithUnreadJournal:
    def _before(self, tmp_path: Path) -> Path:
        now = time.time()
        store = make_db(tmp_path / "before", hours=24)
        add_sys_baseline(store, hours=24, start_ts=now - 26 * _HOUR)
        _add_kernel_errors(store, now - 20 * _HOUR, now - 8 * _HOUR)
        path = store.path
        store.close()
        return path

    def test_after_period_without_journal_access_is_not_resolved(self, tmp_path, host):
        before = self._before(tmp_path)
        after = tmp_path / "after.db"
        host["journal"] = False
        _record(after)
        with Storage(after) as store:
            add_sys_baseline(store, hours=24, start_ts=time.time() + 60)

        code, data = _compare(before, after, "KERNEL-ERROR-01")

        (row,) = data["rows"]
        assert row["verdict"] == "not_evaluable"
        assert row["after"]["why"] == "not_observed"
        assert code == 3

    def test_after_period_read_only_in_part_is_not_resolved(self, tmp_path, host):
        before = self._before(tmp_path)
        after = tmp_path / "after.db"
        _make_old_db(after, start_ts=time.time() - 20 * _HOUR, hours=20)
        _record(after)
        with Storage(after) as store:
            add_sys_baseline(store, hours=4, start_ts=time.time() + 60)

        code, data = _compare(before, after, "KERNEL-ERROR-01")

        (row,) = data["rows"]
        assert row["verdict"] == "not_evaluable"
        assert row["after"]["why"] == "not_observed"
        assert code == 3


def _make_old_db(path: Path, *, start_ts: float, hours: float) -> None:
    """A DB recorded by procdiag 0.3 (schema 3) for *hours* from *start_ts*."""
    _make_v3_db(path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM sys_samples")
    conn.execute("DELETE FROM disk_samples")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('procdiag_version', '0.3.0')")
    steps = range(int(hours * 6) + 1)
    conn.executemany(
        "INSERT INTO sys_samples (ts, cpu_total, mem_used, mem_available,"
        " swap_in, swap_out, psi_cpu_some10, psi_mem_some10, psi_mem_full10,"
        " psi_io_some10, psi_io_full10) VALUES (?, 5, 1, 1, 0, 0, 0, 0, 0, 0, 0)",
        [(start_ts + i * 600,) for i in steps],
    )
    conn.executemany(
        "INSERT INTO disk_samples VALUES (?, '/', 100, 10)",
        [(start_ts + i * 600,) for i in steps],
    )
    conn.commit()
    conn.close()


class TestPeriodRecordedByOldVersion:
    def test_migrated_db_does_not_read_old_period_as_no_problem(self, tmp_path):
        db = tmp_path / "old.db"
        _make_old_db(db, start_ts=time.time() - 3 * _HOUR, hours=3)
        with Storage(db):
            pass  # a write open (record, prune) migrates the DB

        with Storage(db, read_only=True) as store:
            diag = Engine(_cfg()).run(store, only=_V4_RULES)

        assert diag.evaluated_rules == []
        reasons = {r.rule_id: r.reason for r in diag.uncovered_rules}
        assert reasons == {
            "KERNEL-ERROR-01": CoverageGap.NOT_OBSERVED,
            "SERVICE-RESTART-01": CoverageGap.NOT_OBSERVED,
            "UNCLEAN-SHUTDOWN-01": CoverageGap.NOT_OBSERVED,
            "DISK-INODE-01": CoverageGap.NOT_RECORDED,
            "FS-READONLY-01": CoverageGap.NOT_RECORDED,
        }

    def test_recording_after_the_upgrade_notes_the_old_period(self, tmp_path, host):
        db = tmp_path / "old.db"
        _make_old_db(db, start_ts=time.time() - 3 * _HOUR, hours=3)
        _record(db)

        data = _check(db, ["KERNEL-ERROR-01"])

        assert data["evaluated_rules"] == ["KERNEL-ERROR-01"]
        assert any("0.3 以前" in n for n in data["quality"]["notes"])
