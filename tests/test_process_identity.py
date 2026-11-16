"""Leaks and OOM kills are judged per real process and name the process.

Each test records ``proc_samples`` the way the process sampler writes them (one
row per PID), rolls them up with ``Storage.rollup_incremental`` and runs the
rules through ``Engine.run``.  One leaking worker among identical workers, a
leaking program that was restarted, and an OOM victim that its supervisor
started again (named by the kernel's OOM line) must each be reported, with the
PID that tells the user which process it is.  A program that only replaced its
processes is no OOM kill.
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

from procdiag import storage
from procdiag.cli import cli
from procdiag.compare import Clear, Detected, Verdict
from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Confidence, Finding, Measurement, Observation, Severity
from procdiag.diagnosis import Engine
from procdiag.diagnosis.memory import select_pss_targets
from procdiag.diagnosis.trend import proc_instances, worst_process_per_program
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

    def test_a_pid_is_not_counted_as_another_process_of_itself(self):
        def fired(pid, slope):
            finding = Finding(
                rule_id="MEM-LEAK-01",
                severity=Severity.WARNING,
                subject="svc",
                summary=f"svc (PID {pid}): RSS +{slope}.0 MB/h",
                evidence={"proc_id": 1, "pid": pid},
                detected_at=(0.0, 1.0),
                confidence=Confidence.MEDIUM,
            )
            observation = Observation("svc", "hash", float(slope), finding.evidence)
            return Measurement(observation, finding)

        (f,) = worst_process_per_program(
            [fired(100, 5), fired(100, 4), fired(101, 3), fired(101, 2)]
        )

        assert f.evidence["pid"] == 100
        assert f.evidence["other_processes"] == 1
        assert f.evidence["other_pids"] == [101]
        assert "他 1 プロセスも同様 (PID 101)" in f.summary

    def test_rows_without_a_pid_are_not_counted_as_another_process(self, tmp_path):
        start = _start(7)
        store = make_db(tmp_path, hours=7)
        add_sys_baseline(store, hours=7, start_ts=start)
        proc_id = _register(store, "svc", "/opt/plc/svc", start)

        def rollup(table, m, slope, **pid):
            rss = int(100 * _MiB + slope * _MiB * m / 60)
            return make_row(
                table,
                ts_min=start + m * 60,
                proc_id=proc_id,
                cpu_avg=1.0,
                cpu_max=1.0,
                rss_avg=rss,
                rss_max=rss,
                fds_max=40,
                threads_max=10,
                **pid,
            )

        store.buffer(
            "proc_pid_rollup_1m",
            [rollup("proc_pid_rollup_1m", m, 6, pid=4000) for m in range(400)],
        )
        store.buffer(
            "proc_rollup_1m", [rollup("proc_rollup_1m", m, 3) for m in range(10, 390)]
        )
        store.flush(reason="synth")

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pid"] == 4000
        assert "other_processes" not in f.evidence
        assert "プロセスも同様" not in f.summary
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

    @pytest.mark.parametrize(
        ("overlap_min", "reported"), [(0, True), (2, True), (6, True), (7, False)]
    )
    def test_the_new_process_may_start_minutes_before_the_old_one_stops(
        self, tmp_path, overlap_min, reported
    ):
        def processes(h):
            alive = []
            if h < 4 + overlap_min / 60:
                alive.append((0, 5000, 100 * _MiB + 5 * _MiB * h, 40 + int(2 * h), 10))
            if h >= 4:
                age = h - 4
                alive.append(
                    (0, 5001, 100 * _MiB + 5 * _MiB * age, 40 + int(2 * age), 10)
                )
            return alive

        store, _ = _recording(
            tmp_path, 8, [("collector", "/opt/plc/collector")], processes
        )

        found = _findings(store, ["MEM-LEAK-01"])

        assert bool(found) is reported
        if reported:
            assert found[0].evidence["pids"] == [5000, 5001]
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

    @pytest.mark.parametrize("rule", _LEAK_RULES)
    def test_a_long_chain_of_restarts_shows_its_last_five_pids(self, tmp_path, rule):
        def processes(h):
            if h < 4:
                return [(0, 9000 + int(h * 6), 100 * _MiB, 40, 10)]
            grown = h - 4
            return [
                (
                    0,
                    9999,
                    100 * _MiB + 5 * _MiB * grown,
                    40 + int(5 * grown),
                    10 + int(5 * grown),
                )
            ]

        store, _ = _recording(tmp_path, 12, [("svc", "/opt/plc/svc")], processes)

        (f,) = _findings(store, [rule])

        (detected,) = [
            line for line in f.proposal.splitlines() if line.startswith("[検出]")
        ]
        assert len(f.evidence["pids"]) == 25
        assert "9020 → 9021 → 9022 → 9023 → 9999" in detected
        assert "9019" not in detected
        assert "25 プロセス" in detected
        assert len(detected) < 300
        store.close()


class TestWorkerThatStartsNearTheEndOfARecording:
    @staticmethod
    def _pool(tmp_path, minutes_before_end):
        """A leaking worker through 8 h; a flat one of its program starts late."""

        def processes(h):
            alive = [
                (0, 1000, 1500 * _MiB + 20 * _MiB * h, 50 + int(100 * h), 8 + int(h))
            ]
            if minutes_before_end and h >= 8 - minutes_before_end / 60:
                alive.append((0, 1001, 100 * _MiB, 50, 8))
            return alive

        return _recording(tmp_path, 8, [("svc", "/opt/plc/svc")], processes)

    @pytest.mark.parametrize("minutes_before_end", [1, 3, 4, 5, 10])
    def test_the_leak_stays_with_the_worker_that_leaks(
        self, tmp_path, minutes_before_end
    ):
        store, _ = self._pool(tmp_path, minutes_before_end)

        found = {f.rule_id: f for f in _findings(store, _LEAK_RULES)}

        assert sorted(found) == sorted(_LEAK_RULES)
        for f in found.values():
            assert f.evidence["pid"] == 1000
            assert f.evidence["pids"] == [1000]
            assert "PID 1000" in f.summary
            assert "PID 1001" not in f.summary
            assert "記録中に再起動されても" not in f.proposal
        store.close()

    @pytest.mark.parametrize("minutes_before_end", [1, 3, 4, 10])
    def test_the_numbers_are_those_of_the_leaking_worker_alone(
        self, tmp_path, minutes_before_end
    ):
        store, _ = self._pool(tmp_path, minutes_before_end)
        with_newcomer = {f.rule_id: f for f in _findings(store, _LEAK_RULES)}
        store.close()
        store, _ = self._pool(tmp_path / "alone", 0)
        alone = {f.rule_id: f for f in _findings(store, _LEAK_RULES)}
        store.close()

        for rule, f in with_newcomer.items():
            for key in (
                "current_fds",
                "current_threads",
                "days_to_soft_limit",
                "days_to_hard_limit",
                "days_to_oom",
                "slope_fd_per_h",
                "slope_thread_per_h",
                "slope_mb_per_h",
            ):
                assert f.evidence.get(key) == alone[rule].evidence.get(key), (rule, key)

    def test_a_replacement_that_starts_just_before_the_leaker_stops_is_a_restart(
        self, tmp_path
    ):
        def processes(h):
            minute = round(h * 60)
            alive = []
            if minute <= 477:
                alive.append((0, 1000, 100 * _MiB + 20 * _MiB * h, 50, 10))
            if minute >= 476:
                alive.append((0, 1001, 100 * _MiB, 50, 10))
            return alive

        store, _ = _recording(tmp_path, 8, [("svc", "/opt/plc/svc")], processes)

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pids"] == [1000, 1001]
        assert f.evidence["pid"] == 1001
        store.close()

    def test_a_leaker_with_a_hole_in_its_rows_has_stopped_when_it_is_the_usual_step_short(
        self, tmp_path
    ):
        def processes(h):
            minute = round(h * 60)
            alive = []
            if minute <= 469 and not 100 <= minute < 110:
                alive.append((0, 1000, 100 * _MiB + 20 * _MiB * h, 50, 10))
            if minute >= 468:
                alive.append((0, 1001, 100 * _MiB, 50, 10))
            return alive

        store, _ = _recording(tmp_path, 8, [("svc", "/opt/plc/svc")], processes)

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pids"] == [1000, 1001]
        store.close()

    def test_a_process_seen_once_a_minute_before_the_end_may_still_be_running(self):
        end = 100_000.0

        def row(proc_id, pid, ts):
            return {"proc_id": proc_id, "pid": pid, "ts_min": ts, "rss_avg": 1.0}

        instances = proc_instances(
            [row(1, 1, end - 60), row(1, 2, end - 60), row(1, 2, end)]
            + [row(2, 9, end - 3600 + 60 * k) for k in range(61)],
            "rss_avg",
        )

        assert len([i for i in instances if i.proc_id == 1]) == 2

    @pytest.mark.parametrize(
        ("seconds_before_end", "chained"), [(240, False), (330, True)]
    )
    def test_a_sparsely_sampled_worker_is_running_until_a_step_before_the_end(
        self, seconds_before_end, chained
    ):
        step = 300.0
        end = 100_000.0

        def rows(proc_id, pid, first, last, every):
            ts = first
            while ts <= last:
                yield {"proc_id": proc_id, "pid": pid, "ts_min": ts, "rss_avg": 1.0}
                ts += every

        instances = proc_instances(
            list(rows(1, 1000, end - 6 * 3600, end - seconds_before_end, step))
            + list(rows(1, 1001, end - 180, end, step))
            + list(rows(2, 2000, end - 6 * 3600, end, 60.0)),
            "rss_avg",
        )

        assert (len([i for i in instances if i.proc_id == 1]) == 1) is chained


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
            "/usr/bin/python3 https://example.com/uploader.py",
            "/usr/bin/python3 /srv/plc@example.com/uploader.py",
            "/usr/bin/python3 /opt/plc/uploader.cfg",
            "/usr/bin/python3 -" + "v" * 161 + " /opt/plc/uploader.py",
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
            assert "「python3 uploader.py」 (PID 2102)" in f.proposal
            assert "ps -p 2102 -o args=" in f.proposal
            assert "systemctl status 2102" in f.proposal
            assert "kill -TERM 2102" not in f.proposal
            (permanent,) = [
                line
                for line in f.proposal.splitlines()
                if line.startswith("[恒久対処]")
            ]
            assert "2102" not in permanent
        assert "kill -TERM <PID>" in found["MEM-LEAK-01"].proposal
        assert "py-spy dump --pid <PID>" in found["MEM-LEAK-01"].proposal
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
    def _oom(
        tmp_path,
        *,
        respawn,
        crisis=True,
        kernel_pid=1000,
        peak_rss=None,
        oom_line_at=0,
        program=("plc-gateway", "/opt/plc/gateway --port 502"),
    ):
        """One hour at 10 s: the *program* (PID 1000, 3 GiB) is killed at 30 min.

        The kernel logs the kill *oom_line_at* seconds after its last sample.
        """
        t0 = time.time() // 60 * 60 - 2 * _HOUR
        kill = t0 + 1800
        store = make_db(tmp_path, hours=1)
        sys_rows = [
            _host(t0 + i, crisis=crisis and kill - 60 <= t0 + i <= kill)
            for i in range(0, 3600, 10)
        ]
        victim = _register(store, *program, t0)
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
            [(kill + oom_line_at, "oom", _oom_line(kernel_pid, program[0]))],
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

    @pytest.mark.parametrize(
        ("oom_line_at", "matched"),
        [(-180, True), (-181, False), (60, True), (61, False)],
    )
    def test_the_kernel_line_names_the_victim_only_around_its_last_sample(
        self, tmp_path, oom_line_at, matched
    ):
        store = self._oom(tmp_path, respawn=True, oom_line_at=oom_line_at)

        found = self._run(store)

        assert bool(found) is matched
        store.close()

    def test_the_kernel_line_reports_a_kill_without_a_host_wide_crisis(self, tmp_path):
        store = self._oom(tmp_path, respawn=True, crisis=False)

        (f,) = self._run(store)

        assert f.evidence["pid"] == 1000
        assert f.confidence is Confidence.HIGH
        store.close()

    def test_a_kernel_line_about_another_pid_is_not_this_process(self, tmp_path):
        store = self._oom(tmp_path, respawn=False, kernel_pid=999)

        (f,) = self._run(store)

        assert f.evidence["pid"] == 1000
        assert "kernel_line" not in f.evidence
        assert f.confidence is Confidence.MEDIUM
        store.close()

    def test_a_victim_started_again_is_not_suspected_without_its_kernel_line(
        self, tmp_path
    ):
        store = self._oom(tmp_path, respawn=True, kernel_pid=999)

        assert self._run(store) == []
        store.close()

    def test_the_report_names_the_script_of_the_victim(self, tmp_path):
        program = ("python3", "/usr/bin/python3 /opt/plc/gateway.py --port 502")
        store = self._oom(tmp_path, respawn=True, program=program)

        (f,) = self._run(store)

        (detected,) = [
            line for line in f.proposal.splitlines() if line.startswith("[検出]")
        ]
        assert "python3 gateway.py (PID 1000) が消滅" in detected
        assert "--port" not in f.proposal
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

    def test_it_says_the_program_not_the_last_process_must_have_run_ten_minutes(self):
        result = CliRunner().invoke(cli, ["explain", "OOM-KILL-01"])

        assert "最初の記録から 10 分以上" in result.output
        assert "最後のプロセスが 10 分に満たなくても" in result.output


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

        assert f.evidence["pid"] == 5005
        assert "other_processes" not in f.evidence
        assert "プロセスも同様" not in f.summary
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


class TestNoOomKillWhileTheProgramRuns:
    @staticmethod
    def _recorded(tmp_path, name, runs, swap_minutes):
        """Twelve hours of an 8 GiB host with a 4 KiB swap-out in each of
        *swap_minutes*.  *runs* are ``(pid, start_h, end_h)`` of the program
        *name* (300 MiB, flat); a plc process runs throughout."""
        t0 = time.time() // 60 * 60 - 13 * _HOUR
        store = make_db(tmp_path, hours=12)
        store.buffer(
            "sys_samples",
            [
                _host(t0 + m * 60, swap_out=4096 if m in swap_minutes else 0)
                for m in range(12 * 60)
            ],
        )
        program = _register(store, name, f"/srv/{name}/app", t0)
        plc = _register(store, "plc", "/opt/plc/plc", t0)
        store.buffer(
            "proc_samples",
            [
                _sample(t0 + k * 30, program, pid, rss=300 * _MiB)
                for pid, start_h, end_h in runs
                for k in range(int(start_h * 120), int(end_h * 120))
            ]
            + [_sample(t0 + k * 30, plc, 1, rss=100 * _MiB) for k in range(12 * 120)],
        )
        store.flush(reason="synth")
        return store

    @pytest.mark.parametrize("swap_every", [17, 47, 180])
    def test_a_pool_that_recycles_its_workers_is_not_an_oom_kill(
        self, tmp_path, swap_every
    ):
        runs = []
        for slot in range(4):
            start = -slot * 0.53
            while start < 12:
                if start + 2 > 0:
                    runs.append((7000 + len(runs), max(start, 0), min(start + 2, 12)))
                start += 2
        store = self._recorded(
            tmp_path, "gunicorn", runs, set(range(0, 12 * 60, swap_every))
        )

        assert _oom_findings(store) == []
        store.close()

    def test_a_planned_restart_is_not_an_oom_kill(self, tmp_path):
        runs = [(3100, 0, 5), (3101, 5, 12)]
        store = self._recorded(tmp_path, "plc-gateway", runs, {5 * 60 - 1})

        assert _oom_findings(store) == []
        store.close()


class TestOomWithoutKernelLineJudgesTheProgramsSpan:
    @staticmethod
    def _recorded(tmp_path, runs, *, name="plc-gateway"):
        """Twelve hours of an 8 GiB host and the program *name* (300 MiB, flat).

        *runs* are ``(pid, first_tick, end_tick)`` in 30 s samples.  A 4 KiB
        swap-out a minute before the last sample is the only memory crisis.
        """
        last_sample = max(end for _pid, _first, end in runs) - 1

        def hours(tick):
            return (tick + 0.5) / 120

        return TestNoOomKillWhileTheProgramRuns._recorded(
            tmp_path,
            name,
            [(pid, hours(first), hours(end)) for pid, first, end in runs],
            {last_sample // 2 - 1},
        )

    @pytest.mark.parametrize("lived_min", [3, 5, 9])
    def test_a_service_restarted_by_hand_that_dies_young_is_suspected(
        self, tmp_path, lived_min
    ):
        runs = [(3100, 0, 600), (3101, 600, 600 + 2 * lived_min)]
        store = self._recorded(tmp_path, runs)

        (f,) = _oom_findings(store)

        assert f.evidence["pid"] == 3101
        assert f.confidence is Confidence.MEDIUM
        assert f.evidence["lifetime_minutes"] == lived_min - 0.5
        assert f.evidence["program_lifetime_minutes"] == 300 + lived_min - 0.5
        store.close()

    def test_a_crash_loop_that_ends_in_a_kill_is_suspected(self, tmp_path):
        runs = [(5000 + k, 7 * k, 7 * k + 6) for k in range(85)]
        store = self._recorded(tmp_path, runs, name="uploader")

        (f,) = _oom_findings(store)

        assert f.evidence["pid"] == 5084
        assert f.evidence["lifetime_minutes"] == 2.5
        assert f.evidence["program_lifetime_minutes"] == 296.5
        store.close()

    def test_the_report_says_the_program_ran_long_enough_not_the_process(
        self, tmp_path
    ):
        store = self._recorded(tmp_path, [(3100, 0, 600), (3101, 600, 606)])

        (f,) = _oom_findings(store)

        assert "PID 3101" in f.summary
        assert "生存 2.5分, 同じプログラムの最初のプロセスから 302.5分," in f.summary
        detected, basis = [
            line
            for line in f.proposal.splitlines()
            if line.startswith(("[検出]", "[根拠]"))
        ]
        assert (
            "生存期間 2.5 分、同じプログラムの最初のプロセスから 302.5 分)" in detected
        )
        assert (
            "生存期間は 2.5分 だが、同じプログラムは最初のプロセスから 302.5分 >= 10分"
            in basis
        )
        store.close()

    def test_the_program_span_is_not_mentioned_when_the_process_lived_long(
        self, tmp_path
    ):
        store = self._recorded(tmp_path, [(3100, 0, 600), (3101, 600, 630)])

        (f,) = _oom_findings(store)

        assert f.evidence["lifetime_minutes"] == 14.5
        assert "program_lifetime_minutes" not in f.evidence
        assert "同じプログラムの最初のプロセスから" not in f.summary
        assert "同じプログラムの最初のプロセスから" not in f.proposal
        assert "生存期間 14.5分 >= 10分 の長命プロセス" in f.proposal
        store.close()

    def test_a_program_recorded_for_exactly_ten_minutes_is_suspected(self, tmp_path):
        store = self._recorded(tmp_path, [(3100, 700, 721)])

        (f,) = _oom_findings(store)

        assert f.evidence["lifetime_minutes"] == 10.0
        store.close()

    def test_a_program_recorded_for_just_under_ten_minutes_is_not(self, tmp_path):
        store = self._recorded(tmp_path, [(3100, 700, 720)])

        assert _oom_findings(store) == []
        store.close()

    def test_the_ten_minutes_follow_the_setting_in_the_report(self, tmp_path):
        store = self._recorded(tmp_path, [(3100, 0, 600), (3101, 600, 606)])
        config = ProcdiagConfig(
            rules=RulesConfig(overrides={"OOM-KILL-01": {"min_lifetime_s": 120}})
        )

        diag = Engine(config).run(store, only=["OOM-KILL-01"])

        (f,) = diag.findings
        assert f.evidence["min_lifetime_minutes"] == 2.0
        assert "生存期間 2.5分 >= 2分 の長命プロセス" in f.proposal
        assert ">= 10分" not in f.proposal
        store.close()

    def test_the_ten_minutes_of_the_program_span_follow_the_setting_too(self, tmp_path):
        store = self._recorded(tmp_path, [(3100, 0, 600), (3101, 600, 606)])
        config = ProcdiagConfig(
            rules=RulesConfig(overrides={"OOM-KILL-01": {"min_lifetime_s": 300}})
        )

        diag = Engine(config).run(store, only=["OOM-KILL-01"])

        (f,) = diag.findings
        assert "最初のプロセスから 302.5分 >= 5分 記録されていた" in f.proposal
        store.close()

    @pytest.mark.parametrize(
        "runs",
        [
            [(3100, 720, 738)],
            [(5000, 720, 726), (5001, 727, 733), (5002, 734, 740)],
        ],
        ids=["one process", "crash loop"],
    )
    def test_a_program_that_ran_under_ten_minutes_is_not_suspected(
        self, tmp_path, runs
    ):
        store = self._recorded(tmp_path, runs)

        assert _oom_findings(store) == []
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
            "ALTER TABLE disk_samples DROP COLUMN avail;"
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

    def test_its_findings_do_not_restart_a_process_by_its_name(self, tmp_path):
        path, _, _ = self._schema4_db(tmp_path)

        with Storage(path, read_only=True) as store:
            (f,) = _findings(store, ["MEM-LEAK-01"])

        assert "systemctl restart collector" not in f.proposal
        assert "pgrep -a -- collector" in f.proposal
        assert "systemctl status <PID>" in f.proposal

    def test_a_migration_cut_short_leaves_the_database_as_it_was(
        self, tmp_path, monkeypatch
    ):
        path, _, _ = self._schema4_db(tmp_path)

        def killed(conn):
            raise sqlite3.OperationalError("killed")

        monkeypatch.setitem(storage._MIGRATIONS, 5, [*storage._MIGRATIONS[5], killed])
        with pytest.raises(sqlite3.OperationalError):
            Storage(path)
        conn = sqlite3.connect(path)
        (version,) = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        tables = {
            name
            for (name,) in conn.execute("SELECT name FROM sqlite_master")
            if name.startswith("proc_")
        }
        conn.close()

        assert version == "4"
        assert "proc_pid_rollup_1m" not in tables

    def test_rollups_of_both_tables_come_back_in_time_order(self, tmp_path):
        path, start, proc_id = self._schema4_db(tmp_path, hours=1)

        with Storage(path) as store:
            store.buffer(
                "proc_pid_rollup_1m",
                [
                    make_row(
                        "proc_pid_rollup_1m",
                        ts_min=start - m * 60,
                        proc_id=proc_id,
                        pid=777,
                        cpu_avg=1.0,
                        cpu_max=1.0,
                        rss_avg=100 * _MiB,
                        rss_max=100 * _MiB,
                        fds_max=40,
                        threads_max=10,
                    )
                    for m in range(1, 31)
                ],
            )
            store.flush(reason="synth")
            minutes = [row["ts_min"] for row in store.rows("proc_rollup_1m")]

        assert len(minutes) == 90
        assert minutes == sorted(minutes)

    def test_it_migrates_keeping_every_rollup_row(self, tmp_path):
        path, _, _ = self._schema4_db(tmp_path)

        with Storage(path) as store:
            pids = [r["pid"] for r in store.rows("proc_rollup_1m")]
            assert store.meta()["schema_version"] == str(storage.SCHEMA_VERSION)
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
            assert store.meta()["schema_version"] == str(storage.SCHEMA_VERSION)
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


class TestClockStepBackAcrossThePidBump:
    @pytest.mark.parametrize(
        ("legacy", "with_pids"),
        [(range(0, 240), range(120, 360)), (range(120, 360), range(0, 240))],
        ids=["rows-after-the-step-start-later", "rows-after-the-step-start-first"],
    )
    def test_rows_without_a_pid_and_the_rows_after_them_are_one_leak_whatever_the_clock_says(
        self, tmp_path, legacy, with_pids
    ):
        start = _start(8)
        store = make_db(tmp_path, hours=8)
        add_sys_baseline(store, hours=8, start_ts=start)
        proc_id = _register(store, "svc", "/opt/plc/svc", start)

        def rollup(table, m, level, **pid):
            rss = int(level * _MiB + 5 * _MiB * m / 60)
            return make_row(
                table,
                ts_min=start + m * 60,
                proc_id=proc_id,
                cpu_avg=1.0,
                cpu_max=1.0,
                rss_avg=rss,
                rss_max=rss,
                fds_max=40,
                threads_max=10,
                **pid,
            )

        store.buffer(
            "proc_rollup_1m", [rollup("proc_rollup_1m", m, 100) for m in legacy]
        )
        store.buffer(
            "proc_pid_rollup_1m",
            [rollup("proc_pid_rollup_1m", m, 90, pid=777) for m in with_pids],
        )
        store.flush(reason="synth")

        (f,) = _findings(store, ["MEM-LEAK-01"])

        assert f.evidence["pids"] == [777]
        assert f.evidence["n"] == 240 + 240
        assert f.evidence["slope_mb_per_h"] == pytest.approx(5.0, rel=0.01)
        store.close()


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
