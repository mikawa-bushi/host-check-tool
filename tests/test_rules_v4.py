"""Tests for the schema-v4 detections.

DISK-INODE-01, FS-READONLY-01, UNCLEAN-SHUTDOWN-01, SERVICE-RESTART-01,
KERNEL-ERROR-01, exact ZOMBIE-01 counts, measured FD limits for FD-LEAK-01,
the service / kernel-log samplers, session recording and the v3→v4 migration.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from synth import add_proc_series, add_sys_baseline, make_db
from test_recorder import FakeClock, _AdvancingSleep, _make_config, _make_samplers

from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Confidence, Severity, Window
from procdiag.diagnosis import Engine
from procdiag.recorder import Recorder
from procdiag.storage import SCHEMA_VERSION, Storage

_HOUR = 3600.0
_DAY = 86400.0


def _window(hours: float = 2.0) -> Window:
    now = time.time()
    return Window(start_ts=now - hours * _HOUR, end_ts=now)


def _disk(store, ts, mp, *, inodes_total, inodes_used, read_only=0, total=100, used=10):
    store.buffer(
        "disk_samples", [(ts, mp, total, used, inodes_total, inodes_used, read_only)]
    )


# ---------------------------------------------------------------------------
# Schema v4 migration
# ---------------------------------------------------------------------------


def _make_v3_db(path, *, wal: bool = True) -> None:
    """A schema-v3 DB: today's DB with every v4 addition removed again."""
    with Storage(path):
        pass
    conn = sqlite3.connect(path)
    for table in ("sessions", "service_events", "kernel_events", "proc_counts"):
        conn.execute(f"DROP TABLE {table}")
    for table, column in (
        ("disk_samples", "inodes_total"),
        ("disk_samples", "inodes_used"),
        ("disk_samples", "read_only"),
        ("proc_registry", "fd_soft_limit"),
        ("proc_registry", "fd_hard_limit"),
    ):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    conn.execute(
        "INSERT INTO disk_samples VALUES (?, '/', 100, 10)", (time.time() - 60,)
    )
    conn.execute(
        "INSERT INTO sys_samples (ts, cpu_total) VALUES (?, 5.0)", (time.time() - 60,)
    )
    conn.commit()
    if not wal:  # e.g. a copy made by another tool
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()


class TestMigrationV4:
    def test_write_open_adds_columns_and_bumps_version(self, tmp_path):
        db = tmp_path / "v3.db"
        _make_v3_db(db)
        with Storage(db) as store:
            assert store.meta()["schema_version"] == str(SCHEMA_VERSION)
            assert store.has_column("disk_samples", "inodes_total")
            assert store.has_column("proc_registry", "fd_soft_limit")
            assert store.has_table("sessions") and store.has_table("kernel_events")
            assert store.rows("disk_samples")[0]["used"] == 10  # data kept

    def test_column_step_is_idempotent(self, tmp_path):
        from procdiag.storage import _add_column

        db = tmp_path / "x.db"
        _make_v3_db(db)
        conn = sqlite3.connect(db)
        step = _add_column("disk_samples", "read_only", "INTEGER")
        step(conn)
        step(conn)  # a re-run after a crash must not fail
        cols = [r[1] for r in conn.execute("PRAGMA table_info(disk_samples)")]
        conn.close()
        assert cols.count("read_only") == 1

    def test_read_only_old_db_runs_new_rules_quietly(self, tmp_path):
        db = tmp_path / "v3.db"
        _make_v3_db(db, wal=False)  # read-only open must not try to switch to WAL
        with Storage(db, read_only=True) as store:
            diag = Engine(
                ProcdiagConfig(rules=RulesConfig(min_observation_hours=0))
            ).run(
                store,
                only=[
                    "DISK-INODE-01",
                    "FS-READONLY-01",
                    "UNCLEAN-SHUTDOWN-01",
                    "SERVICE-RESTART-01",
                    "KERNEL-ERROR-01",
                ],
            )
        assert diag.errored_rules == []
        assert diag.findings == []


# ---------------------------------------------------------------------------
# DISK-INODE-01 / FS-READONLY-01
# ---------------------------------------------------------------------------


