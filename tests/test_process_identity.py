"""Leaks and OOM kills are judged per real process and name the process.

Each test records ``proc_samples`` the way the process sampler writes them (one
row per PID), rolls them up with ``Storage.rollup_incremental`` and runs the
rules through ``Engine.run``.  One leaking worker among identical workers, a
leaking program that was restarted, and an OOM victim that its supervisor
started again must each be reported, with the PID and command line that tell
the user which process it is.
"""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import patch

import pytest
from synth import add_sys_baseline, make_db
from test_compare import _leak, _leak_cmp

from procdiag.compare import Clear, Detected, Verdict
from procdiag.config import ProcdiagConfig
from procdiag.core import Confidence
from procdiag.diagnosis import Engine
from procdiag.diagnosis.memory import select_pss_targets
from procdiag.storage import Storage, make_row

_MiB = 1024**2
_GiB = 1024**3
_HOUR = 3600.0
_LEAK_RULES = ["MEM-LEAK-01", "FD-LEAK-01", "THREAD-LEAK-01"]


def _sample(ts, proc_id, pid, *, rss, fds=40, threads=10):
    return make_row(
        "proc_samples",
        ts=ts,
        proc_id=proc_id,
        pid=pid,
        cpu_pct=1.0,
        rss=int(rss),
        pss=None,
        num_threads=threads,
        num_fds=fds,
        io_read_delta=0,
        io_write_delta=0,
    )


def _register(store, name, cmdline, ts):
    proc_id = store.proc_id_for(
        name=name, cmdline_hash=f"hash:{cmdline}", cmdline_sample=cmdline, ts=ts
    )
    store.conn.commit()
    return proc_id


def _recording(tmp_path, hours, programs, processes):
    """*hours* of minutes ending two minutes ago, rolled up as the recorder does.

    *programs* are ``(name, cmdline)``; ``processes(h)`` lists the processes
    alive *h* hours in as ``(program index, pid, rss, fds, threads)``.
    """
    start = time.time() // 60 * 60 - hours * _HOUR - 120
    store = make_db(tmp_path, hours=hours)
    add_sys_baseline(store, hours=hours, start_ts=start)
    ids = [_register(store, name, cmdline, start) for name, cmdline in programs]
    rows = []
    for m in range(int(hours * 60)):
        for i, pid, rss, fds, threads in processes(m / 60):
            rows.append(
                _sample(
                    start + m * 60 + 5, ids[i], pid, rss=rss, fds=fds, threads=threads
                )
            )
    store.buffer("proc_samples", rows)
    store.flush(reason="synth")
    store.rollup_incremental()
    return store, ids


def _findings(store, rules):
    diag = Engine(ProcdiagConfig()).run(store, only=rules)
    assert sorted(diag.evaluated_rules) == sorted(rules)
    return diag.findings


class TestOneLeakingWorkerAmongIdenticalWorkers:
    @staticmethod
    def _pool(tmp_path):
        def processes(h):
            return [
                (0, pid, 100 * _MiB + (3 * _MiB * h if pid == 4002 else 0), 40, 10)
                for pid in (4000, 4001, 4002, 4003)
            ]

        return _recording(tmp_path, 8, [("worker", "worker --pool")], processes)

    def test_the_leaking_worker_is_reported_with_its_pid(self, tmp_path):
        store, _ = self._pool(tmp_path)

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pid"] == 4002
        assert f.evidence["slope_mb_per_h"] == pytest.approx(3.0, rel=0.01)
        assert "PID 4002" in f.summary
        store.close()

    def test_the_pss_precheck_picks_the_leaking_workers_program(self, tmp_path):
        store, (proc_id,) = self._pool(tmp_path)
        rows = store.rows("proc_rollup_1m", since=time.time() - 6 * _HOUR)

        assert select_pss_targets(rows) == [proc_id]
        store.close()


