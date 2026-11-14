"""I/O and CPU saturation rules report how long the pressure really lasted.

Each test builds a recording at the real 1 Hz system rate, runs the rule through
``Engine.run`` and checks the finding as the report shows it: a few seconds, a
recording gap or one busy second per minute is never reported as minutes of
pressure, and a sustained episode keeps its real length and its real culprit.
"""

from __future__ import annotations

import pytest
from synth import make_db

from procdiag.config import ProcdiagConfig
from procdiag.core import Window
from procdiag.diagnosis import Engine
from procdiag.storage import make_row

_MiB = 1024**2
_GiB = 1024**3
_T0 = 1_780_000_000.0 - (1_780_000_000.0 % 60) - 6 * 3600


def _run(store, rule_id, window=None):
    diag = Engine(ProcdiagConfig()).run(store, only=[rule_id], window=window)
    assert diag.evaluated_rules == [rule_id], (
        diag.skipped_rules,
        diag.uncovered_rules,
    )
    return [f for f in diag.findings if f.rule_id == rule_id]


def _sys(ts, *, psi_io=0.0, psi_cpu=0.0, cpu=5.0, core=7.0):
    return make_row(
        "sys_samples",
        ts=ts,
        cpu_total=cpu,
        cpu_max_core=core,
        mem_used=2 * _GiB,
        mem_available=6 * _GiB,
        swap_used=0,
        swap_in=0,
        swap_out=0,
        load1=0.1,
        psi_cpu_some10=psi_cpu,
        psi_mem_some10=0.0,
        psi_mem_full10=0.0,
        psi_io_some10=psi_io,
        psi_io_full10=0.0,
        disk_read_bytes=0,
        disk_write_bytes=0,
        self_cpu=0.1,
        self_rss=50 * _MiB,
    )


def _proc(ts, proc_id, *, read=0, write=0):
    return make_row(
        "proc_samples",
        ts=ts,
        proc_id=proc_id,
        pid=100 + proc_id,
        cpu_pct=1.0,
        rss=100 * _MiB,
        pss=None,
        num_fds=4,
        num_threads=2,
        io_read_delta=read,
        io_write_delta=write,
    )


def _register(store, *names):
    for i, name in enumerate(names, start=1):
        store.conn.execute(
            "INSERT INTO proc_registry (proc_id, name, cmdline_hash, first_seen) "
            "VALUES (?, ?, ?, ?)",
            (i, name, f"hash{i}", _T0),
        )
    store.conn.commit()


def _io_store(tmp_path, seconds_by_session, pressure, gap=0.0, procs=None):
    """1 Hz recording: one session per entry, ``gap`` seconds of nothing between.

    ``pressure(session, i)`` says whether second ``i`` of the session is under
    I/O pressure.  A logger that did a little I/O the whole time is recorded so
    the rule has per-process counters to read.
    """
    store = make_db(tmp_path)
    _register(store, "logger", "backup", "plc-logger")
    sys_rows, proc_rows = [], []
    start = _T0
    for session, seconds in enumerate(seconds_by_session):
        for i in range(seconds):
            sys_rows.append(
                _sys(start + i, psi_io=60.0 if pressure(session, i) else 1.0)
            )
            if i % 10 == 0:
                proc_rows.append(_proc(start + i, 1, write=4096))
                proc_rows.extend((procs or (lambda ts: []))(start + i))
        start += seconds + gap
    store.buffer("sys_samples", sys_rows)
    store.buffer("proc_samples", proc_rows)
    store.flush(reason="synth")
    return store