class TestDiskInode:
    def _eval(self, store):
        from procdiag.diagnosis.io_disk import DiskInodeRule

        store.flush()
        return DiskInodeRule().evaluate(store, _window())

    def test_high_inode_usage_is_critical_even_with_free_space(self, tmp_path):
        store = make_db(tmp_path)
        now = time.time()
        _disk(store, now - 60, "/var", inodes_total=1000, inodes_used=960, used=20)
        (f,) = self._eval(store)
        assert f.severity == Severity.CRITICAL and f.confidence == Confidence.HIGH
        assert f.evidence["used_pct"] == 96.0 and f.evidence["bytes_used_pct"] == 20.0
        store.close()

    def test_fast_growth_warns_before_threshold(self, tmp_path):
        store = make_db(tmp_path)
        now = time.time()
        for i in range(6):  # +10% of the table per 10 minutes → full in ~hours
            _disk(
                store,
                now - (6 - i) * 600,
                "/",
                inodes_total=1000,
                inodes_used=100 + i * 100,
            )
        (f,) = self._eval(store)
        assert f.evidence["days_left"] is not None and f.evidence["days_left"] < 1
        assert f.confidence == Confidence.MEDIUM  # forecast, below the level threshold
        store.close()

    def test_normal_usage_and_unknown_inodes_are_silent(self, tmp_path):
        store = make_db(tmp_path)
        now = time.time()
        _disk(store, now - 60, "/", inodes_total=1000, inodes_used=300)
        _disk(store, now - 60, "/btrfs", inodes_total=None, inodes_used=None)
        assert self._eval(store) == []
        store.close()


class TestFsReadOnly:
    def _eval(self, store):
        from procdiag.diagnosis.io_disk import FsReadOnlyRule

        store.flush()
        return FsReadOnlyRule().evaluate(store, _window())

    def test_switch_to_read_only_is_critical(self, tmp_path):
        store = make_db(tmp_path)
        now = time.time()
        for i, ro in enumerate([0, 0, 1, 1]):
            _disk(
                store,
                now - (4 - i) * 60,
                "/data",
                inodes_total=10,
                inodes_used=1,
                read_only=ro,
            )
        (f,) = self._eval(store)
        assert f.severity == Severity.CRITICAL
        assert f.evidence["still_read_only"] is True
        assert f.evidence["switched_at"] == pytest.approx(now - 120)
        store.close()

    def test_root_read_only_throughout_warns_other_mounts_ignored(self, tmp_path):
        store = make_db(tmp_path)
        now = time.time()
        for i in range(3):
            _disk(
                store,
                now - (3 - i) * 60,
                "/",
                inodes_total=10,
                inodes_used=1,
                read_only=1,
            )
            _disk(
                store,
                now - (3 - i) * 60,
                "/media/cdrom",
                inodes_total=10,
                inodes_used=1,
                read_only=1,
            )
        (f,) = self._eval(store)
        assert f.subject == "/" and f.severity == Severity.WARNING
        store.close()


# ---------------------------------------------------------------------------
# UNCLEAN-SHUTDOWN-01 and session recording
# ---------------------------------------------------------------------------


def _session(store, *, started, boot, alive, stopped=None):
    store.conn.execute(
        "INSERT INTO sessions (started_ts, boot_time, hostname, procdiag_version, "
        "last_alive_ts, stopped_ts) VALUES (?, ?, 'h', 'x', ?, ?)",
        (started, boot, alive, stopped),
    )
    store.conn.commit()


class TestUncleanShutdown:
    def _eval(self, store):
        from procdiag.diagnosis.crash import UncleanShutdownRule

        return UncleanShutdownRule().evaluate(store, _window(hours=10))

    def test_reboot_without_clean_stop_is_reported(self, tmp_path):
        store = make_db(tmp_path)
        now = time.time()
        _session(
            store, started=now - 5 * _HOUR, boot=now - 6 * _HOUR, alive=now - 3 * _HOUR
        )
        _session(store, started=now - 2 * _HOUR, boot=now - 2.1 * _HOUR, alive=now)
        (f,) = self._eval(store)
        assert f.evidence["kind"] == "host_down" and f.severity == Severity.WARNING
        assert f.evidence["events"][0]["last_alive"] == pytest.approx(now - 3 * _HOUR)
        store.close()

    def test_clean_stop_and_recorder_kill_are_distinguished(self, tmp_path):
        store = make_db(tmp_path)
        now = time.time()
        boot = now - 9 * _HOUR
        _session(
            store,
            started=now - 8 * _HOUR,
            boot=boot,
            alive=now - 7 * _HOUR,
            stopped=now - 7 * _HOUR,
        )
        _session(store, started=now - 6 * _HOUR, boot=boot, alive=now - 5 * _HOUR)
        _session(store, started=now - 4 * _HOUR, boot=boot + 3, alive=now)  # same boot
        (f,) = self._eval(store)
        assert f.evidence["kind"] == "recorder_killed" and f.severity == Severity.INFO
        store.close()

    def test_recorder_opens_and_closes_its_session(self, tmp_path):
        clock = FakeClock(0.0)
        rec = Recorder(
            _make_config(),
            db_path=tmp_path / "rec.db",
            samplers=_make_samplers(),
            duration=1.0,
            clock=clock,
            sleep=_AdvancingSleep(clock),
        )
        assert rec.run() == 0
        with Storage(tmp_path / "rec.db", read_only=True) as store:
            (session,) = store.sessions()
        assert session["stopped_ts"] is not None
        assert session["last_alive_ts"] >= session["started_ts"]


