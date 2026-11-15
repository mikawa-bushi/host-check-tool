"""Memory, swap, zombie and thread rules report real problems with correct numbers.

Each test builds a synthetic recording with fixed timestamps and runs the
rule through ``Engine.run`` so the numbers are checked as the report shows them.
"""

from __future__ import annotations

import random
import socket
import time
from datetime import datetime

import pytest
from synth import add_proc_series, make_db
from test_compare import _compare

from procdiag.compare import Unknown, Verdict, Why
from procdiag.config import ProcdiagConfig
from procdiag.core import Confidence
from procdiag.core.util import humanize_bytes
from procdiag.diagnosis import Engine
from procdiag.diagnosis.trend import fit_linear
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


def _windows(start_minute, minutes, windows):
    """Recording minute -> pressure for ``(day, minute_of_day, length)`` windows.

    *start_minute* is where the recording starts, in minutes after the local
    midnight of day 0.
    """
    pressure = {}
    for day, minute_of_day, length in windows:
        first = day * 1440 + minute_of_day - start_minute
        for i in range(first, first + length):
            if 0 <= i < minutes:
                pressure[i] = 9.0
    return pressure


def _nights(first_day=-1, last_day=9):
    return [(d, 2 * 60, 2 * 60) for d in range(first_day, last_day + 1)]


def _across_midnight(before, after, first_day=-1, last_day=9):
    """Every night, a run of ``before`` minutes up to midnight and ``after`` past it."""
    return [
        (d, 24 * 60 - before, before + after) for d in range(first_day, last_day + 1)
    ]