class TestIoBottleneckDuration:
    def test_fifteen_seconds_of_pressure_is_not_fifteen_minutes(self, tmp_path):
        store = _io_store(tmp_path, [2400], lambda s, i: 1200 <= i < 1215)

        assert _run(store, "IO-BOTTLENECK-01") == []
        store.close()

    def test_a_recording_gap_is_not_pressure_time(self, tmp_path):
        store = _io_store(
            tmp_path,
            [1800, 1800],
            lambda s, i: (s == 0 and i >= 1795) or (s == 1 and i < 5),
            gap=2400,
        )

        assert _run(store, "IO-BOTTLENECK-01") == []
        store.close()

    def test_pressure_on_both_sides_of_a_gap_is_not_added_up(self, tmp_path):
        store = _io_store(
            tmp_path,
            [1200, 1200],
            lambda s, i: (s == 0 and i >= 600) or (s == 1 and i < 600),
            gap=2400,
        )

        assert _run(store, "IO-BOTTLENECK-01") == []
        store.close()

    def test_sustained_pressure_reports_its_real_length(self, tmp_path):
        store = _io_store(tmp_path, [3000], lambda s, i: 600 <= i < 1800)

        (f,) = _run(store, "IO-BOTTLENECK-01")

        assert f.evidence["duration_minutes"] == pytest.approx(20.0, abs=1.0)
        assert "20.0分間継続" in f.summary
        assert _T0 + 600 <= f.evidence["episode_start"] <= _T0 + 660
        store.close()

    def test_a_short_stall_inside_a_long_episode_does_not_end_it(self, tmp_path):
        store = _io_store(
            tmp_path,
            [3000],
            lambda s, i: 600 <= i < 1800 and not 1000 <= i < 1004,
        )

        (f,) = _run(store, "IO-BOTTLENECK-01")

        assert f.evidence["duration_minutes"] == pytest.approx(20.0, abs=1.0)
        store.close()


class TestIoBottleneckEpisodes:
    def test_the_longest_of_two_episodes_is_reported_and_blamed(self, tmp_path):
        def procs(ts):
            second = ts - _T0
            if 600 <= second < 1800:
                return [_proc(ts, 2, write=10 * _MiB)]
            if 2400 <= second < 4200:
                return [_proc(ts, 3, write=10 * _MiB)]
            return []

        store = _io_store(
            tmp_path,
            [4800],
            lambda s, i: 600 <= i < 1800 or 2400 <= i < 4200,
            procs=procs,
        )

        (f,) = _run(store, "IO-BOTTLENECK-01")

        assert f.evidence["duration_minutes"] == pytest.approx(30.0, abs=1.0)
        assert _T0 + 2340 <= f.evidence["episode_start"] <= _T0 + 2460
        names = [p["name"] for p in f.evidence["top_writers"]]
        assert names[0] == "plc-logger"
        assert "backup" not in names
        store.close()


class TestIoBottleneckCulprit:
    @staticmethod
    def _store(tmp_path):
        def procs(ts):
            hour = (ts - _T0) / 3600
            rows = []
            if 0.5 <= hour < 1.0:
                rows.append(_proc(ts, 2, read=int(60 * _GiB / 360)))
            if 1.5 <= hour < 1.5 + 1 / 3:
                rows.append(_proc(ts, 3, write=int(3 * _GiB / 120)))
            return rows

        return _io_store(
            tmp_path,
            [3 * 3600],
            lambda s, i: 1.5 * 3600 <= i < 1.5 * 3600 + 1200,
            procs=procs,
        )

    def test_the_process_that_did_io_during_the_episode_is_named(self, tmp_path):
        store = self._store(tmp_path)

        (f,) = _run(store, "IO-BOTTLENECK-01")

        names = [p["name"] for p in f.evidence["top_writers"]]
        assert names[0] == "plc-logger"
        assert "backup" not in names
        assert "特に plc-logger の動作" in f.proposal
        assert "backup" not in f.proposal
        store.close()

    def test_reads_and_writes_are_counted_and_labelled_apart(self, tmp_path):
        store = self._store(tmp_path)

        (f,) = _run(store, "IO-BOTTLENECK-01")

        top = f.evidence["top_writers"][0]
        assert top["write_mb_total"] == pytest.approx(3072, rel=0.02)
        assert top["read_mb_total"] == 0
        assert top["io_mb_total"] == pytest.approx(3072, rel=0.02)
        assert "書き込み上位" not in f.proposal
        assert "読み 0 MB / 書き 3072 MB" in f.proposal
        store.close()

    def test_a_process_that_only_read_is_not_called_a_writer(self, tmp_path):
        def procs(ts):
            if 1800 <= ts - _T0 < 3000:
                return [_proc(ts, 2, read=100 * _MiB)]
            return []

        store = _io_store(tmp_path, [4000], lambda s, i: 1800 <= i < 3000, procs=procs)

        (f,) = _run(store, "IO-BOTTLENECK-01")

        top = f.evidence["top_writers"][0]
        assert top["name"] == "backup"
        assert top["write_mb_total"] == 0
        assert top["read_mb_total"] > 0
        assert "backup (読み" in f.proposal
        store.close()


def _core_rows(ts, ncpu, pinned, *, rest=2.0):
    per_core = [100.0 if pinned else rest] + [rest] * (ncpu - 1)
    return _sys(ts, cpu=sum(per_core) / ncpu, core=max(per_core))