# ---------------------------------------------------------------------------
# SERVICE-RESTART-01 + ServiceSampler
# ---------------------------------------------------------------------------


def _svc(store, ts, unit, kind, n=0, result=None):
    store.buffer("service_events", [(ts, unit, kind, n, result)])


class TestServiceRestart:
    def _eval(self, store):
        from procdiag.diagnosis.crash import ServiceRestartRule

        store.flush()
        return {f.subject: f for f in ServiceRestartRule().evaluate(store, _window(10))}

    def test_crash_loop_warning_failed_and_quiet_units(self, tmp_path):
        store = make_db(tmp_path)
        now = time.time()
        for i in range(2):  # 4 restarts within 10 minutes → crash loop
            _svc(
                store, now - 600 + i * 300, "plc-gw.service", "restart", 2, "exit-code"
            )
        for i in range(3):  # 3 restarts spread over 9 h → warning
            _svc(
                store,
                now - (9 - 3 * i) * _HOUR,
                "logger.service",
                "restart",
                1,
                "signal",
            )
        _svc(store, now - 60, "db.service", "failed", 0, "oom-kill")
        _svc(store, now - 60, "once.service", "restart", 1, "exit-code")  # isolated
        found = self._eval(store)
        assert set(found) == {"plc-gw.service", "logger.service", "db.service"}
        assert found["plc-gw.service"].severity == Severity.CRITICAL
        assert found["plc-gw.service"].evidence["max_restarts_per_hour"] == 4
        assert found["logger.service"].severity == Severity.WARNING
        assert found["db.service"].evidence["results"] == ["oom-kill"]
        store.close()


class TestServiceSampler:
    def test_events_only_on_restart_growth_and_failed_transition(self, monkeypatch):
        from test_sampler_cgroup_process import _make_ctx

        from procdiag.samplers import service as svc_mod

        states = iter(
            [
                {
                    "a.service": {
                        "Id": "a.service",
                        "NRestarts": "5",
                        "ActiveState": "active",
                    }
                },
                {
                    "a.service": {
                        "Id": "a.service",
                        "NRestarts": "7",
                        "ActiveState": "failed",
                        "Result": "exit-code",
                    }
                },
                {
                    "a.service": {
                        "Id": "a.service",
                        "NRestarts": "0",
                        "ActiveState": "failed",
                    }
                },
            ]
        )
        monkeypatch.setattr(svc_mod.systemd, "available", lambda: True)
        monkeypatch.setattr(
            svc_mod.systemd, "list_service_units", lambda: ["a.service"]
        )
        monkeypatch.setattr(svc_mod.systemd, "show_units", lambda units: next(states))

        sampler = svc_mod.ServiceSampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        assert sampler.collect(ctx) == []  # first poll = baseline
        (batch,) = sampler.collect(ctx)
        kinds = [(r[2], r[3], r[4]) for r in batch.rows]
        assert kinds == [("restart", 2, "exit-code"), ("failed", 0, "exit-code")]
        assert sampler.collect(ctx) == []  # manual reset + still failed: no new event

    def test_unit_failures_reads_the_failed_with_result_lines_after_since(
        self, monkeypatch
    ):
        from procdiag import systemd

        out = (
            '{"UNIT":"a.service","UNIT_RESULT":"exit-code",'
            '"__REALTIME_TIMESTAMP":"1700000000500000"}\n'
            '{"UNIT":"a.service","UNIT_RESULT":"signal",'
            '"__REALTIME_TIMESTAMP":"1700000100000000"}\n'
            '{"UNIT":"b.service","UNIT_RESULT":"core-dump",'
            '"__REALTIME_TIMESTAMP":"1700000200000000"}\n'
        )
        monkeypatch.setattr(systemd, "run", lambda args: out)

        assert systemd.unit_failures(1700000000.5) == {
            "a.service": ["signal"],
            "b.service": ["core-dump"],
        }