class TestLeakingProgramRestarted:
    @pytest.mark.parametrize("restart_h", [0.6, 2.1, 4.1, 5.6, 7.6])
    def test_a_leak_that_goes_on_after_a_restart_is_reported(self, tmp_path, restart_h):
        def processes(h):
            pid, age = (5001, h - restart_h) if h >= restart_h else (5000, h)
            rss = 100 * _MiB + 5 * _MiB * age
            return [(0, pid, rss, 40 + int(2 * age), 10 + int(2 * age))]

        store, _ = _recording(
            tmp_path, 8, [("collector", "/opt/plc/collector")], processes
        )

        found = {f.rule_id: f for f in _findings(store, _LEAK_RULES)}

        assert sorted(found) == sorted(_LEAK_RULES)
        assert found["MEM-LEAK-01"].evidence["slope_mb_per_h"] == pytest.approx(
            5.0, rel=0.01
        )
        for f in found.values():
            assert f.evidence["pid"] == 5001
            assert f.evidence["pids"] == [5000, 5001]
        store.close()

    def test_a_flat_program_restarted_at_another_level_is_not_a_leak(self, tmp_path):
        def processes(h):
            return [
                (
                    (0, 6001, 160 * _MiB, 40, 10)
                    if h >= 4
                    else (0, 6000, 100 * _MiB, 40, 10)
                )
            ]

        store, _ = _recording(
            tmp_path, 8, [("collector", "/opt/plc/collector")], processes
        )

        assert _findings(store, ["MEM-LEAK-01"]) == []
        store.close()

    def test_a_program_restarted_every_few_minutes_is_not_a_leak(self, tmp_path):
        def processes(h):
            run, age = divmod(h * 60, 10)
            return [(0, 7000 + int(run), 50 * _MiB + 3 * _MiB * age, 40, 10)]

        store, _ = _recording(tmp_path, 8, [("batch", "/opt/plc/batch")], processes)

        assert _findings(store, ["MEM-LEAK-01"]) == []
        store.close()


class TestFindingsNameTheProcess:
    def test_two_programs_with_one_name_are_told_apart(self, tmp_path):
        def processes(h):
            return [
                (0, 2101, 60 * _MiB, 40, 10),
                (1, 2102, 60 * _MiB + 4 * _MiB * h, 40, 10),
            ]

        programs = [
            ("python3", "/usr/bin/python3 /opt/plc/collector.py"),
            ("python3", "/usr/bin/python3 /opt/plc/uploader.py"),
        ]
        store, _ = _recording(tmp_path, 7, programs, processes)

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pid"] == 2102
        assert f.evidence["cmdline"] == "/usr/bin/python3 /opt/plc/uploader.py"
        assert "/opt/plc/uploader.py" in f.proposal
        assert "systemctl status 2102" in f.proposal
        assert "systemctl restart python3" not in f.proposal
        store.close()


class TestOomVictim:
    @staticmethod
    def _oom(tmp_path, *, respawn, crisis=True, kernel_pid=1000, peak_rss=None):
        """One hour at 10 s: plc-gateway (PID 1000, 3 GiB) is killed at 30 min."""
        t0 = time.time() // 60 * 60 - 2 * _HOUR
        kill = t0 + 1800
        store = make_db(tmp_path, hours=1)
        sys_rows = []
        for i in range(0, 3600, 10):
            ts = t0 + i
            hit = crisis and kill - 60 <= ts <= kill
            sys_rows.append(
                make_row(
                    "sys_samples",
                    ts=ts,
                    cpu_total=10.0,
                    cpu_max_core=12.0,
                    mem_used=int(7.8 * _GiB) if hit else 4 * _GiB,
                    mem_available=int(0.2 * _GiB) if hit else 4 * _GiB,
                    swap_used=0,
                    swap_in=0,
                    swap_out=0,
                    load1=0.1,
                    psi_cpu_some10=0.0,
                    psi_mem_some10=0.0,
                    psi_mem_full10=25.0 if hit else 0.0,
                    psi_io_some10=0.0,
                    psi_io_full10=0.0,
                    disk_read_bytes=0,
                    disk_write_bytes=0,
                    self_cpu=0.1,
                    self_rss=50 * _MiB,
                )
            )
        victim = _register(store, "plc-gateway", "/opt/plc/gateway --port 502", t0)
        other = _register(store, "sshd", "/usr/sbin/sshd -D", t0)
        procs = []
        for i in range(0, 3600, 10):
            ts = t0 + i
            if ts <= kill:
                rss = peak_rss if peak_rss and i < 600 else 3 * _GiB
                procs.append(_sample(ts, victim, 1000, rss=rss))
            elif respawn and ts >= kill + 5:
                procs.append(_sample(ts, victim, 2000, rss=300 * _MiB))
            procs.append(_sample(ts, other, 50, rss=20 * _MiB))
        store.buffer("sys_samples", sys_rows)
        store.buffer("proc_samples", procs)
        store.buffer(
            "kernel_events",
            [
                (
                    kill,
                    "oom",
                    f"Out of memory: Killed process {kernel_pid} (plc-gateway) "
                    "total-vm:3500000kB, anon-rss:3100000kB, file-rss:0kB",
                )
            ],
        )
        store.flush(reason="synth")
        return store

    @staticmethod
    def _run(store):
        with patch("procdiag.diagnosis.crash._read_oom_journal", return_value=[]):
            return _findings(store, ["OOM-KILL-01"])

    def test_a_victim_started_again_is_reported_with_the_kernel_line(self, tmp_path):
        store = self._oom(tmp_path, respawn=True)

        (f,) = self._run(store)

        assert f.subject == "plc-gateway"
        assert f.evidence["pid"] == 1000
        assert f.evidence["cmdline"] == "/opt/plc/gateway --port 502"
        assert "Killed process 1000 (plc-gateway)" in f.evidence["kernel_line"]
        assert f.confidence is Confidence.HIGH
        assert "PID 1000" in f.summary
        store.close()

    def test_the_kernel_line_reports_a_kill_without_a_host_wide_crisis(self, tmp_path):
        store = self._oom(tmp_path, respawn=True, crisis=False)

        (f,) = self._run(store)

        assert f.evidence["pid"] == 1000
        assert f.confidence is Confidence.HIGH
        store.close()

    def test_a_kernel_line_about_another_pid_is_not_this_process(self, tmp_path):
        store = self._oom(tmp_path, respawn=True, kernel_pid=999)

        (f,) = self._run(store)

        assert f.evidence["pid"] == 1000
        assert "kernel_line" not in f.evidence
        assert f.confidence is Confidence.MEDIUM
        store.close()

    def test_last_rss_is_the_last_sample_not_the_peak(self, tmp_path):
        store = self._oom(tmp_path, respawn=False, peak_rss=6 * _GiB)

        (f,) = self._run(store)

        assert f.evidence["last_rss_mb"] == 3072.0
        assert "最終 RSS 3072.0 MB" in f.summary
        store.close()


