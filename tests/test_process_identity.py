"""Leaks and OOM kills are judged per real process and name the process.

Each test records ``proc_samples`` the way the process sampler writes them (one
row per PID), rolls them up with ``Storage.rollup_incremental`` and runs the
rules through ``Engine.run``.  One leaking worker among identical workers, a
leaking program that was restarted, and an OOM victim that its supervisor
started again must each be reported, with the PID and command line that tell
the user which process it is.
"""

from __future__ import annotations

import json
import sqlite3
import time
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from synth import add_sys_baseline, make_db
from test_compare import _leak, _leak_cmp

from procdiag.cli import cli
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


def _sample(ts, proc_id, pid, *, rss, fds=40, threads=10, pss=None):
    return make_row(
        "proc_samples",
        ts=ts,
        proc_id=proc_id,
        pid=pid,
        cpu_pct=1.0,
        rss=int(rss),
        pss=None if pss is None else int(pss),
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


def _start(hours):
    return time.time() // 60 * 60 - hours * _HOUR - 120


def _recording(tmp_path, hours, programs, processes, *, start=None):
    """*hours* of minutes ending two minutes ago, rolled up as the recorder does.

    *programs* are ``(name, cmdline)``; ``processes(h)`` lists the processes
    alive *h* hours in as ``(program index, pid, rss, fds, threads)``, with an
    optional PSS last.
    """
    start = _start(hours) if start is None else start
    store = make_db(tmp_path, hours=hours)
    add_sys_baseline(store, hours=hours, start_ts=start)
    ids = [_register(store, name, cmdline, start) for name, cmdline in programs]
    rows = []
    for m in range(int(hours * 60)):
        for i, pid, rss, fds, threads, *pss in processes(m / 60):
            rows.append(
                _sample(
                    start + m * 60 + 5,
                    ids[i],
                    pid,
                    rss=rss,
                    fds=fds,
                    threads=threads,
                    pss=pss[0] if pss else None,
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

    def test_only_the_leaking_workers_pss_confirms_its_leak(self, tmp_path):
        def processes(h):
            return [
                (0, 4000, 100 * _MiB, 40, 10, 80 * _MiB),
                (0, 4002, 100 * _MiB + 3 * _MiB * h, 40, 10, 80 * _MiB + 3 * _MiB * h),
            ]

        store, _ = _recording(tmp_path, 8, [("worker", "worker --pool")], processes)

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pid"] == 4002
        assert f.evidence["pss_slope_mb_per_h"] == pytest.approx(3.0, rel=0.01)
        assert f.confidence is Confidence.HIGH
        store.close()


class TestEveryWorkerOfAPoolLeaks:
    @staticmethod
    def _pool(tmp_path, workers=8):
        def processes(h):
            return [
                (
                    0,
                    pid,
                    100 * _MiB + (4 if pid == 3005 else 3) * _MiB * h,
                    30 + int(3 * h),
                    4,
                )
                for pid in range(3000, 3000 + workers)
            ]

        return _recording(tmp_path, 8, [("php-fpm", "php-fpm: pool www")], processes)

    def test_one_finding_per_rule_names_the_worst_worker_and_counts_the_rest(
        self, tmp_path
    ):
        store, _ = self._pool(tmp_path)

        found = {f.rule_id: f for f in _findings(store, _LEAK_RULES)}

        assert sorted(found) == ["FD-LEAK-01", "MEM-LEAK-01"]
        mem = found["MEM-LEAK-01"]
        assert mem.evidence["pid"] == 3005
        assert mem.evidence["slope_mb_per_h"] == pytest.approx(4.0, rel=0.01)
        assert mem.evidence["other_processes"] == 7
        others = mem.evidence["other_pids"]
        assert len(others) == 5 and set(others) < set(range(3000, 3008)) - {3005}
        assert "他 7 プロセスも同様" in mem.summary
        assert "他 7 プロセス" in mem.proposal
        assert found["FD-LEAK-01"].evidence["other_processes"] == 7
        store.close()

    def test_the_text_report_stays_short(self, tmp_path):
        store, _ = self._pool(tmp_path, workers=50)
        store.close()

        result = CliRunner().invoke(
            cli,
            ["check", "--db", str(store.path), "--only", "MEM-LEAK-01,FD-LEAK-01"],
        )

        assert result.output.count("PID 30") < 30
        assert "他 49 プロセスも同様" in result.output

    def test_two_leaking_programs_are_two_findings(self, tmp_path):
        def processes(h):
            return [
                (0, 3000, 100 * _MiB + 3 * _MiB * h, 40, 10),
                (0, 3001, 100 * _MiB + 3 * _MiB * h, 40, 10),
                (1, 3100, 100 * _MiB + 2 * _MiB * h, 40, 10),
            ]

        programs = [("php-fpm", "php-fpm: pool www"), ("collector", "collector")]
        store, _ = _recording(tmp_path, 8, programs, processes)

        found = _findings(store, ["MEM-LEAK-01"])

        assert sorted(f.subject for f in found) == ["collector", "php-fpm"]
        store.close()

    def test_the_pss_precheck_names_the_program_once(self, tmp_path):
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

    def test_a_restart_continues_the_worker_that_stopped_last(self, tmp_path):
        def processes(h):
            alive = []
            if h < 2:
                alive.append((0, 100, 100 * _MiB, 40, 10))
            if h < 4:
                alive.append((0, 101, 100 * _MiB + 5 * _MiB * h, 40, 10))
            if h >= 4.5:
                alive.append((0, 102, 100 * _MiB + 5 * _MiB * (h - 4.5), 40, 10))
            return alive

        store, _ = _recording(tmp_path, 8, [("svc", "/opt/plc/svc")], processes)

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pids"] == [101, 102]
        assert f.evidence["slope_mb_per_h"] == pytest.approx(5.0, rel=0.01)
        store.close()

    def test_a_program_restarted_every_few_minutes_is_not_a_leak(self, tmp_path):
        def processes(h):
            run, age = divmod(h * 60, 10)
            return [(0, 7000 + int(run), 50 * _MiB + 3 * _MiB * age, 40, 10)]

        store, _ = _recording(tmp_path, 8, [("batch", "/opt/plc/batch")], processes)

        assert _findings(store, ["MEM-LEAK-01"]) == []
        store.close()


class TestFindingsNameTheProcess:
    _SECRET = (
        "/usr/bin/python3 /opt/plc/uploader.py"
        " --db-url postgres://plc:hunter2@10.0.0.5/plc"
    )

    @staticmethod
    def _uploader(tmp_path, cmdline, *, start=None):
        def processes(h):
            return [
                (0, 2101, 60 * _MiB, 40, 10),
                (1, 2102, 60 * _MiB + 4 * _MiB * h, 40 + int(2 * h), 10 + int(2 * h)),
            ]

        programs = [
            ("python3", "/usr/bin/python3 /opt/plc/collector.py"),
            ("python3", cmdline),
        ]
        store, _ = _recording(tmp_path, 7, programs, processes, start=start)
        return store

    def test_two_programs_with_one_name_are_told_apart(self, tmp_path):
        store = self._uploader(tmp_path, "/usr/bin/python3 /opt/plc/uploader.py")

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pid"] == 2102
        assert f.summary.startswith("python3 uploader.py (PID 2102): RSS +4.0 MB/h")
        assert "systemctl restart python3" not in f.proposal
        store.close()

    def test_the_command_line_is_shown_nowhere(self, tmp_path):
        store = self._uploader(tmp_path, self._SECRET)
        store.close()
        html = tmp_path / "report.html"
        only = ["--only", ",".join(_LEAK_RULES)]
        runner = CliRunner()

        text = runner.invoke(cli, ["check", "--db", str(store.path), *only])
        data = runner.invoke(
            cli, ["check", "--db", str(store.path), "--format", "json", *only]
        ).stdout
        runner.invoke(cli, ["check", "--db", str(store.path), "-o", str(html), *only])

        assert len(json.loads(data)["findings"]) == 3
        for shown in (text.output, data, html.read_text(encoding="utf-8")):
            assert "uploader.py (PID 2102)" in shown
            assert "hunter2" not in shown
            assert "--db-url" not in shown

    @pytest.mark.parametrize(
        "cmdline",
        [
            "/usr/bin/python3 --config=/etc/plc/uploader.py",
            "/usr/bin/python3 /opt/plc/uploader2.py",
            "/usr/bin/python3 uploader.py",
            "/usr/bin/python3 scp://plc@10.0.0.5/opt/uploader.py",
            "/usr/bin/python3 /opt/plc/uploader.cfg",
        ],
    )
    def test_no_script_name_unless_it_is_a_plain_script_path(self, tmp_path, cmdline):
        store = self._uploader(tmp_path, cmdline)

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.summary.startswith("python3 (PID 2102): RSS")
        store.close()

    def test_digits_of_another_worker_are_not_shown(self, tmp_path):
        def processes(h):
            return [
                (0, 4000, 100 * _MiB, 40, 10),
                (0, 4001, 100 * _MiB + 5 * _MiB * h, 40, 10),
            ]

        store, _ = _recording(
            tmp_path, 8, [("worker", "/opt/app/worker --port 18001")], processes
        )

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pid"] == 4001
        assert "18001" not in f.summary + f.proposal + json.dumps(f.evidence)
        store.close()

    def test_the_user_checks_the_pid_before_acting_on_it(self, tmp_path):
        start = _start(7)
        store = self._uploader(
            tmp_path, "/usr/bin/python3 /opt/plc/uploader.py", start=start
        )
        last_minute = start + (7 * 60 - 1) * 60

        found = {f.rule_id: f for f in _findings(store, _LEAK_RULES)}

        assert sorted(found) == sorted(_LEAK_RULES)
        for f in found.values():
            assert f.evidence["last_seen"] == last_minute
            assert time.strftime("%Y-%m-%d %H:%M", time.localtime(last_minute)) in (
                f.proposal
            )
            assert "ps -p 2102 -o args=" in f.proposal
            assert "systemctl status 2102" in f.proposal
            assert "kill -TERM 2102" not in f.proposal
        assert "kill -TERM <PID>" in found["MEM-LEAK-01"].proposal
        store.close()


def _host(ts, *, crisis=False, swap_out=0):
    """A system sample of an 8 GiB host, out of memory when *crisis*."""
    return make_row(
        "sys_samples",
        ts=ts,
        cpu_total=10.0,
        cpu_max_core=12.0,
        mem_used=int(7.8 * _GiB) if crisis else 4 * _GiB,
        mem_available=int(0.2 * _GiB) if crisis else 4 * _GiB,
        swap_used=0,
        swap_in=0,
        swap_out=swap_out,
        load1=0.1,
        psi_cpu_some10=0.0,
        psi_mem_some10=0.0,
        psi_mem_full10=25.0 if crisis else 0.0,
        psi_io_some10=0.0,
        psi_io_full10=0.0,
        disk_read_bytes=0,
        disk_write_bytes=0,
        self_cpu=0.1,
        self_rss=50 * _MiB,
    )


def _oom_line(pid, name):
    return (
        f"Out of memory: Killed process {pid} ({name}) "
        "total-vm:3500000kB, anon-rss:3100000kB, file-rss:0kB"
    )


def _oom_findings(store):
    with patch("procdiag.diagnosis.crash._read_oom_journal", return_value=[]):
        return _findings(store, ["OOM-KILL-01"])


class TestOomVictim:
    @staticmethod
    def _oom(tmp_path, *, respawn, crisis=True, kernel_pid=1000, peak_rss=None):
        """One hour at 10 s: plc-gateway (PID 1000, 3 GiB) is killed at 30 min."""
        t0 = time.time() // 60 * 60 - 2 * _HOUR
        kill = t0 + 1800
        store = make_db(tmp_path, hours=1)
        sys_rows = [
            _host(t0 + i, crisis=crisis and kill - 60 <= t0 + i <= kill)
            for i in range(0, 3600, 10)
        ]
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
            "kernel_events", [(kill, "oom", _oom_line(kernel_pid, "plc-gateway"))]
        )
        store.flush(reason="synth")
        return store

    _run = staticmethod(_oom_findings)

    def test_a_victim_started_again_is_reported_with_the_kernel_line(self, tmp_path):
        store = self._oom(tmp_path, respawn=True)

        (f,) = self._run(store)

        assert f.subject == "plc-gateway"
        assert f.evidence["pid"] == 1000
        assert "--port" not in f.summary + f.proposal + json.dumps(f.evidence)
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


class TestExplainOomKill:
    def test_it_describes_the_recorded_kernel_line(self):
        result = CliRunner().invoke(cli, ["explain", "OOM-KILL-01"])

        assert result.exit_code == 0, result.output
        assert "Killed process" in result.output


class TestOomFindingsPerProgram:
    @staticmethod
    def _runs(tmp_path, runs, *, kernel_pids=()):
        """Seven hours at 10 s; *runs* are ``(pid, start_s, end_s)`` of the program
        reportgen (400 MiB), each disappearing during a minute of swapping."""
        t0 = time.time() // 60 * 60 - 8 * _HOUR
        ends = [t0 + end for _pid, _start, end in runs]
        store = make_db(tmp_path, hours=7)
        swapping = [
            _host(
                t0 + i, swap_out=4096 if any(e - 60 <= t0 + i <= e for e in ends) else 0
            )
            for i in range(0, 7 * 3600, 10)
        ]
        proc_id = _register(store, "reportgen", "/opt/plc/reportgen", t0)
        store.buffer("sys_samples", swapping)
        store.buffer(
            "proc_samples",
            [
                _sample(t0 + i, proc_id, pid, rss=400 * _MiB)
                for pid, start, end in runs
                for i in range(start, end + 1, 10)
            ],
        )
        store.buffer(
            "kernel_events",
            [
                (t0 + end, "oom", _oom_line(pid, "reportgen"))
                for pid, _start, end in runs
                if pid in kernel_pids
            ],
        )
        store.flush(reason="synth")
        return store

    def test_an_hourly_batch_flagged_by_the_heuristic_is_one_finding(self, tmp_path):
        runs = [(5000 + k, k * 3600, k * 3600 + 900) for k in range(6)]
        store = self._runs(tmp_path, runs)

        (f,) = _oom_findings(store)

        others = f.evidence["other_pids"]
        assert f.evidence["other_processes"] == 5
        assert sorted([f.evidence["pid"], *others]) == [pid for pid, _s, _e in runs]
        assert "他 5 プロセスも同様" in f.summary
        assert "他 5 プロセス" in f.proposal
        store.close()

    def test_each_kill_the_kernel_reported_is_its_own_finding(self, tmp_path):
        runs = [(6000, 0, 1800), (6001, 3600, 5400)]
        store = self._runs(tmp_path, runs, kernel_pids={6000, 6001})

        found = _oom_findings(store)

        assert sorted(f.evidence["pid"] for f in found) == [6000, 6001]
        assert all(f.confidence is Confidence.HIGH for f in found)
        store.close()

    def test_a_short_lived_process_the_kernel_killed_is_reported(self, tmp_path):
        store = self._runs(tmp_path, [(7000, 3600, 3780)], kernel_pids={7000})

        (f,) = _oom_findings(store)

        assert f.evidence["pid"] == 7000
        assert f.evidence["lifetime_minutes"] == 3.0
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
    def _schema4_db(tmp_path, *, hours=8, programs=1):
        """A DB as 0.6 wrote it: rollups per program, no pid column, schema 4.

        The first program (collector) grows 5 MiB/h for *hours*; the other
        *programs* are flat.
        """
        path = tmp_path / "v4.db"
        start = _start(8)
        store = Storage(path)
        add_sys_baseline(store, hours=8, start_ts=start)
        ids = [
            _register(store, "collector" if k == 0 else f"prog{k}", f"/opt/{k}", start)
            for k in range(programs)
        ]
        store.close()
        conn = sqlite3.connect(path)
        conn.executescript(
            "DROP TABLE proc_rollup_1m;"
            "DROP TABLE IF EXISTS proc_pid_rollup_1m;"
            "CREATE TABLE proc_rollup_1m (ts_min REAL, proc_id INTEGER,"
            " cpu_avg REAL, cpu_max REAL, rss_avg INTEGER, rss_max INTEGER,"
            " fds_max INTEGER, threads_max INTEGER, PRIMARY KEY (ts_min, proc_id));"
            "UPDATE meta SET value = '4' WHERE key = 'schema_version';"
        )
        conn.executemany(
            "INSERT INTO proc_rollup_1m VALUES (?, ?, 1.0, 1.0, ?, ?, 40, 10)",
            [
                (start + m * 60, proc_id, rss, rss)
                for m in range(int(hours * 60))
                for proc_id in ids
                for rss in [
                    100 * _MiB + (5 * _MiB * m // 60 if proc_id == ids[0] else 0)
                ]
            ],
        )
        conn.commit()
        conn.close()
        return path, start, ids[0]

    def test_it_is_read_as_before_without_migrating(self, tmp_path):
        path, _, _ = self._schema4_db(tmp_path)

        with Storage(path, read_only=True) as store:
            (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pid"] is None
        assert f.summary.startswith("collector: RSS +5.0 MB/h")

    def test_it_migrates_keeping_every_rollup_row(self, tmp_path):
        path, _, _ = self._schema4_db(tmp_path)

        with Storage(path) as store:
            pids = [r["pid"] for r in store.rows("proc_rollup_1m")]
            assert store.meta()["schema_version"] == "5"
        with Storage(path) as store:
            (f,) = _findings(store, ["MEM-LEAK-01"])

        assert len(pids) == 8 * 60 and set(pids) == {None}
        assert f.evidence["slope_mb_per_h"] == pytest.approx(5.0, rel=0.01)

    def test_it_migrates_on_a_disk_without_room_for_a_copy(self, tmp_path, monkeypatch):
        path, _, _ = self._schema4_db(tmp_path, programs=40)
        conn = sqlite3.connect(path)
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.close()
        connect = sqlite3.connect

        def nearly_full(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.execute(f"PRAGMA max_page_count = {pages + 32}")
            return conn

        monkeypatch.setattr(sqlite3, "connect", nearly_full)
        with Storage(path) as store:
            assert store.meta()["schema_version"] == "5"
            (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["slope_mb_per_h"] == pytest.approx(5.0, rel=0.01)

    def test_rollups_after_migrating_continue_the_old_ones(self, tmp_path):
        path, start, proc_id = self._schema4_db(tmp_path, hours=4)

        with Storage(path) as store:
            store.buffer(
                "proc_samples",
                [
                    _sample(start + m * 60 + 5, proc_id, 777, rss=rss)
                    for m in range(4 * 60, 8 * 60)
                    for rss in [100 * _MiB + 5 * _MiB * m // 60]
                ],
            )
            store.flush(reason="synth")
            store.rollup_incremental()
        with Storage(path, read_only=True) as store:
            (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pid"] == 777
        assert f.evidence["n"] == 8 * 60
        assert f.evidence["slope_mb_per_h"] == pytest.approx(5.0, rel=0.01)


class TestPerProcessRollupsAreKeptInBounds:
    @staticmethod
    def _forty_days(tmp_path):
        """Per-process rollups of three workers over 40 days, raw samples pruned."""
        now = time.time() // 60 * 60
        store = Storage(tmp_path / "long.db")
        proc_id = _register(store, "worker", "worker --pool", now - 40 * 86400)
        store.buffer(
            "proc_samples",
            [
                _sample(ts, proc_id, pid, rss=100 * _MiB)
                for ts in range(int(now - 40 * 86400), int(now - 3600), 300)
                for pid in (4000, 4001, 4002)
            ],
        )
        store.flush(reason="synth")
        store.rollup_incremental()
        store.retention(raw="1h", rollup=None)
        return store, now

    def test_the_size_guard_cuts_old_per_process_rollups(self, tmp_path):
        store, now = self._forty_days(tmp_path)
        store.conn.execute("VACUUM")
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = store.path.stat().st_size

        result = store.enforce_size_budget(
            max_bytes=int(size * 0.85), min_free_bytes=None
        )

        oldest, _ = store.time_bounds("proc_rollup_1m")
        assert "rollup_30d" in result["steps"]
        assert oldest >= now - 30 * 86400 - 60
        assert result["size_after"] <= size * 0.85
        store.close()

    def test_prune_cuts_old_per_process_rollups(self, tmp_path):
        store, now = self._forty_days(tmp_path)
        store.close()

        result = CliRunner().invoke(
            cli, ["prune", "--db", str(store.path), "--rollup", "7d"]
        )

        assert result.exit_code == 0, result.output
        with Storage(store.path, read_only=True) as reopened:
            oldest, _ = reopened.time_bounds("proc_rollup_1m")
        assert oldest >= now - 7 * 86400 - 60