_POLL = 300.0


def _unit(
    n_restarts, *, restart="on-failure", force="", state="active", result="success"
):
    return {
        "NRestarts": str(n_restarts),
        "Restart": restart,
        "RestartForceExitStatus": force,
        "ActiveState": state,
        "Result": result,
    }


def _record_session(store, monkeypatch, polls, *, start, journal=None):
    """*journal* maps a unit to the (ts, UNIT_RESULT) of its "Failed with
    result" lines; None means the journal is unreadable."""
    from test_sampler_cgroup_process import _make_ctx

    from procdiag import kernlog
    from procdiag.samplers import service as svc_mod

    answers = iter(polls)
    monkeypatch.setattr(kernlog, "journal_readable", lambda: journal is not None)
    monkeypatch.setattr(
        svc_mod.systemd,
        "unit_failures",
        lambda since: {
            unit: [r for ts, r in lines if since < ts <= svc_mod.time.time()]
            for unit, lines in (journal or {}).items()
        },
        raising=False,
    )
    monkeypatch.setattr(svc_mod.systemd, "available", lambda: True)
    monkeypatch.setattr(svc_mod.systemd, "list_service_units", lambda: sorted(polls[0]))
    monkeypatch.setattr(
        svc_mod.systemd,
        "show_units",
        lambda units, *a, **k: {u: {"Id": u, **p} for u, p in next(answers).items()},
    )
    sampler = svc_mod.ServiceSampler()
    ctx = _make_ctx()
    sampler.setup(ctx)
    for k in range(len(polls)):
        with monkeypatch.context() as m:
            m.setattr(svc_mod.time, "time", lambda ts=start + k * _POLL: ts)
            batches = sampler.collect(ctx)
        for batch in batches:
            store.buffer(batch.table, batch.rows)


def _diagnose(store):
    store.flush()
    diag = Engine(ProcdiagConfig()).run(store, only=["SERVICE-RESTART-01"])
    store.close()
    return {f.subject: f for f in diag.findings}


