"""Memory, swap, zombie and thread rules report real problems with correct numbers.

Each test builds a synthetic recording with fixed timestamps and runs the
rule through ``Engine.run`` so the numbers are checked as the report shows them.
"""

from __future__ import annotations

import random
import socket
import time

import pytest
from synth import add_proc_series, make_db

from procdiag.config import ProcdiagConfig
from procdiag.core import Confidence
from procdiag.core.util import humanize_bytes
from procdiag.diagnosis import Engine
from procdiag.storage import make_row

_MiB = 1024**2
_GiB = 1024**3
_PAGE = 4096
_DAY = 86400.0

_T0 = 1_780_000_000.0 - (1_780_000_000.0 % 60)


def _run(store, rule_id):
    diag = Engine(ProcdiagConfig()).run(store, only=[rule_id])
    assert diag.evaluated_rules == [rule_id], (
        diag.skipped_rules,
        diag.uncovered_rules,
    )
    return [f for f in diag.findings if f.rule_id == rule_id]


def _sys_sample(ts, *, mem_used=2 * _GiB, mem_available=6 * _GiB, **kw):
    values = dict(
        ts=ts,
        cpu_total=10.0,
        cpu_max_core=12.0,
        mem_used=int(mem_used),
        mem_available=int(mem_available),
        swap_used=0,
        swap_in=0,
        swap_out=0,
        load1=0.1,
        psi_cpu_some10=0.0,
        psi_mem_some10=0.0,
        psi_mem_full10=0.0,
        psi_io_some10=0.0,
        psi_io_full10=0.0,
        disk_read_bytes=0,
        disk_write_bytes=0,
        self_cpu=0.1,
        self_rss=50 * _MiB,
    )
    values.update(kw)
    return make_row("sys_samples", **values)


def _sys_rollup(ts_min, *, psi_mem_max=0.0, swap_out_sum=0):
    return make_row(
        "sys_rollup_1m",
        ts_min=ts_min,
        cpu_avg=10.0,
        cpu_max=12.0,
        cpu_p95=11.0,
        cpu_max_core_max=12.0,
        mem_used_avg=2 * _GiB,
        mem_used_max=2 * _GiB,
        psi_cpu_max=0.0,
        psi_mem_max=psi_mem_max,
        psi_io_max=0.0,
        swap_out_sum=swap_out_sum,
    )


# ---------------------------------------------------------------------------
# MEM-LEAK-01 days to OOM
# ---------------------------------------------------------------------------


class TestDaysToOom:
    def test_projection_uses_the_memory_that_is_left(self, tmp_path):
        store = make_db(tmp_path)
        hours = 8
        total = 8 * _GiB
        rows = []
        for i in range(hours * 60):
            avail = 360 * _MiB - 20 * _MiB * i / 60
            rows.append(
                _sys_sample(_T0 + i * 60, mem_used=total - avail, mem_available=avail)
            )
        store.buffer("sys_samples", rows)
        add_proc_series(
            store,
            "leaker",
            rss_start=300 * _MiB,
            rss_slope_per_h=20 * _MiB,
            hours=hours,
            start_ts=_T0,
        )
        store.flush(reason="synth")

        (f,) = _run(store, "MEM-LEAK-01")

        left_mb = (360 - 20 * (hours * 60 - 1) / 60) * 1.0
        assert f.evidence["mem_available_mb"] == pytest.approx(left_mb, abs=1)
        assert f.evidence["days_to_oom"] == pytest.approx(
            left_mb / f.evidence["slope_mb_per_h"] / 24, rel=0.02
        )
        assert f.evidence["days_to_oom"] < 0.5
        assert "0.4日" in f.proposal
        assert "MemAvailable" in f.proposal
        store.close()

    def test_unknown_available_memory_gives_no_number(self, tmp_path):
        store = make_db(tmp_path)
        add_proc_series(
            store,
            "leaker",
            rss_start=300 * _MiB,
            rss_slope_per_h=20 * _MiB,
            hours=8,
            start_ts=_T0,
        )
        store.buffer(
            "sys_samples",
            [_sys_sample(_T0 + i * 60) for i in range(8 * 60)],
        )
        store.flush(reason="synth")
        store.conn.execute("UPDATE sys_samples SET mem_available = NULL")
        store.conn.commit()

        (f,) = _run(store, "MEM-LEAK-01")

        assert f.evidence["days_to_oom"] is None
        assert "不明" in f.proposal
        store.close()


# ---------------------------------------------------------------------------
# MEM-PRESSURE-01
# ---------------------------------------------------------------------------


@pytest.fixture
def local_tz(monkeypatch):
    def use(spec: str) -> None:
        monkeypatch.setenv("TZ", spec)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


_JST_OFFSET = 9 * 3600.0


