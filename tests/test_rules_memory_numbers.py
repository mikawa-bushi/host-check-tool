"""Memory, swap, zombie and thread rules report real problems with correct numbers.

Each test builds a synthetic recording with fixed timestamps and runs the
rule through ``Engine.run`` so the numbers are checked as the report shows them.
"""

from __future__ import annotations

import random
import re
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


def _pressure_recording(
    store, start, minutes, *, pressure, swap_out=None, missing=frozenset()
):
    """One rollup row per minute; *pressure* and *swap_out* map minute -> value.

    The minutes in *missing* have no row.
    """
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
            if i not in missing
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
        "before, after",
        [(5, 15), (9, 9), (15, 15), (15, 5)],
        ids=["5+15", "9+9", "15+15", "15+5"],
    )
    def test_each_night_across_midnight_is_one_pressure_day_the_first_free_one(
        self, tmp_path, local_tz, before, after
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
        assert f.evidence["pressure_days"] == [_local_date(d) for d in range(runs)]
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

    @pytest.mark.parametrize("start_hour", [14, 20])
    def test_one_day_of_unbroken_pressure_makes_two_pressure_days_counted_once(
        self, tmp_path, local_tz, start_hour
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 24 * 60
        pressure = {m: 20.0 for m in range(minutes)}
        _pressure_recording(
            store,
            _local_midnight_jst() + start_hour * 3600,
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

    @pytest.mark.parametrize(
        "shift, fires",
        [(-1, False), (0, True), (1, True)],
        ids=["361+359", "360+360", "359+361"],
    )
    def test_a_long_run_makes_a_later_day_a_pressure_day_only_with_six_hours_in_it(
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


class TestMemPressureOccurrences:
    """Runs less than six hours apart are one occurrence, and each makes one day."""

    @pytest.mark.parametrize(
        "nights, first_day",
        [
            ([(0, 23 * 60 + 58, 20), (1, 23 * 60 + 40, 21)], 0),
            ([(1, 5, 20), (1, 23 * 60 + 55, 20)], 1),
        ],
        ids=["late-then-early", "after-midnight-then-across"],
    )
    def test_two_nights_across_midnight_are_two_days_however_their_minutes_fall(
        self, tmp_path, local_tz, nights, first_day
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start_minute = 12 * 60
        minutes = 48 * 60
        pressure = _windows(start_minute, minutes, nights)
        _pressure_recording(
            store,
            _local_midnight_jst() + start_minute * 60,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["pressure_days"] == [
            _local_date(first_day),
            _local_date(first_day + 1),
        ]
        assert f.evidence["n_total_days"] == 3
        store.close()

    def test_one_extra_pressure_minute_does_not_merge_two_nights(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start_minute = 12 * 60
        minutes = 72 * 60
        pressure = _windows(start_minute, minutes, _across_midnight(15, 15))
        pressure.update(_windows(start_minute, minutes, [(1, 15, 1)]))
        _pressure_recording(
            store,
            _local_midnight_jst() + start_minute * 60,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_pressure_days"] == 3
        assert f.evidence["n_total_days"] == 4
        store.close()

    @pytest.mark.parametrize("start_hour", range(0, 24, 2))
    def test_nights_across_midnight_stay_one_day_each_when_the_clocks_go_back(
        self, tmp_path, local_tz, start_hour
    ):
        local_tz("EST5EDT,M3.2.0,M11.1.0")
        store = make_db(tmp_path)
        start = datetime(2026, 10, 30, start_hour).timestamp()
        minutes = 72 * 60
        pressure = {}
        for i in range(minutes):
            clock = datetime.fromtimestamp(start + i * 60)
            if clock.hour >= 22 or clock.hour < 2:
                pressure[i] = 9.0
        _pressure_recording(
            store,
            start,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        found = _run(store, "MEM-PRESSURE-01")

        assert len(found) == 1, f"nightly pressure from {start_hour}:00 was missed"
        store.close()

    @pytest.mark.parametrize("hours", [24, 48])
    def test_a_missing_row_does_not_split_a_one_off_run_in_two(
        self, tmp_path, local_tz, hours
    ):
        local_tz("JST-9")
        minutes = hours * 60
        engine = Engine(ProcdiagConfig())
        reported = []
        for start_minute in range(0, 1440, 90):
            path = tmp_path / str(start_minute)
            path.mkdir()
            store = make_db(path)
            pressure = _windows(start_minute, minutes, [(0, 23 * 60 + 45, 30)])
            _pressure_recording(
                store,
                _local_midnight_jst() + start_minute * 60,
                minutes,
                pressure=pressure,
                swap_out={m: 2 * _MiB for m in pressure},
                missing={24 * 60 - start_minute},
            )
            diag = engine.run(store, only=["MEM-PRESSURE-01"])
            store.close()
            if diag.findings:
                reported.append(f"{start_minute // 60:02d}:{start_minute % 60:02d}")

        assert (
            reported == []
        ), f"one run was called chronic when recording from {reported}"

    @pytest.mark.parametrize(
        "quiet_minutes, fires", [(359, False), (360, True)], ids=["5h59m", "6h"]
    )
    def test_runs_less_than_six_hours_apart_are_one_occurrence(
        self, tmp_path, local_tz, quiet_minutes, fires
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start_minute = 12 * 60
        minutes = 48 * 60
        second = 21 * 60 + 30 + quiet_minutes
        pressure = _windows(start_minute, minutes, [(0, 21 * 60, 30), (0, second, 30)])
        _pressure_recording(
            store,
            _local_midnight_jst() + start_minute * 60,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        found = _run(store, "MEM-PRESSURE-01")

        assert bool(found) is fires
        if found:
            assert found[0].evidence["pressure_days"] == [
                _local_date(0),
                _local_date(1),
            ]
        store.close()

    def test_a_job_every_four_hours_is_pressure_on_every_day(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        pressure = {m: 9.0 for m in range(minutes) if m % (4 * 60) < 15}
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_pressure_days"] == 3
        assert f.evidence["n_total_days"] == 3
        store.close()

    @pytest.mark.parametrize(
        "swap_days, fires", [((1,), False), ((0, 1, 2), True)], ids=["1", "3"]
    )
    def test_unbroken_pressure_is_a_pressure_day_only_where_it_swapped_out(
        self, tmp_path, local_tz, swap_days, fires
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure={m: 20.0 for m in range(minutes)},
            swap_out={d * 1440 + 12 * 60: 2 * _MiB for d in swap_days},
        )

        found = _run(store, "MEM-PRESSURE-01")

        assert bool(found) is fires
        if found:
            assert found[0].evidence["n_pressure_days"] == 3
            assert found[0].evidence["swap_out_total_bytes"] == 3 * 2 * _MiB
        store.close()

    @pytest.mark.parametrize("swap_minute", [0, 14 * 60 - 1], ids=["first", "last"])
    def test_a_fourteen_hour_run_with_one_swap_minute_is_one_day(
        self, tmp_path, local_tz, swap_minute
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start_minute = 12 * 60
        minutes = 48 * 60
        run_start = 18 * 60 - start_minute
        pressure = {run_start + k: 20.0 for k in range(14 * 60)}
        _pressure_recording(
            store,
            _local_midnight_jst() + start_minute * 60,
            minutes,
            pressure=pressure,
            swap_out={run_start + swap_minute: 2 * _MiB},
        )

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    @pytest.mark.parametrize("start_hour", range(0, 24, 3))
    def test_unbroken_pressure_with_swap_out_throughout_is_found_in_36_hours(
        self, tmp_path, local_tz, start_hour
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 36 * 60
        pressure = {m: 20.0 for m in range(minutes)}
        _pressure_recording(
            store,
            _local_midnight_jst() + start_hour * 3600,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_pressure_days"] >= 2
        store.close()

    def test_the_totals_leave_out_pressure_without_enough_swap_out(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 72 * 60
        pressure = {m: 9.0 for m in _daily(14 * 60, 30, minutes)}
        swap = {m: 8 * _MiB for m in pressure}
        weak = range(1440 + 3 * 60, 1440 + 3 * 60 + 20)
        pressure.update({m: 9.0 for m in weak})
        swap[weak[5]] = _MiB // 2
        _pressure_recording(
            store, _local_midnight_jst(), minutes, pressure=pressure, swap_out=swap
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["pressure_event_count"] == 3 * 30
        assert f.evidence["swap_out_total_bytes"] == 3 * 30 * 8 * _MiB
        store.close()

    def test_the_report_says_how_the_days_were_counted(self, tmp_path, local_tz):
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

        lines = {line[:4]: line for line in f.proposal.splitlines()}
        assert "2/2 日" in f.summary and "反復" not in f.summary
        assert "2 / 2 日" in lines["[検出]"] and "反復" not in lines["[検出]"]
        assert "6 時間未満" in lines["[根拠]"]
        assert "最も早い日" in lines["[根拠]"]
        assert "6 時間以上かかった日" in lines["[根拠]"]
        store.close()


class TestMemPressureEdgeDaysAreWeighedByTheirObservation:
    @staticmethod
    def _daily_1400(tmp_path, start_hour, hours):
        tmp_path.mkdir(exist_ok=True)
        store = make_db(tmp_path)
        minutes = int(hours * 60)
        pressure = _windows(
            int(start_hour * 60), minutes, [(d, 14 * 60, 30) for d in range(5)]
        )
        _pressure_recording(
            store,
            _local_midnight_jst() + start_hour * 3600,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )
        return store

    @pytest.mark.parametrize(
        ("start_hour", "hours", "fraction"),
        [(15, 66, 0.65), (17, 64, 0.67), (16, 68, 0.64), (14.5, 65, 0.66)],
    )
    def test_quiet_edge_days_do_not_outvote_a_daily_pattern(
        self, tmp_path, local_tz, start_hour, hours, fraction
    ):
        local_tz("JST-9")
        store = self._daily_1400(tmp_path, start_hour, hours)

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert (f.evidence["n_pressure_days"], f.evidence["n_total_days"]) == (2, 4)
        assert f.evidence["recurrence_fraction"] == pytest.approx(fraction, abs=0.006)
        store.close()

    def test_the_same_days_without_the_edge_days_say_the_same(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = self._daily_1400(tmp_path, 0, 48)

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert (f.evidence["n_pressure_days"], f.evidence["n_total_days"]) == (2, 2)
        assert f.evidence["recurrence_fraction"] == 1.0
        store.close()

    @pytest.mark.parametrize(("edge_hours", "fires"), [(6, True), (9, False)])
    def test_a_quiet_edge_day_counts_as_much_as_it_was_observed(
        self, tmp_path, local_tz, edge_hours, fires
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start_hour = 24 - edge_hours
        minutes = (edge_hours + 24 * 3) * 60
        pressure = _windows(
            start_hour * 60, minutes, [(1, 14 * 60, 30), (2, 14 * 60, 30)]
        )
        _pressure_recording(
            store,
            _local_midnight_jst() + start_hour * 3600,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        found = _run(store, "MEM-PRESSURE-01")

        assert bool(found) is fires
        store.close()

    def test_pressure_on_two_of_five_whole_days_is_still_not_most_of_them(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 5 * 24 * 60
        pressure = _windows(0, minutes, [(1, 14 * 60, 30), (3, 14 * 60, 30)])
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_a_day_of_twenty_five_hours_counts_as_one_day_not_more(
        self, tmp_path, local_tz
    ):
        local_tz("America/New_York")
        store = make_db(tmp_path)
        midnight = time.mktime(datetime(2026, 10, 31).timetuple())
        minutes = (24 + 25 + 24) * 60
        pressure = {
            d * 1440 + (60 if d == 2 else 0) + 14 * 60 + k: 9.0
            for d in (0, 2)
            for k in range(30)
        }
        _pressure_recording(
            store,
            midnight,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert (f.evidence["n_pressure_days"], f.evidence["n_total_days"]) == (2, 3)
        assert f.evidence["recurrence_fraction"] == 0.67
        store.close()


class TestMemPressureTypicalDayIsAtMostTwentyFourHours:
    def test_a_day_of_twenty_five_hours_does_not_shorten_the_days_of_twenty_four(
        self, tmp_path, local_tz
    ):
        local_tz("America/New_York")
        store = make_db(tmp_path)
        midnight = time.mktime(datetime(2026, 10, 31).timetuple())
        minutes = (24 + 25 + 24) * 60
        pressure = {d * 1440 + 14 * 60 + k: 9.0 for d in (0, 1) for k in range(30)}
        _pressure_recording(
            store,
            midnight,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert (f.evidence["n_pressure_days"], f.evidence["n_total_days"]) == (2, 3)
        assert f.evidence["recurrence_fraction"] == 0.67
        assert f.evidence["typical_day_hours"] == 24.0
        store.close()

    def test_a_day_of_twenty_four_and_one_of_twenty_five_hours_are_a_day_of_twenty_four(
        self, tmp_path, local_tz
    ):
        local_tz("America/New_York")
        store = make_db(tmp_path)
        midnight = time.mktime(datetime(2026, 10, 31).timetuple())
        minutes = (24 + 25) * 60
        pressure = {d * 1440 + 14 * 60 + k: 9.0 for d in (0, 1) for k in range(30)}
        _pressure_recording(
            store,
            midnight,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert (f.evidence["n_pressure_days"], f.evidence["n_total_days"]) == (2, 2)
        assert f.evidence["typical_day_hours"] == 24.0
        store.close()


class TestMemPressureOfAPcThatRunsPartOfEachDay:
    @staticmethod
    def _shifts(tmp_path, *, on_hour, hours_on, first_minute, minutes, pressure_days):
        """A PC on *hours_on* hours from *on_hour* each day, 30 minutes of pressure
        at 10:00 on *pressure_days*; recorded from *first_minute* of day 0 for
        *minutes* minutes."""
        tmp_path.mkdir(exist_ok=True)
        store = make_db(tmp_path)
        on = range(on_hour * 60, (on_hour + hours_on) * 60)
        missing = frozenset(
            i for i in range(minutes) if (first_minute + i) % 1440 not in on
        )
        pressure = _windows(
            first_minute, minutes, [(d, 10 * 60, 30) for d in pressure_days]
        )
        _pressure_recording(
            store,
            _local_midnight_jst() + first_minute * 60,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
            missing=missing,
        )
        return store

    @pytest.mark.parametrize(
        ("on_hour", "hours_on"), [(8, 8), (6, 12), (7, 12), (6, 16), (9, 6)]
    )
    def test_pressure_on_two_of_six_days_is_not_chronic_whatever_hours_it_runs(
        self, tmp_path, local_tz, on_hour, hours_on
    ):
        local_tz("JST-9")
        store = self._shifts(
            tmp_path,
            on_hour=on_hour,
            hours_on=hours_on,
            first_minute=0,
            minutes=6 * 1440,
            pressure_days=(0, 5),
        )

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_a_pc_on_twelve_hours_a_day_with_pressure_on_half_of_four_days(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = self._shifts(
            tmp_path,
            on_hour=7,
            hours_on=12,
            first_minute=0,
            minutes=4 * 1440,
            pressure_days=(0, 3),
        )

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_a_short_first_day_of_a_part_time_pc_counts_as_much_as_it_was_observed(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = self._shifts(
            tmp_path,
            on_hour=8,
            hours_on=8,
            first_minute=9 * 60 + 30,
            minutes=4 * 1440 + 7 * 60,
            pressure_days=(1, 2),
        )

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    @pytest.mark.parametrize(
        ("pressure_days", "fires"), [((0, 2, 4), True), ((1, 3), False)]
    )
    def test_three_of_five_days_is_most_of_them_for_a_pc_on_eight_hours_a_day(
        self, tmp_path, local_tz, pressure_days, fires
    ):
        local_tz("JST-9")
        store = self._shifts(
            tmp_path,
            on_hour=8,
            hours_on=8,
            first_minute=0,
            minutes=5 * 1440,
            pressure_days=pressure_days,
        )

        found = _run(store, "MEM-PRESSURE-01")

        assert bool(found) is fires
        if found:
            assert found[0].evidence["recurrence_fraction"] == 0.6
        store.close()

    def test_pressure_on_most_days_of_a_pc_on_eight_hours_a_day_is_found(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = self._shifts(
            tmp_path,
            on_hour=8,
            hours_on=8,
            first_minute=0,
            minutes=6 * 1440,
            pressure_days=(0, 1, 3, 4),
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert (f.evidence["n_pressure_days"], f.evidence["n_total_days"]) == (4, 6)
        store.close()


_WEEKDAYS = (0, 1, 2, 3, 4, 7, 8, 9, 10, 11)


class TestMemPressureTypicalDayIsWhatMostDaysObserve:
    @staticmethod
    def _schedule(tmp_path, days, pressure_days):
        """A PC on ``days[day] = (first hour, hours on)``, with 30 minutes of
        pressure at 10:00 on *pressure_days*; day 0 starts at local midnight."""
        tmp_path.mkdir(exist_ok=True)
        store = make_db(tmp_path)
        minutes = (max(days) + 1) * 1440
        on = {
            m
            for day, (first, hours) in days.items()
            for m in range(day * 1440 + first * 60, day * 1440 + (first + hours) * 60)
        }
        pressure = _windows(0, minutes, [(d, 10 * 60, 30) for d in pressure_days])
        _pressure_recording(
            store,
            _local_midnight_jst(),
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
            missing=frozenset(range(minutes)) - on,
        )
        return store

    @staticmethod
    def _weeks(saturday=None):
        days = {d: (8, 8) for d in _WEEKDAYS}
        if saturday:
            days[5] = saturday
        return days

    @pytest.mark.parametrize(("pressure", "fires"), [(5, False), (6, False), (7, True)])
    def test_a_whole_saturday_does_not_make_eight_hour_weekdays_weigh_a_third(
        self, tmp_path, local_tz, pressure, fires
    ):
        local_tz("JST-9")
        store = self._schedule(tmp_path, self._weeks((0, 24)), _WEEKDAYS[:pressure])

        found = _run(store, "MEM-PRESSURE-01")

        assert bool(found) is fires
        if found:
            (f,) = found
            assert (f.evidence["n_pressure_days"], f.evidence["n_total_days"]) == (
                7,
                11,
            )
            assert f.evidence["typical_day_hours"] == 8.0
            assert f.evidence["observed_day_equivalents"] == 11.0
            assert "7/11 日" in f.summary
        store.close()

    def test_the_same_weekdays_without_the_saturday_are_judged_the_same_way(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = self._schedule(tmp_path, self._weeks(), _WEEKDAYS[:6])

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["recurrence_fraction"] == 0.6
        assert f.evidence["typical_day_hours"] == 8.0
        store.close()

    @pytest.mark.parametrize("saturday", [(8, 12), (6, 16)])
    def test_a_longer_day_that_is_not_a_whole_day_does_not_change_the_weekdays_either(
        self, tmp_path, local_tz, saturday
    ):
        local_tz("JST-9")
        store = self._schedule(tmp_path, self._weeks(saturday), _WEEKDAYS[:6])

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_six_hour_days_and_one_whole_day_are_judged_by_the_six_hours(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        days = {d: (9, 6) for d in range(6)}
        days[6] = (0, 24)
        store = self._schedule(tmp_path, days, (0, 1, 2, 3))

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_the_typical_day_is_the_median_not_the_shortest_of_the_days(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        days = {0: (9, 6), 1: (9, 6), 2: (4, 16), 3: (4, 16)}
        store = self._schedule(tmp_path, days, (2, 3))

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["recurrence_fraction"] == 0.65
        assert f.evidence["observed_day_equivalents"] == 3.09
        assert f.evidence["typical_day_hours"] == 11.0
        store.close()

    def test_a_day_with_less_than_six_hours_does_not_shorten_the_typical_day(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        days = {0: (9, 4), 1: (9, 4), 2: (4, 16), 3: (4, 16), 4: (4, 16), 5: (9, 6)}
        store = self._schedule(tmp_path, days, (0, 1, 2, 3))

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_total_days"] == 6
        assert f.evidence["typical_day_hours"] == 16.0
        store.close()

    def test_days_that_are_all_too_short_to_be_judged_without_pressure_show_a_whole_day(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        days = {d: (9, 4) for d in range(7)}
        store = self._schedule(tmp_path, days, range(7))

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["n_total_days"] == 7
        assert f.evidence["recurrence_fraction"] == 1.0
        assert f.evidence["typical_day_hours"] == 24.0
        store.close()

    def test_the_basis_says_how_the_typical_day_was_chosen(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = self._schedule(tmp_path, self._weeks((0, 24)), _WEEKDAYS[:7])

        (f,) = _run(store, "MEM-PRESSURE-01")

        basis = next(
            line for line in f.proposal.splitlines() if line.startswith("[根拠]")
        )
        assert "中央値 (この記録では 8 時間" in basis
        assert "観測日 11 日は 11 日分" in basis
        assert "7 / 11 日 (64%)" in f.proposal.splitlines()[0]
        store.close()


class TestMemPressureShowsTheRatioItUsed:
    @staticmethod
    def _found(tmp_path, local_tz, start_hour, hours):
        local_tz("JST-9")
        tmp_path.mkdir(exist_ok=True)
        store = make_db(tmp_path)
        minutes = int(hours * 60)
        pressure = _windows(
            int(start_hour * 60), minutes, [(d, 14 * 60, 30) for d in range(5)]
        )
        _pressure_recording(
            store,
            _local_midnight_jst() + start_hour * 3600,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )
        (f,) = _run(store, "MEM-PRESSURE-01")
        store.close()
        return f

    @staticmethod
    def _ratio(line):
        """The ``N / D 日 (P%)`` of a finding's text, as ``(N, D, P)``."""
        match = re.search(r"([\d.]+) / ([\d.]+) 日 \((\d+)%\)", line)
        assert match, line
        return float(match[1]), float(match[2]), int(match[3])

    @pytest.mark.parametrize(
        ("start_hour", "hours"), [(15, 66), (17, 64), (16, 68), (14.5, 65)]
    )
    def test_the_detection_line_divides_to_the_percentage_it_shows(
        self, tmp_path, local_tz, start_hour, hours
    ):
        f = self._found(tmp_path, local_tz, start_hour, hours)

        pressure_days, days, percent = self._ratio(f.proposal.splitlines()[0])

        assert round(pressure_days / days * 100) == percent
        assert percent == round(f.evidence["recurrence_fraction"] * 100)

    def test_the_summary_divides_to_the_ratio_the_rule_used(self, tmp_path, local_tz):
        f = self._found(tmp_path, local_tz, 15, 66)

        match = re.search(r"([\d.]+)/([\d.]+) 日", f.summary)
        assert match, f.summary
        assert float(match[1]) / float(match[2]) == pytest.approx(
            f.evidence["recurrence_fraction"], abs=0.006
        )

    def test_the_basis_says_how_the_quiet_days_were_counted(self, tmp_path, local_tz):
        f = self._found(tmp_path, local_tz, 15, 66)

        basis = next(
            line for line in f.proposal.splitlines() if line.startswith("[根拠]")
        )
        assert "圧迫日 2 / 観測日 4" in basis
        assert "観測日 4 日は 3.09 日分になり" in basis
        assert "中央値 (この記録では 16.5 時間。上限は 24 時間)" in basis

    def test_whole_days_are_shown_as_plain_day_counts(self, tmp_path, local_tz):
        f = self._found(tmp_path, local_tz, 0, 48)

        assert self._ratio(f.proposal.splitlines()[0]) == (2, 2, 100)
        assert "2/2 日" in f.summary


class TestMemPressureSmallSwapOutOfSeveralRuns:
    @staticmethod
    def _runs_a_day(tmp_path, hours, run_hours, swap):
        """Twenty-minute runs at *run_hours* every day, *swap* bytes in each."""
        tmp_path.mkdir(exist_ok=True)
        store = make_db(tmp_path)
        minutes = hours * 60
        pressure = {
            d * 1440 + h * 60 + k: 9.0
            for d in range(hours // 24 + 1)
            for h in run_hours
            for k in range(20)
            if d * 1440 + h * 60 + k < minutes
        }
        swap_out = {
            d * 1440 + h * 60 + 5: swap
            for d in range(hours // 24 + 1)
            for h in run_hours
            if d * 1440 + h * 60 + 5 < minutes
        }
        _pressure_recording(
            store, _local_midnight_jst(), minutes, pressure=pressure, swap_out=swap_out
        )
        return store

    def test_two_runs_a_day_with_half_a_mebibyte_each_make_the_day(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = self._runs_a_day(tmp_path, 72, (6, 18), _MiB // 2)

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert (f.evidence["n_pressure_days"], f.evidence["n_total_days"]) == (3, 3)
        assert f.evidence["pressure_event_count"] == 6 * 20
        assert f.evidence["swap_out_total_bytes"] == 6 * (_MiB // 2)
        store.close()

    def test_the_report_numbers_follow_the_credited_runs(self, tmp_path, local_tz):
        local_tz("JST-9")
        store = self._runs_a_day(tmp_path, 72, (6, 18), _MiB // 2)

        (f,) = _run(store, "MEM-PRESSURE-01")

        basis = next(
            line for line in f.proposal.splitlines() if line.startswith("[根拠]")
        )
        assert "圧迫日 3 / 観測日 3、反復率 1.0" in basis
        assert "超過分数は合計 120 分" in basis
        assert f"swap_out 合計 {6 * (_MiB // 2)} バイト" in basis
        store.close()

    def test_a_run_without_swap_out_is_not_counted_with_the_ones_that_made_the_day(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        tmp_path.mkdir(exist_ok=True)
        store = make_db(tmp_path)
        minutes = 72 * 60
        pressure = {
            d * 1440 + h * 60 + k: 9.0
            for d in range(3)
            for h in (6, 14, 22)
            for k in range(20)
        }
        swap_out = {
            d * 1440 + h * 60 + 5: int(0.6 * _MiB) for d in range(3) for h in (14, 22)
        }
        _pressure_recording(
            store, _local_midnight_jst(), minutes, pressure=pressure, swap_out=swap_out
        )

        (f,) = _run(store, "MEM-PRESSURE-01")

        assert f.evidence["pressure_event_count"] == 3 * 2 * 20
        store.close()

    def test_three_runs_a_day_with_less_than_a_mebibyte_in_all_do_not(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = self._runs_a_day(tmp_path, 72, (6, 14, 22), int(0.3 * _MiB))

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_two_runs_a_day_with_less_than_a_mebibyte_in_all_do_not(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = self._runs_a_day(tmp_path, 72, (6, 18), int(0.4 * _MiB))

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()

    def test_weak_runs_of_one_night_across_midnight_are_still_one_day(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start = 20 * 60
        minutes = 30 * 60
        pressure = _windows(start, minutes, [(0, 23 * 60 + 45, 30)])
        _pressure_recording(
            store,
            _local_midnight_jst() + start * 60,
            minutes,
            pressure=pressure,
            swap_out={m: _MiB // 4 for m in pressure},
        )

        assert _run(store, "MEM-PRESSURE-01") == []
        store.close()


class TestMemPressureLongDayBoundary:
    @pytest.mark.parametrize(("end_minute", "days"), [(6 * 60, 2), (6 * 60 - 1, 1)])
    def test_a_day_needs_six_hours_of_an_unbroken_run_to_be_its_own_pressure_day(
        self, tmp_path, local_tz, end_minute, days
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        start = 12 * 60
        minutes = 24 * 60
        pressure = _windows(start, minutes, [(0, 18 * 60, 6 * 60 + end_minute)])
        _pressure_recording(
            store,
            _local_midnight_jst() + start * 60,
            minutes,
            pressure=pressure,
            swap_out={m: 2 * _MiB for m in pressure},
        )

        found = _run(store, "MEM-PRESSURE-01")

        assert bool(found) is (days == 2)
        if found:
            assert found[0].evidence["n_pressure_days"] == 2
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

    def test_a_wait_of_two_minutes_is_never_shown_as_zero_hours(
        self, tmp_path, local_tz
    ):
        local_tz("JST-9")
        store = make_db(tmp_path)
        minutes = 24 * 60 + 6 * 60 - 2
        _pressure_recording(store, _local_midnight_jst(), minutes, pressure={})

        diag = Engine(ProcdiagConfig()).run(store, only=["MEM-PRESSURE-01"])

        (skipped,) = diag.skipped_rules
        assert skipped.rule_id == "MEM-PRESSURE-01"
        assert skipped.remaining_hours == 0.1
        assert any(
            "MEM-PRESSURE-01" in step and "あと 0.1h" in step for step in diag.reasoning
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