class TestCompareOneProgramWithManyProcesses:
    def test_the_program_is_paired_by_its_worst_process(self):
        before = [_leak("worker", 3.0), _leak("worker", 0.0), _leak("worker", 0.1)]
        after = [_leak("worker", 0.2), _leak("worker", 0.0), _leak("worker", 0.1)]

        (row,) = _leak_cmp(before, after).rows

        assert row.verdict is Verdict.RESOLVED
        assert isinstance(row.before, Detected) and row.before.value == 3.0
        assert isinstance(row.after, Clear) and row.after.value == 0.2


class TestDatabaseFromBeforePids:
    @staticmethod
    def _schema4_db(tmp_path):
        """A DB as 0.6 wrote it: rollups per program, no pid column, schema 4."""
        path = tmp_path / "v4.db"
        start = time.time() // 60 * 60 - 8 * _HOUR - 120
        store = Storage(path)
        add_sys_baseline(store, hours=8, start_ts=start)
        proc_id = _register(store, "collector", "/opt/plc/collector", start)
        store.close()
        conn = sqlite3.connect(path)
        conn.executescript(
            "DROP TABLE proc_rollup_1m;"
            "CREATE TABLE proc_rollup_1m (ts_min REAL, proc_id INTEGER,"
            " cpu_avg REAL, cpu_max REAL, rss_avg INTEGER, rss_max INTEGER,"
            " fds_max INTEGER, threads_max INTEGER, PRIMARY KEY (ts_min, proc_id));"
            "UPDATE meta SET value = '4' WHERE key = 'schema_version';"
        )
        conn.executemany(
            "INSERT INTO proc_rollup_1m VALUES (?, ?, 1.0, 1.0, ?, ?, 40, 10)",
            [
                (start + m * 60, proc_id, rss, rss)
                for m in range(8 * 60)
                for rss in [100 * _MiB + 5 * _MiB * m // 60]
            ],
        )
        conn.commit()
        conn.close()
        return path

    def test_it_is_read_as_before_without_migrating(self, tmp_path):
        path = self._schema4_db(tmp_path)

        with Storage(path, read_only=True) as store:
            (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pid"] is None
        assert f.summary.startswith("collector: RSS +5.0 MB/h")

    def test_it_migrates_keeping_every_rollup_row(self, tmp_path):
        path = self._schema4_db(tmp_path)

        with Storage(path) as store:
            pids = [r["pid"] for r in store.rows("proc_rollup_1m")]
            assert store.meta()["schema_version"] == "5"
        with Storage(path) as store:
            (f,) = _findings(store, ["MEM-LEAK-01"])

        assert len(pids) == 8 * 60 and set(pids) == {None}
        assert f.evidence["slope_mb_per_h"] == pytest.approx(5.0, rel=0.01)