def _local_midnight_jst() -> float:
    return (_T0 // _DAY) * _DAY - _JST_OFFSET


def _pressure_recording(store, start, minutes, *, pressure, swap_out=None):
    """One rollup row per minute; *pressure* and *swap_out* map minute -> value."""
    swap_out = swap_out or {}
    store.buffer(
        "sys_rollup_1m",
        [
            _sys_rollup(
                start + i * 60.0,
                psi_mem_max=pressure.get(i, 0.0),
                swap_out_sum=swap_out.get(i, 0),
            )
            for i in range(minutes)
        ],
    )
    store.flush(reason="synth")


def _daily(minute_of_day, length, total_minutes, last_day=99):
    return {
        d * 1440 + minute_of_day + k
        for d in range(last_day + 1)
        for k in range(length)
        if d * 1440 + minute_of_day + k < total_minutes
    }


class TestMemPressure:
    def test_one_pressure_minute_and_a_stray_swap_out_is_not_chronic(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start = _local_midnight_jst() + 6 * 3600
        _pressure_recording(
            store,
            start,
            25 * 60,
            pressure={300: 12.0},
            swap_out={900: _PAGE},
        )
        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_short_slivers_of_a_day_do_not_make_a_high_confidence_recurrence(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start = _local_midnight_jst() - 30 * 60
        bursts = {15, 15 + 12 * 60 + 15, 15 + 24 * 60 + 30}
        _pressure_recording(
            store,
            start,
            25 * 60,
            pressure={m: 12.0 for m in bursts},
            swap_out={m: _MiB for m in bursts},
        )
        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_sustained_pressure_with_swap_out_on_most_days_fires(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start = _local_midnight_jst()
        minutes = 72 * 60
        pressure = {m: 9.0 for m in _daily(14 * 60, 30, minutes)}
        _pressure_recording(
            store,
            start,
            minutes,
            pressure=pressure,
            swap_out={m: 8 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_total_days"] == 3
        assert f.evidence["n_pressure_days"] == 3
        assert f.evidence["swap_out_total_bytes"] == 3 * 30 * 8 * _MiB
        assert f.confidence == Confidence.HIGH
        assert "3/3" in f.summary
        assert humanize_bytes(3 * 30 * 8 * _MiB) in f.summary
        store.close()

    def test_edge_slivers_are_not_counted_as_observed_days(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start = _local_midnight_jst() - 30 * 60
        minutes = 49 * 60
        pressure = {m: 9.0 for m in range(30 + 14 * 60, 30 + 14 * 60 + 30)}
        pressure.update(
            {m: 9.0 for m in range(30 + 24 * 60 + 14 * 60, 30 + 24 * 60 + 14 * 60 + 30)}
        )
        _pressure_recording(
            store,
            start,
            minutes,
            pressure=pressure,
            swap_out={m: 8 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_total_days"] == 2
        assert f.evidence["n_pressure_days"] == 2
        assert f.confidence == Confidence.MEDIUM
        store.close()

    def test_swap_out_far_from_the_pressure_does_not_count(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start = _local_midnight_jst()
        minutes = 72 * 60
        pressure = {m: 9.0 for m in _daily(14 * 60, 30, minutes)}
        swap = {d * 1440 + 3 * 60: 64 * _MiB for d in range(3)}
        _pressure_recording(store, start, minutes, pressure=pressure, swap_out=swap)
        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_pressure_on_only_one_of_three_days_is_not_recurring(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start = _local_midnight_jst()
        minutes = 72 * 60
        pressure = {m: 9.0 for m in _daily(14 * 60, 30, minutes, last_day=0)}
        _pressure_recording(
            store,
            start,
            minutes,
            pressure=pressure,
            swap_out={m: 8 * _MiB for m in pressure},
        )
        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()


# ---------------------------------------------------------------------------
# SWAP-THRASH-01
# ---------------------------------------------------------------------------


class TestSwapThrashNumbers:
    def _isolated_events(self, tmp_path, gap_minutes):
        store = make_db(tmp_path)
        events = {60, 60 + gap_minutes}
        store.buffer(
            "sys_samples",
            [
                _sys_sample(
                    _T0 + i * 60.0,
                    swap_in=2 * _PAGE if i in events else 0,
                    swap_out=2 * _PAGE if i in events else 0,
                    psi_mem_full10=0.5 if i in events else 0.0,
                )
                for i in range(22 * 60)
            ],
        )
        store.flush(reason="synth")
        return store

    def test_totals_are_bytes_not_pages(self, tmp_path):
        store = self._isolated_events(tmp_path, 20 * 60)
        (f,) = _run(store, "SWAP-THRASH-01")
        assert f.evidence["swap_in_total_bytes"] == 4 * _PAGE
        assert f.evidence["swap_out_total_bytes"] == 4 * _PAGE
        assert "swap_in_total_pages" not in f.evidence
        assert "ページ" not in f.proposal
        assert humanize_bytes(4 * _PAGE) in f.proposal
        store.close()

    def test_isolated_events_far_apart_are_separate_episodes(self, tmp_path):
        store = self._isolated_events(tmp_path, 20 * 60)
        (f,) = _run(store, "SWAP-THRASH-01")
        assert f.evidence["thrash_event_count"] == 2
        assert f.evidence["episode_count"] == 2
        assert f.evidence["duration_minutes"] < 10
        assert "1201" not in f.summary
        assert "1201" not in f.proposal
        store.close()

    def test_consecutive_samples_are_one_episode(self, tmp_path):
        store = self._isolated_events(tmp_path, 1)
        (f,) = _run(store, "SWAP-THRASH-01")
        assert f.evidence["episode_count"] == 1
        assert f.evidence["duration_minutes"] == 2
        store.close()


# ---------------------------------------------------------------------------
# ZOMBIE-01
# ---------------------------------------------------------------------------

_SCAN = 30.0
_ZOMBIE_HOURS = 7


def _zombie_scans(store, counts):
    app = store.proc_id_for(
        name="app", cmdline_hash="h_app", cmdline_sample="/usr/bin/app", ts=_T0
    )
    store.conn.commit()
    store.buffer(
        "proc_counts",
        [
            make_row("proc_counts", ts=_T0 + i * _SCAN, total=120, zombies=z)
            for i, z in enumerate(counts)
        ],
    )
    store.buffer(
        "proc_samples",
        [_app_sample(_T0 + i * _SCAN, app) for i in range(len(counts))],
    )
    _sys_minutes(store, len(counts) * _SCAN / 60)
    store.flush(reason="synth")


class TestZombieNoise:
    def test_one_transient_zombie_is_not_an_increasing_trend(self, tmp_path):
        store = make_db(tmp_path)
        n = int(_ZOMBIE_HOURS * 3600 / _SCAN)
        _zombie_scans(store, [1 if i == int(5 * 3600 / _SCAN) else 0 for i in range(n)])
        assert _run(store, "ZOMBIE-01") == []
        store.close()

    def test_scattered_transient_zombies_never_alarm(self, tmp_path):
        n = int(_ZOMBIE_HOURS * 3600 / _SCAN)
        fired = 0
        for seed in range(20):
            rng = random.Random(seed)
            sub = tmp_path / f"seed{seed}"
            sub.mkdir()
            store = make_db(sub)
            _zombie_scans(store, [1 if rng.random() < 1 / 119 else 0 for _ in range(n)])
            fired += len(_run(store, "ZOMBIE-01"))
            store.close()
        assert fired == 0

    def test_zombies_that_were_reaped_are_not_reported(self, tmp_path):
        store = make_db(tmp_path)
        n = int(_ZOMBIE_HOURS * 3600 / _SCAN)
        ramp = int(5 * 3600 / _SCAN)
        _zombie_scans(
            store, [min(12, i * 12 // ramp) if i < ramp else 0 for i in range(n)]
        )
        assert _run(store, "ZOMBIE-01") == []
        store.close()

    def test_real_accumulation_still_fires(self, tmp_path):
        store = make_db(tmp_path)
        rng = random.Random(7)
        n = int(_ZOMBIE_HOURS * 3600 / _SCAN)
        per_hour = 3
        counts = [
            int(per_hour * i * _SCAN / 3600) + (1 if rng.random() < 0.05 else 0)
            for i in range(n)
        ]
        _zombie_scans(store, counts)
        (f,) = _run(store, "ZOMBIE-01")
        assert f.evidence["method"] == "status"
        assert f.evidence["slope_per_h"] == pytest.approx(per_hour, rel=0.2)
        assert f.evidence["current_zombies"] >= 15
        store.close()


def _sys_minutes(store, minutes):
    store.buffer(
        "sys_samples", [_sys_sample(_T0 + i * 60.0) for i in range(int(minutes))]
    )


def _app_sample(ts, proc_id):
    return make_row(
        "proc_samples",
        ts=ts,
        proc_id=proc_id,
        pid=100,
        cpu_pct=1.0,
        rss=50 * _MiB,
        pss=None,
        num_threads=4,
        num_fds=None,
        io_read_delta=0,
        io_write_delta=0,
    )


def _as_pre_v4(store):
    store.conn.execute("DROP TABLE proc_counts")
    store.conn.commit()


def _pre_v4_recording(store, zombies_at):
    app = store.proc_id_for(
        name="app", cmdline_hash="h_app", cmdline_sample="/usr/bin/app", ts=_T0
    )
    zed = store.proc_id_for(
        name="worker", cmdline_hash="h_worker", cmdline_sample="", ts=_T0
    )
    store.conn.commit()
    rows = []
    n = int(_ZOMBIE_HOURS * 3600 / _SCAN)
    for i in range(n):
        ts = _T0 + i * _SCAN
        rows.append(_app_sample(ts, app))
        for j in range(zombies_at(i * _SCAN / 3600)):
            rows.append(
                make_row(
                    "proc_samples",
                    ts=ts,
                    proc_id=zed,
                    pid=20000 + j,
                    cpu_pct=0.0,
                    rss=0,
                    pss=None,
                    num_threads=1,
                    num_fds=None,
                    io_read_delta=None,
                    io_write_delta=None,
                )
            )
    store.buffer("proc_samples", rows)
    _sys_minutes(store, n * _SCAN / 60)
    store.flush(reason="synth")


class TestZombieApproximation:
    def test_real_zombies_with_one_thread_are_counted(self, tmp_path):
        store = make_db(tmp_path)
        _pre_v4_recording(store, lambda hours: int(5 * hours))
        _as_pre_v4(store)

        (f,) = _run(store, "ZOMBIE-01")

        assert f.evidence["method"] == "approx"
        assert f.confidence == Confidence.LOW
        assert f.evidence["slope_per_h"] == pytest.approx(5, rel=0.2)
        store.close()

    def test_a_constant_background_of_zero_rss_processes_is_silent(self, tmp_path):
        store = make_db(tmp_path)
        _pre_v4_recording(store, lambda hours: 20)
        _as_pre_v4(store)
        assert _run(store, "ZOMBIE-01") == []
        store.close()


# ---------------------------------------------------------------------------
# THREAD-LEAK-01
# ---------------------------------------------------------------------------


def _thread_leak_store(tmp_path, *, threads_slope=40):
    store = make_db(tmp_path)
    for filler in ("sshd", "cron", "agent-a"):
        store.proc_id_for(
            name=filler, cmdline_hash=f"h_{filler}", cmdline_sample="", ts=_T0
        )
    store.conn.commit()
    proc_id = add_proc_series(
        store,
        "edge-agent",
        rss_start=100 * _MiB,
        rss_slope_per_h=0,
        hours=8,
        threads_slope=threads_slope,
        start_ts=_T0,
    )
    store.buffer("sys_samples", [_sys_sample(_T0 + i * 60) for i in range(8 * 60)])
    store.flush(reason="synth")
    return store, proc_id


class TestThreadLeak:
    def test_proposal_does_not_point_at_the_registry_id(self, tmp_path):
        store, proc_id = _thread_leak_store(tmp_path)
        assert proc_id >= 4
        (f,) = _run(store, "THREAD-LEAK-01")
        assert f"/proc/{proc_id}/" not in f.proposal
        assert "pgrep -a edge-agent" in f.proposal
        assert "/proc/<PID>/status" in f.proposal
        store.close()

    def test_limit_is_read_from_this_host(self, tmp_path, monkeypatch):
        limit_file = tmp_path / "threads-max"
        limit_file.write_text("171913\n")
        monkeypatch.setattr("procdiag.diagnosis.leak._THREADS_MAX_PATH", limit_file)
        monkeypatch.setattr(socket, "gethostname", lambda: "synth-host")
        store, _ = _thread_leak_store(tmp_path)

        (f,) = _run(store, "THREAD-LEAK-01")

        slope = f.evidence["slope_threads_per_h"]
        current = f.evidence["current_threads"]
        assert f.evidence["thread_max_estimate"] == 171913
        assert f.evidence["thread_max_source"] == "this_host"
        assert f.evidence["days_to_thread_max"] == pytest.approx(
            (171913 - current) / slope / 24, rel=1e-6
        )
        assert "171913" in f.proposal
        assert "32768" not in f.proposal
        store.close()

    def test_limit_of_another_host_is_not_guessed(self, tmp_path, monkeypatch):
        limit_file = tmp_path / "threads-max"
        limit_file.write_text("171913\n")
        monkeypatch.setattr("procdiag.diagnosis.leak._THREADS_MAX_PATH", limit_file)
        monkeypatch.setattr(socket, "gethostname", lambda: "analyst-laptop")
        store, _ = _thread_leak_store(tmp_path)

        (f,) = _run(store, "THREAD-LEAK-01")

        assert f.evidence["thread_max_estimate"] is None
        assert f.evidence["thread_max_source"] == "unknown"
        assert f.evidence["days_to_thread_max"] is None
        assert "32768" not in f.proposal
        assert "171913" not in f.proposal
        assert "cat /proc/sys/kernel/threads-max" in f.proposal
        store.close()