def _local_date(days_after_day0):
    return datetime.fromtimestamp(
        _local_midnight_jst() + days_after_day0 * _DAY + 12 * 3600
    ).strftime("%Y-%m-%d")


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

    def test_scattered_one_minute_blips_are_not_sustained(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        pressure = {d * 1440 + h * 60: 9.0 for d in range(3) for h in range(0, 24, 2)}
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure=pressure,
            swap_out={m: 8 * _MiB for m in pressure},
        )
        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    @pytest.mark.parametrize("length, fires", [(9, False), (10, True)])
    def test_run_must_last_ten_minutes(self, tmp_path, local_tz, length, fires):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        pressure = {m: 9.0 for m in _daily(14 * 60, length, minutes)}
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure=pressure,
            swap_out={m: 8 * _MiB for m in pressure},
        )
        assert bool(_run(store, "MEM-PRESSURE-01")) is fires
        store.close()

    def test_a_dip_inside_the_pressure_ends_the_run(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        pressure = {
            d * 1440 + 14 * 60 + k: 9.0
            for d in range(3)
            for k in list(range(0, 6)) + list(range(7, 13))
        }
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure=pressure,
            swap_out={m: 8 * _MiB for m in pressure},
        )
        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    @pytest.mark.parametrize(
        "offset, fires", [(-2, False), (-1, True), (30, True), (31, False)]
    )
    def test_swap_out_counts_within_a_minute_of_the_run(
        self, tmp_path, local_tz, offset, fires
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        pressure = {m: 9.0 for m in _daily(14 * 60, 30, minutes)}
        swap = {d * 1440 + 14 * 60 + offset: 8 * _MiB for d in range(3)}
        _pressure_recording(
            store, _local_midnight_jst(), minutes, pressure=pressure, swap_out=swap
        )
        assert bool(_run(store, "MEM-PRESSURE-01")) is fires
        store.close()

    @pytest.mark.parametrize("swap_out, fires", [(_MiB - 1, False), (_MiB, True)])
    def test_swap_out_must_reach_a_mebibyte_a_day(
        self, tmp_path, local_tz, swap_out, fires
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        pressure = {m: 9.0 for m in _daily(14 * 60, 30, minutes)}
        swap = {d * 1440 + 14 * 60 + 5: swap_out for d in range(3)}
        _pressure_recording(
            store, _local_midnight_jst(), minutes, pressure=pressure, swap_out=swap
        )
        assert bool(_run(store, "MEM-PRESSURE-01")) is fires
        store.close()

    @pytest.mark.parametrize(
        "swap_out, fires", [(_MiB // 2 - 1, False), (_MiB // 2, True)]
    )
    def test_swap_out_around_several_runs_in_a_day_is_added_up(
        self, tmp_path, local_tz, swap_out, fires
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        runs = [(d, hour * 60, 12) for d in range(3) for hour in (14, 17)]
        pressure = _windows(0, minutes, runs)
        swap = {
            d * 1440 + hour * 60 + 5: swap_out for d in range(3) for hour in (14, 17)
        }
        _pressure_recording(
            store, _local_midnight_jst(), minutes, pressure=pressure, swap_out=swap
        )
        assert bool(_run(store, "MEM-PRESSURE-01")) is fires
        store.close()

    def test_swap_out_between_two_close_runs_is_counted_once(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        runs = [(d, 14 * 60 + start, 10) for d in range(3) for start in (0, 11)]
        pressure = _windows(0, minutes, runs)
        swap = {d * 1440 + 14 * 60 + 10: 2 * _MiB for d in range(3)}
        _pressure_recording(
            store, _local_midnight_jst(), minutes, pressure=pressure, swap_out=swap
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["swap_out_total_bytes"] == 3 * 2 * _MiB
        assert f.evidence["pressure_event_count"] == 3 * 20
        store.close()

    @pytest.mark.parametrize(
        "days, observed, fires",
        [
            ((0, 1), 4, False),
            ((0, 1, 2), 4, True),
            ((0, 1), 3, True),
            ((0, 1, 2), 5, True),
            ((0, 1, 2, 3), 7, False),
        ],
    )
    def test_pressure_days_must_be_most_of_the_observed_days(
        self, tmp_path, local_tz, days, observed, fires
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = observed * 24 * 60
        pressure = {d * 1440 + 14 * 60 + k: 9.0 for d in days for k in range(30)}
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure=pressure,
            swap_out={m: 8 * _MiB for m in pressure},
        )
        found = _run(store, "MEM-PRESSURE-01")
        assert bool(found) is fires
        if found:
            assert found[0].evidence["n_pressure_days"] == len(days)
            assert found[0].evidence["n_total_days"] == observed
        store.close()


class TestMemPressureThresholds:
    @pytest.mark.parametrize("psi, fires", [(5.0, False), (5.1, True)])
    def test_a_pressure_minute_is_psi_above_five(self, tmp_path, local_tz, psi, fires):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        pressure = {m: psi for m in _daily(14 * 60, 30, minutes)}
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure=pressure,
            swap_out={m: 8 * _MiB for m in pressure},
        )
        assert bool(_run(store, "MEM-PRESSURE-01")) is fires
        store.close()

    @pytest.mark.parametrize(
        "start_minute, observed_days", [(18 * 60, 3), (18 * 60 + 1, 2)]
    )
    def test_an_edge_day_needs_six_hours_of_psi_rows_to_count(
        self, tmp_path, local_tz, start_minute, observed_days
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = (1440 - start_minute) + 2 * 1440
        pressure = _windows(start_minute, minutes, [(1, 14 * 60, 30), (2, 14 * 60, 30)])
        _pressure_recording(
            store,
            _local_midnight_jst() + start_minute * 60,
            minutes,
            pressure=pressure,
            swap_out={m: 8 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_total_days"] == observed_days
        assert f.evidence["n_pressure_days"] == 2
        store.close()


class TestMemPressureSliverDays:
    """A day with under six hours of PSI rows is a day only when it holds the pressure."""

    @pytest.mark.parametrize("start_minute", range(0, 1440, 30))
    def test_nightly_pressure_is_found_wherever_a_two_day_recording_starts(
        self, tmp_path, local_tz, start_minute
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 48 * 60
        pressure = _windows(start_minute, minutes, _nights())
        _pressure_recording(
            store,
            _local_midnight_jst() + start_minute * 60,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        found = _run(store, "MEM-PRESSURE-01")

        clock = f"{start_minute // 60:02d}:{start_minute % 60:02d}"
        assert len(found) == 1, f"recording from {clock} was judged healthy"
        assert found[0].evidence["n_pressure_days"] >= 2
        store.close()

    def test_the_sliver_that_holds_a_night_is_a_pressure_day(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start_minute = 4 * 60
        minutes = 48 * 60
        pressure = _windows(start_minute, minutes, _nights())
        _pressure_recording(
            store,
            _local_midnight_jst() + start_minute * 60,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_total_days"] == 3
        assert f.evidence["n_pressure_days"] == 2
        assert f.confidence == Confidence.MEDIUM
        store.close()

    @pytest.mark.parametrize(
        "run, swap_out, counted",
        [(10, 8 * _MiB, True), (9, 8 * _MiB, False), (10, 0, False)],
    )
    def test_a_sliver_counts_only_for_a_sustained_run_with_swap_out(
        self, tmp_path, local_tz, run, swap_out, counted
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start_minute = 20 * 60
        minutes = 55 * 60
        full_days = _windows(
            start_minute, minutes, [(1, 14 * 60, 30), (2, 14 * 60, 30)]
        )
        sliver = _windows(start_minute, minutes, [(3, 60, run)])
        _pressure_recording(
            store,
            _local_midnight_jst() + start_minute * 60,
            minutes,
            pressure={**full_days, **sliver},
            swap_out={
                **{m: 8 * _MiB for m in full_days},
                **{m: swap_out for m in sliver},
            },
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        expected = 3 if counted else 2
        assert f.evidence["n_total_days"] == expected
        assert f.evidence["n_pressure_days"] == expected
        store.close()

    @pytest.mark.parametrize(
        "windows",
        [_nights(), _across_midnight(5, 15)],
        ids=["02:00-04:00", "23:55-00:15"],
    )
    @pytest.mark.parametrize("start_minute", range(0, 1440, 30))
    def test_a_day_short_of_observation_is_never_a_promise_that_cannot_be_kept(
        self, tmp_path, local_tz, start_minute, windows
    ):
        local_tz("JST-9")
        engine = Engine(ProcdiagConfig())
        start = _local_midnight_jst() + start_minute * 60

        def recording(path, minutes):
            path.mkdir()
            store = make_db(path)
            pressure = _windows(start_minute, minutes, windows)
            _pressure_recording(
                store,
                start,
                minutes,
                pressure=pressure,
                swap_out={m: 2 * _MiB for m in pressure},
            )
            return store

        store = recording(tmp_path / "short", 24 * 60)
        diag = engine.run(store, only=["MEM-PRESSURE-01"])
        store.close()
        if not diag.skipped_rules:
            assert diag.evaluated_rules == ["MEM-PRESSURE-01"]
            return
        (skipped,) = diag.skipped_rules

        longer = int(24 * 60 + skipped.remaining_hours * 60) + 1
        store = recording(tmp_path / "longer", longer)
        again = engine.run(store, only=["MEM-PRESSURE-01"])
        store.close()
        assert again.evaluated_rules == ["MEM-PRESSURE-01"]


class TestMemPressureEpisodes:
    """One sustained run is one episode, wherever local midnight cuts it."""

    @pytest.mark.parametrize("hours", [24, 48])
    @pytest.mark.parametrize(
        "before, after, swap_everywhere",
        [
            (10, 10, False),
            (15, 15, False),
            (5, 15, False),
            (9, 9, False),
            (120, 120, True),
        ],
        ids=["20min", "30min", "5+15", "9+9", "4h"],
    )
    def test_a_one_off_run_across_midnight_is_not_chronic(
        self, tmp_path, local_tz, before, after, swap_everywhere, hours
    ):
        local_tz("JST-9")
        minutes = hours * 60
        engine = Engine(ProcdiagConfig())
        reported = []
        for start_minute in range(0, 1440, 90):
            path = tmp_path / str(start_minute)
            path.mkdir()
            store = make_db(path)
            pressure = _windows(
                start_minute, minutes, [(0, 24 * 60 - before, before + after)]
            )
            swap_out = (
                {m: 2 * _MiB for m in pressure}
                if swap_everywhere
                else {24 * 60 - start_minute: 2 * _MiB}
            )
            _pressure_recording(
                store,
                _local_midnight_jst() + start_minute * 60,
                minutes,
                pressure=pressure,
                swap_out=swap_out,
            )
            diag = engine.run(store, only=["MEM-PRESSURE-01"])
            store.close()
            if diag.findings:
                reported.append(f"{start_minute // 60:02d}:{start_minute % 60:02d}")

        assert (
            reported == []
        ), f"one run was called chronic when recording from {reported}"

    @pytest.mark.parametrize(
        "before, after, first_day",
        [(5, 15, 1), (9, 9, 0), (15, 15, 0)],
        ids=["5+15", "9+9", "15+15"],
    )
    def test_a_run_across_midnight_is_one_pressure_day_the_one_with_most_of_it(
        self, tmp_path, local_tz, before, after, first_day
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start_minute = 12 * 60
        minutes = 72 * 60
        pressure = _windows(start_minute, minutes, _across_midnight(before, after))
        _pressure_recording(
            store,
            _local_midnight_jst() + start_minute * 60,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        runs = 3
        assert f.evidence["pressure_days"] == [
            _local_date(d) for d in range(first_day, first_day + runs)
        ]
        assert f.evidence["n_total_days"] == 4
        assert f.evidence["pressure_event_count"] == runs * (before + after)
        assert f.evidence["swap_out_total_bytes"] == runs * (before + after) * 2 * _MiB
        store.close()

    @pytest.mark.parametrize(
        "before, after", [(15, 15), (9, 9), (5, 15), (10, 10)], ids=lambda v: str(v)
    )
    def test_a_run_across_midnight_every_night_is_found(
        self, tmp_path, local_tz, before, after
    ):
        local_tz("JST-9")
        minutes = 72 * 60
        engine = Engine(ProcdiagConfig())
        missed = []
        for start_minute in range(0, 1440, 90):
            path = tmp_path / str(start_minute)
            path.mkdir()
            store = make_db(path)
            pressure = _windows(start_minute, minutes, _across_midnight(before, after))
            _pressure_recording(
                store,
                _local_midnight_jst() + start_minute * 60,
                minutes,
                pressure=pressure,
                swap_out={m: 2 * _MiB for m in pressure},
            )
            diag = engine.run(store, only=["MEM-PRESSURE-01"])
            store.close()
            if not diag.findings:
                missed.append(f"{start_minute // 60:02d}:{start_minute % 60:02d}")

        assert missed == [], f"nightly pressure was missed when recording from {missed}"

    def test_one_day_of_unbroken_pressure_makes_two_pressure_days_counted_once(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 24 * 60
        pressure = {m: 20.0 for m in range(minutes)}
        _pressure_recording(
            store,
            _local_midnight_jst() + 14 * 3600,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_total_days"] == 2
        assert f.evidence["n_pressure_days"] == 2
        assert f.evidence["pressure_event_count"] == minutes
        assert f.evidence["swap_out_total_bytes"] == minutes * 2 * _MiB
        store.close()

    def test_one_day_of_unbroken_pressure_from_the_evening_is_one_pressure_day(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 24 * 60
        pressure = {m: 20.0 for m in range(minutes)}
        _pressure_recording(
            store,
            _local_midnight_jst() + 20 * 3600,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        diag = Engine(ProcdiagConfig()).run(store, only=["MEM-PRESSURE-01"])

        assert diag.findings == []
        store.close()

    @pytest.mark.parametrize(
        "shift, fires",
        [(-1, False), (0, True), (1, False)],
        ids=["361+359", "360+360", "359+361"],
    )
    def test_a_long_run_makes_a_second_pressure_day_only_with_six_hours_in_it(
        self, tmp_path, local_tz, shift, fires
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 4 * 24 * 60
        pressure = _windows(
            0, minutes, [(1, 18 * 60 + shift, 12 * 60), (3, 14 * 60, 30)]
        )
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        found = _run(store, "MEM-PRESSURE-01")

        assert bool(found) is fires
        if found:
            assert found[0].evidence["pressure_days"] == [
                _local_date(1),
                _local_date(2),
                _local_date(3),
            ]
        store.close()


class TestMemPressureNeedsObservedDays:
    def _all_day_pressure(self, tmp_path, start, hours, quiet_hours=0):
        tmp_path.mkdir(exist_ok=True)
        store = make_db(tmp_path)
        minutes = int(hours * 60)
        pressure = {m: 20.0 for m in range(quiet_hours * 60, minutes)}
        _pressure_recording(
            store,
            start,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )
        return store

    def test_one_observed_day_is_not_evaluable_not_healthy(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = self._all_day_pressure(
            tmp_path, _local_midnight_jst() + 20 * 3600, 24, quiet_hours=4
        )

        diag = Engine(ProcdiagConfig()).run(store, only=["MEM-PRESSURE-01"])

        assert diag.evaluated_rules == []
        (skipped,) = diag.skipped_rules
        assert skipped.rule_id == "MEM-PRESSURE-01"
        assert skipped.remaining_hours == pytest.approx(10.0, abs=0.05)
        assert skipped.required_hours == pytest.approx(
            diag.quality.span_hours + 10.0, abs=0.1
        )
        store.close()

    def test_pressure_in_the_edge_sliver_is_enough_to_evaluate(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 24 * 60
        pressure = _windows(20 * 60, minutes, [(0, 21 * 60, 30), (1, 14 * 60, 30)])
        _pressure_recording(
            store,
            _local_midnight_jst() + 20 * 3600,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_total_days"] == 2
        assert f.evidence["n_pressure_days"] == 2
        store.close()

    def test_two_observed_days_are_evaluated(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = self._all_day_pressure(tmp_path, _local_midnight_jst() + 14 * 3600, 24)
        (f,) = _run(store, "MEM-PRESSURE-01")
        assert f.evidence["n_total_days"] == 2
        assert f.evidence["n_pressure_days"] == 2
        store.close()

    def test_compare_does_not_call_it_resolved(self, tmp_path, local_tz):
        local_tz("JST-9")
        before_store = self._all_day_pressure(
            tmp_path / "before", _local_midnight_jst(), 72
        )
        after_store = self._all_day_pressure(
            tmp_path / "after", _local_midnight_jst() + 20 * 3600, 24
        )
        after_store.conn.execute("UPDATE sys_rollup_1m SET psi_mem_max = 0.5")
        after_store.conn.commit()
        engine = Engine(ProcdiagConfig())
        before = engine.run(before_store, only=["MEM-PRESSURE-01"])
        after = engine.run(after_store, only=["MEM-PRESSURE-01"])
        assert before.findings and after.skipped_rules

        (row,) = _compare(before, after).rows

        assert row.verdict is Verdict.NOT_EVALUABLE
        assert isinstance(row.after, Unknown) and row.after.why is Why.TOO_SHORT
        assert "あと" in row.after.detail
        before_store.close()
        after_store.close()


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
        (basis,) = [
            line for line in f.proposal.splitlines() if line.startswith("[根拠]")
        ]
        assert "ページ" not in basis
        assert humanize_bytes(4 * _PAGE) in basis
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
    _sys_minutes(store, len(counts) * _SCAN / 60 + 2)
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

    @pytest.mark.parametrize(
        "height, amp, r2_band, fires",
        [(9, 2, (0.55, 0.67), False), (7, 1, (0.75, 0.85), True)],
    )
    def test_the_line_must_explain_the_counts(
        self, tmp_path, height, amp, r2_band, fires
    ):
        n = int(_ZOMBIE_HOURS * 3600 / _SCAN)
        counts = [
            round(height * i / n) + amp + (amp if (i // 120) % 2 else -amp)
            for i in range(n)
        ]
        fit = fit_linear(
            [(i * _SCAN, float(z)) for i, z in enumerate(counts)],
            min_points=12,
            min_span_hours=6.0,
        )
        assert r2_band[0] < fit.r2 < r2_band[1] and fit.slope_per_hour > 0.5
        store = make_db(tmp_path)
        _zombie_scans(store, counts)
        assert bool(_run(store, "ZOMBIE-01")) is fires
        store.close()

    @pytest.mark.parametrize("last_scan, fires", [(0, False), (9, True)])
    def test_zombies_must_still_be_there_at_the_end(self, tmp_path, last_scan, fires):
        n = int(_ZOMBIE_HOURS * 3600 / _SCAN)
        counts = [int(9 * i / n) for i in range(n - 1)] + [last_scan]
        store = make_db(tmp_path)
        _zombie_scans(store, counts)
        found = _run(store, "ZOMBIE-01")
        assert bool(found) is fires
        if found:
            assert found[0].evidence["current_zombies"] == last_scan
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
        assert "pgrep -a -- edge-agent" in f.proposal
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

    def test_missing_hostname_is_worded_as_unknown(self, tmp_path, monkeypatch):
        limit_file = tmp_path / "threads-max"
        limit_file.write_text("171913\n")
        monkeypatch.setattr("procdiag.diagnosis.leak._THREADS_MAX_PATH", limit_file)
        store, _ = _thread_leak_store(tmp_path)
        store.conn.execute("DELETE FROM meta WHERE key = 'hostname'")
        store.conn.commit()

        (f,) = _run(store, "THREAD-LEAK-01")

        assert f.evidence["thread_max_source"] == "unknown"
        assert "ホスト名がない" in f.proposal
        assert "記録したホスト以外でチェックしている" not in f.proposal
        store.close()