class TestServiceRestartFromPolls:
    @pytest.mark.parametrize(
        ("counts", "restarts"),
        [([10, 3, 3, 3], 3), ([10, 4, 8, 12], 12)],
        ids=["3-after-reset", "12-after-reset"],
    )
    def test_restarts_after_a_manual_restart_reset_the_counter_are_counted(
        self, tmp_path, monkeypatch, counts, restarts
    ):
        start = time.time() - 3 * _HOUR
        store = make_db(tmp_path, hours=3)
        add_sys_baseline(store, hours=3, start_ts=start)
        _record_session(
            store,
            monkeypatch,
            [{"plc-gw.service": _unit(n)} for n in counts],
            start=start,
        )

        f = _diagnose(store)["plc-gw.service"]

        assert (f.severity, f.evidence["restarts"]) == (Severity.CRITICAL, restarts)

    @pytest.mark.parametrize(
        ("policy", "force", "journal", "severity", "confidence"),
        [
            ("always", "", "no-failures", Severity.WARNING, Confidence.LOW),
            ("on-failure", "75", "no-failures", Severity.WARNING, Confidence.LOW),
            ("on-success", "", None, Severity.WARNING, Confidence.LOW),
            ("always", "", "a-failure-per-restart", Severity.CRITICAL, Confidence.HIGH),
            ("always", "", None, Severity.CRITICAL, Confidence.MEDIUM),
            ("on-failure", "", None, Severity.CRITICAL, Confidence.HIGH),
        ],
        ids=[
            "always-clean-exits",
            "forced-exit-status-clean",
            "on-success",
            "always-crashes",
            "always-journal-unreadable",
            "on-failure",
        ],
    )
    def test_restarts_after_a_clean_exit_are_no_crash_loop(
        self, tmp_path, monkeypatch, policy, force, journal, severity, confidence
    ):
        start = time.time() - 3 * _HOUR
        store = make_db(tmp_path, hours=3)
        add_sys_baseline(store, hours=3, start_ts=start)
        restarted_at = [start + k * _POLL - 60 for k in range(4, 33, 4)]
        lines = {
            None: None,
            "no-failures": {},
            "a-failure-per-restart": {
                "collector.service": [(ts, "exit-code") for ts in restarted_at]
            },
        }[journal]
        _record_session(
            store,
            monkeypatch,
            [
                {"collector.service": _unit(k // 4, restart=policy, force=force)}
                for k in range(33)
            ],
            start=start,
            journal=lines,
        )

        f = _diagnose(store)["collector.service"]

        assert (f.severity, f.confidence) == (severity, confidence)
        assert f.evidence["crash_loop"] is (severity == Severity.CRITICAL)
        if confidence == Confidence.LOW:
            assert "正常に終了" in f.proposal

    def test_a_unit_failed_before_recording_is_not_a_failed_transition_per_start(
        self, tmp_path, monkeypatch
    ):
        start = time.time() - 3 * _HOUR
        store = make_db(tmp_path, hours=3)
        add_sys_baseline(store, hours=3, start_ts=start)
        failed = {"legacy-sync.service": _unit(0, state="failed", result="exit-code")}
        for session in range(3):
            _record_session(
                store, monkeypatch, [failed, failed], start=start + session * _HOUR
            )

        f = _diagnose(store)["legacy-sync.service"]

        assert f.severity == Severity.WARNING
        assert f.evidence["failed_count"] == 0
        assert f.evidence["failed_at_start"] is True

    def test_failed_transitions_seen_while_recording_are_each_counted(
        self, tmp_path, monkeypatch
    ):
        start = time.time() - 3 * _HOUR
        store = make_db(tmp_path, hours=3)
        add_sys_baseline(store, hours=3, start_ts=start)
        active = {"db.service": _unit(0)}
        failed = {"db.service": _unit(0, state="failed", result="exit-code")}
        for session in range(3):
            _record_session(
                store, monkeypatch, [active, failed], start=start + session * _HOUR
            )

        f = _diagnose(store)["db.service"]

        assert f.evidence["failed_count"] == 3


# ---------------------------------------------------------------------------
# KERNEL-ERROR-01 + kernlog + KernelLogSampler
# ---------------------------------------------------------------------------


class TestKernlog:
    def test_classify(self):
        from procdiag.kernlog import classify

        assert classify("EXT4-fs error (device sda1): ext4_find_entry") == "fs_error"
        assert (
            classify("blk_update_request: I/O error, dev sdb, sector 2048")
            == "io_error"
        )
        assert (
            classify("INFO: task kworker:12 blocked for more than 120 seconds.")
            == "hung_task"
        )
        assert classify("usb 1-1: USB disconnect, device number 4") == "usb_disconnect"
        assert classify("myapp[1234]: segfault at 0 ip 00 sp 00 error 4") == "segfault"
        assert classify("Out of memory: Killed process 42 (java)") == "oom"
        assert classify("e1000e: eth0 NIC Link is Up 1000 Mbps") is None

    def test_parse_short_unix_keeps_anomalies_and_cursor(self):
        from procdiag.kernlog import parse_short_unix

        text = (
            "1700000000.500000 edge kernel: usb 1-1: USB disconnect, device number 4\n"
            "1700000001.000000 edge kernel: e1000e: eth0 NIC Link is Up\n"
            "-- cursor: s=abc;i=1\n"
        )
        events, cursor = parse_short_unix(text)
        assert [(e.ts, e.kind) for e in events] == [(1700000000.5, "usb_disconnect")]
        assert events[0].message.startswith("usb 1-1")
        assert cursor == "s=abc;i=1"


class TestKernelError:
    def test_one_finding_per_kind_and_oom_left_to_oom_rule(self, tmp_path):
        from procdiag.diagnosis.crash import KernelErrorRule

        store = make_db(tmp_path)
        now = time.time()
        rows = [
            (now - 300 + i, "io_error", f"I/O error dev sdb sector {i}")
            for i in range(4)
        ]
        rows += [
            (now - 100, "usb_disconnect", "usb 1-1: USB disconnect"),
            (now - 50, "oom", "Out of memory"),
        ]
        store.buffer("kernel_events", rows)
        store.flush()
        found = {
            f.evidence["kind"]: f for f in KernelErrorRule().evaluate(store, _window())
        }
        assert set(found) == {"io_error", "usb_disconnect"}
        assert found["io_error"].severity == Severity.CRITICAL
        assert found["io_error"].evidence["count"] == 4
        assert len(found["io_error"].evidence["samples"]) == 3
        assert found["usb_disconnect"].severity == Severity.WARNING
        store.close()


class TestKernelLogSampler:
    def test_resumes_from_cursor(self, monkeypatch):
        from test_sampler_cgroup_process import _make_ctx

        from procdiag.kernlog import KernelEvent
        from procdiag.samplers import kernel_log as kl_mod

        calls = []

        def fake_read(*, since=None, after_cursor=None):
            calls.append((since is not None, after_cursor))
            return [KernelEvent(1.0, "io_error", "I/O error")], "c1"

        monkeypatch.setattr(kl_mod.kernlog, "journal_readable", lambda: True)
        monkeypatch.setattr(kl_mod.kernlog, "read_events", fake_read)
        sampler = kl_mod.KernelLogSampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        (batch,) = sampler.collect(ctx)
        sampler.collect(ctx)
        assert batch.rows == [(1.0, "io_error", "I/O error")]
        assert calls == [(True, None), (False, "c1")]


# ---------------------------------------------------------------------------
# ZOMBIE-01 exact counts / FD-LEAK-01 measured limits
# ---------------------------------------------------------------------------


class TestExactZombiesAndFdLimits:
    def test_zombie_counts_from_proc_counts(self, tmp_path):
        from procdiag.diagnosis.leak import ZombieRule

        store = make_db(tmp_path)
        now = time.time()
        store.buffer(
            "proc_counts",
            [(now - 8 * _HOUR + i * 600, 200, i // 4) for i in range(48)],
        )
        store.flush()
        (f,) = ZombieRule().evaluate(store, _window(hours=9))
        assert f.evidence["method"] == "status"
        assert f.evidence["current_zombies"] == 11
        assert f.confidence == Confidence.MEDIUM
        store.close()

    def test_fd_leak_uses_recorded_limit(self, tmp_path):
        store = make_db(tmp_path, hours=10)
        add_sys_baseline(store, hours=10)
        proc_id = add_proc_series(
            store,
            "fdhog",
            rss_start=50 * 1024**2,
            rss_slope_per_h=0,
            hours=10,
            fds_slope=10,
        )
        store.set_proc_fd_limits(proc_id, 400, 800)
        store.conn.commit()
        diag = Engine(ProcdiagConfig(rules=RulesConfig(min_observation_hours=6))).run(
            store, only=["FD-LEAK-01"]
        )
        (f,) = diag.findings
        assert f.evidence["fd_limit_source"] == "measured"
        assert f.evidence["fd_soft_limit"] == 400
        assert "soft=400" in f.proposal
        store.close()


# ---------------------------------------------------------------------------
# Proposals for every new rule render all five sections
# ---------------------------------------------------------------------------


def test_new_rule_proposals_have_five_sections(tmp_path):
    store = make_db(tmp_path)
    now = time.time()
    add_sys_baseline(store, hours=1.0)
    _disk(store, now - 120, "/", inodes_total=100, inodes_used=99, read_only=0)
    _disk(store, now - 60, "/", inodes_total=100, inodes_used=99, read_only=1)
    for i in range(2):
        _svc(store, now - 600 + i * 60, "x.service", "restart", 2, "exit-code")
    store.buffer("kernel_events", [(now - 300, "fs_error", "EXT4-fs error")])
    store.flush()
    _session(store, started=now - 3000, boot=now - 4000, alive=now - 2500)
    _session(store, started=now - 2000, boot=now - 2100, alive=now)
    new = [
        "DISK-INODE-01",
        "FS-READONLY-01",
        "UNCLEAN-SHUTDOWN-01",
        "SERVICE-RESTART-01",
        "KERNEL-ERROR-01",
    ]
    diag = Engine(ProcdiagConfig(rules=RulesConfig(min_observation_hours=0))).run(
        store, only=new
    )
    store.close()
    assert sorted({f.rule_id for f in diag.findings}) == sorted(new)
    for f in diag.findings:
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in f.proposal, (f.rule_id, header)