def _rollup_row(ts_min, ncpu, *, bg, pinned=False):
    """A 1-minute rollup row: every core at ``bg`` %, one at 100 % for the minute
    (``pinned``) or for one second of it."""
    peak = (100.0 + bg * (ncpu - 1)) / ncpu if pinned else bg + (100.0 - bg) / ncpu
    avg = peak if pinned else bg + (100.0 - bg) / ncpu / 60
    return make_row(
        "sys_rollup_1m",
        ts_min=ts_min,
        cpu_avg=avg,
        cpu_max=peak,
        cpu_p95=avg,
        cpu_max_core_max=100.0,
        mem_used_avg=2 * _GiB,
        mem_used_max=2 * _GiB,
        psi_cpu_max=0.0,
        psi_mem_max=0.0,
        psi_io_max=0.0,
        swap_out_sum=0,
    )


def _proc_rollup(ts_min, proc_id, *, avg, peak):
    return make_row(
        "proc_rollup_1m",
        ts_min=ts_min,
        proc_id=proc_id,
        cpu_avg=avg,
        cpu_max=peak,
        rss_avg=100 * _MiB,
        rss_max=100 * _MiB,
        fds_max=4,
        threads_max=2,
    )


class TestSinglecoreEpisodes:
    @staticmethod
    def _recorded(tmp_path, ncpu, seconds, pinned, *, rest=2.0, known=True):
        store = make_db(tmp_path)
        if known:
            store.write_meta({"cpu_count": str(ncpu)})
        store.buffer(
            "sys_samples",
            [_core_rows(_T0 + i, ncpu, pinned(i), rest=rest) for i in range(seconds)],
        )
        store.flush(reason="synth")
        store.rollup_incremental()
        return store

    @pytest.mark.parametrize("ncpu", [2, 4, 8])
    def test_a_core_pinned_for_half_an_hour_is_reported(self, tmp_path, ncpu):
        store = self._recorded(tmp_path, ncpu, 1800, lambda i: True)

        (f,) = _run(store, "CPU-SINGLECORE-01")

        assert f.evidence["duration_minutes"] == 30
        assert "30分継続" in f.summary
        assert f.evidence["cpu_count"] == ncpu
        assert f"CPU 数 {ncpu} 個" in f.proposal
        store.close()

    def test_the_cpu_count_is_shown_only_when_it_was_recorded(self, tmp_path):
        store = self._recorded(tmp_path, 4, 1800, lambda i: True, known=False)

        (f,) = _run(store, "CPU-SINGLECORE-01")

        assert "cpu_count" not in f.evidence
        assert "CPU 数" not in f.proposal
        store.close()

    def test_two_cpus_with_no_recorded_count_are_judged_as_strictly_as_before(
        self, tmp_path
    ):
        store = self._recorded(tmp_path, 2, 1800, lambda i: True, known=False)

        assert _run(store, "CPU-SINGLECORE-01") == []
        store.close()

    @pytest.mark.parametrize("known", [True, False])
    @pytest.mark.parametrize("ncpu, rest", [(8, 55.0), (8, 45.0), (4, 55.0)])
    def test_a_busy_host_with_one_pinned_core_is_not_a_single_core(
        self, tmp_path, ncpu, rest, known
    ):
        store = self._recorded(
            tmp_path, ncpu, 1800, lambda i: True, rest=rest, known=known
        )

        assert _run(store, "CPU-SINGLECORE-01") == []
        store.close()

    def test_every_core_busy_on_two_cpus_is_not_a_single_core(self, tmp_path):
        store = self._recorded(tmp_path, 2, 1800, lambda i: True)
        store.conn.execute("UPDATE sys_samples SET cpu_total = 92.0")
        store.conn.commit()

        assert _run(store, "CPU-SINGLECORE-01") == []
        store.close()

    def test_one_cpu_has_no_single_core_to_blame(self, tmp_path):
        store = make_db(tmp_path)
        store.write_meta({"cpu_count": "1"})
        store.buffer(
            "sys_samples",
            [_sys(_T0 + i, cpu=40.0, core=100.0) for i in range(1800)],
        )
        store.flush(reason="synth")
        store.rollup_incremental()

        assert _run(store, "CPU-SINGLECORE-01") == []
        store.close()

    @pytest.mark.parametrize("ncpu, rest", [(4, 2.0), (8, 10.0), (16, 6.0)])
    def test_one_busy_second_per_minute_is_not_thirty_minutes(
        self, tmp_path, ncpu, rest
    ):
        store = self._recorded(tmp_path, ncpu, 1800, lambda i: i % 60 == 30, rest=rest)

        assert _run(store, "CPU-SINGLECORE-01") == []
        store.close()

    @pytest.mark.parametrize("pinned", [True, False])
    @pytest.mark.parametrize("ncpu, bg", [(8, 10.0), (16, 6.0), (4, 2.0)])
    def test_minutes_only_the_rollup_covers_are_not_judged(
        self, tmp_path, ncpu, bg, pinned
    ):
        store = make_db(tmp_path)
        store.write_meta({"cpu_count": str(ncpu)})
        store.buffer(
            "sys_rollup_1m",
            [_rollup_row(_T0 + 60 * m, ncpu, bg=bg, pinned=pinned) for m in range(30)],
        )
        store.flush(reason="synth")

        diag = Engine(ProcdiagConfig()).run(store, only=["CPU-SINGLECORE-01"])

        assert diag.findings == []
        assert [u.rule_id for u in diag.uncovered_rules] == ["CPU-SINGLECORE-01"]
        store.close()

    def test_only_the_raw_part_of_a_window_is_judged(self, tmp_path):
        store = make_db(tmp_path)
        store.write_meta({"cpu_count": "4"})
        store.buffer(
            "sys_rollup_1m",
            [_rollup_row(_T0 + 60 * m, 4, bg=2.0, pinned=True) for m in range(30)],
        )
        store.buffer(
            "sys_samples",
            [_core_rows(_T0 + 600 + i, 4, True) for i in range(1200)],
        )
        store.flush(reason="synth")

        (f,) = _run(store, "CPU-SINGLECORE-01", Window(_T0, _T0 + 1800))

        assert f.evidence["duration_minutes"] == 20
        store.close()

    def test_pinned_for_most_of_each_minute_counts(self, tmp_path):
        store = self._recorded(tmp_path, 4, 1200, lambda i: i % 60 < 40)

        (f,) = _run(store, "CPU-SINGLECORE-01")

        assert f.evidence["duration_minutes"] == 20
        store.close()

    def test_the_process_busy_throughout_is_named_not_one_that_peaked_once(
        self, tmp_path
    ):
        store = self._recorded(tmp_path, 4, 1800, lambda i: True)
        _register(store, "blip", "pinned")
        rows = [_proc_rollup(_T0, 1, avg=1.7, peak=100.0)]
        rows += [_proc_rollup(_T0 + 60 * m, 2, avg=99.0, peak=99.0) for m in range(30)]
        store.buffer("proc_rollup_1m", rows)
        store.flush(reason="synth")

        (f,) = _run(store, "CPU-SINGLECORE-01")

        assert f.subject == "pinned"
        assert f.evidence["top_processes"][0] == "pinned"
        store.close()

    def test_a_recording_gap_ends_the_episode(self, tmp_path):
        store = make_db(tmp_path)
        rows = [_core_rows(_T0 + i, 4, True) for i in range(600)]
        rows += [_core_rows(_T0 + 600 + 2400 + i, 4, True) for i in range(600)]
        store.buffer("sys_samples", rows)
        store.flush(reason="synth")
        store.rollup_incremental()

        assert _run(store, "CPU-SINGLECORE-01") == []
        store.close()


class TestSaturationEpisodes:
    @staticmethod
    def _recorded(tmp_path, seconds, hot):
        store = make_db(tmp_path)
        store.buffer(
            "sys_samples",
            [_sys(_T0 + i, psi_cpu=24.0 if hot(i) else 3.0) for i in range(seconds)],
        )
        store.flush(reason="synth")
        return store

    def test_three_seconds_of_pressure_per_minute_is_not_twenty_minutes(self, tmp_path):
        store = self._recorded(tmp_path, 1200, lambda i: 30 <= i % 60 < 33)

        assert _run(store, "CPU-SATURATION-01") == []
        store.close()

    def test_sustained_pressure_reports_its_real_length(self, tmp_path):
        store = self._recorded(tmp_path, 1500, lambda i: 120 <= i < 1320)

        (f,) = _run(store, "CPU-SATURATION-01")

        assert f.evidence["duration_minutes"] == 20
        assert "20分継続" in f.summary
        store.close()

    def test_fourteen_minutes_is_not_sustained(self, tmp_path):
        store = self._recorded(tmp_path, 1500, lambda i: 120 <= i < 120 + 840)

        assert _run(store, "CPU-SATURATION-01") == []
        store.close()
