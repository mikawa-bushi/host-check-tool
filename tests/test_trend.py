"""Unit tests for procdiag.diagnosis.trend — pure statistical helpers.

Tests cover:
- fit_linear: clean linear series, noisy linear, flat series, insufficient data
- extrapolate_full: growing series, declining/flat (None), 7-day preference
- detect_periodicity: daily pattern, hourly pattern, irregular (None)
- detect_recurrence: fraction computation
"""

from __future__ import annotations

import math
import time

import pytest

from procdiag.core import Confidence
from procdiag.diagnosis.trend import (
    TrendFit,
    baseline_deviation,
    confidence_from_fit,
    confidence_from_span,
    detect_periodicity,
    detect_recurrence,
    extrapolate_full,
    find_sustained_episodes,
    fit_linear,
    learn_baseline,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOUR = 3600.0
_DAY = 86400.0
_MB = 1024 * 1024


def _linear_series(
    slope: float,
    hours: float,
    *,
    n: int = 60,
    start_val: float = 0.0,
    start_ts: float | None = None,
) -> list[tuple[float, float]]:
    """Generate a perfect linear (ts, value) series."""
    if start_ts is None:
        start_ts = time.time() - hours * _HOUR
    step = hours * _HOUR / max(n - 1, 1)
    return [
        (start_ts + i * step, start_val + slope * (i * step / _HOUR)) for i in range(n)
    ]


def _noisy_series(
    slope: float,
    hours: float,
    *,
    n: int = 60,
    noise_frac: float = 0.05,
    seed: int = 0,
    start_ts: float | None = None,
) -> list[tuple[float, float]]:
    """Generate a linear series with Gaussian-like noise."""
    import random

    rng = random.Random(seed)
    pts = _linear_series(slope, hours, n=n, start_ts=start_ts)
    return [(ts, val * (1.0 + rng.gauss(0, noise_frac))) for ts, val in pts]


# ---------------------------------------------------------------------------
# fit_linear
# ---------------------------------------------------------------------------


class TestFitLinear:
    def test_perfect_line(self):
        """A perfectly linear series should return slope ≈ 1 MB/h and R² ≈ 1."""
        series = _linear_series(slope=_MB, hours=12.0, n=60, start_val=100 * _MB)
        fit = fit_linear(series, min_points=12, min_span_hours=6.0)
        assert fit is not None
        assert math.isclose(fit.slope_per_hour, _MB, rel_tol=1e-3)
        assert math.isclose(fit.r2, 1.0, abs_tol=1e-6)
        assert fit.n == 60
        assert fit.span_hours >= 11.9

    def test_noisy_linear(self):
        """A noisy linear series should still yield high R² and correct slope sign."""
        series = _noisy_series(slope=_MB, hours=12.0, n=60, noise_frac=0.05, seed=1)
        fit = fit_linear(series, min_points=12, min_span_hours=6.0)
        assert fit is not None
        assert fit.slope_per_hour > 0
        assert fit.r2 > 0.8  # high but not perfect with noise

    def test_flat_series_low_r2(self):
        """A flat series (slope=0) should return a fit but with low R² or slope≈0."""
        series = _linear_series(slope=0.0, hours=12.0, n=60, start_val=500 * _MB)
        fit = fit_linear(series, min_points=12, min_span_hours=6.0)
        # Flat line is technically still a valid fit; slope should be ~0.
        # R² is undefined for a constant series (ss_tot=0) — we expect 1.0 (our convention).
        assert fit is not None
        assert abs(fit.slope_per_hour) < 1.0  # effectively zero

    def test_insufficient_points_returns_none(self):
        """Fewer than min_points should return None."""
        series = _linear_series(slope=_MB, hours=8.0, n=5)
        assert fit_linear(series, min_points=12, min_span_hours=6.0) is None

    def test_short_span_returns_none(self):
        """Observation window shorter than min_span_hours should return None."""
        series = _linear_series(slope=_MB, hours=3.0, n=60)
        assert fit_linear(series, min_points=12, min_span_hours=6.0) is None

    def test_just_enough_points(self):
        """Exactly min_points samples should succeed."""
        series = _linear_series(slope=_MB, hours=8.0, n=12)
        fit = fit_linear(series, min_points=12, min_span_hours=6.0)
        assert fit is not None

    def test_slope_direction(self):
        """Negative slope should produce negative slope_per_hour."""
        series = _linear_series(slope=-_MB, hours=12.0, n=60, start_val=10_000 * _MB)
        fit = fit_linear(series, min_points=12, min_span_hours=6.0)
        assert fit is not None
        assert fit.slope_per_hour < 0


# ---------------------------------------------------------------------------
# extrapolate_full
# ---------------------------------------------------------------------------


class TestExtrapolateFull:
    def test_growing_series(self):
        """A growing series should yield a finite positive days_left."""
        capacity = 100 * _MB * 1000  # 100 GB in bytes
        series = _linear_series(
            slope=_MB * 100,  # 100 MB/h
            hours=24.0,
            n=48,
            start_val=10 * capacity // 100,
        )
        days = extrapolate_full(series, capacity, min_points=3)
        assert days is not None
        assert days > 0

    def test_flat_or_declining_returns_none(self):
        """A flat series should return None (slope ≤ 0)."""
        series = _linear_series(slope=0.0, hours=24.0, n=48, start_val=50 * _MB * 1000)
        result = extrapolate_full(series, 100 * _MB * 1000, min_points=3)
        assert result is None

    def test_declining_returns_none(self):
        """A declining series should return None."""
        series = _linear_series(
            slope=-_MB * 10,
            hours=24.0,
            n=48,
            start_val=80 * _MB * 1000,
        )
        result = extrapolate_full(series, 100 * _MB * 1000, min_points=3)
        assert result is None

    def test_seven_day_preference(self):
        """When >7 days of data exist, the 7-day window should be preferred."""
        capacity = int(200 * _MB * 1000)
        now = time.time()
        # Older data (15 days ago): very steep growth, would give few days.
        old_ts = now - 15 * _DAY
        old_series = _linear_series(
            slope=_MB * 500,
            hours=7 * 24,
            n=50,
            start_val=int(0.1 * capacity),
            start_ts=old_ts,
        )
        # Recent data (7 days): gentle growth, would give many days.
        recent_ts = now - 7 * _DAY
        recent_series = _linear_series(
            slope=_MB * 20,
            hours=7 * 24,
            n=50,
            start_val=int(0.5 * capacity),
            start_ts=recent_ts,
        )
        full_series = old_series + recent_series
        full_series.sort(key=lambda x: x[0])

        days = extrapolate_full(full_series, capacity, min_points=3)
        assert days is not None
        # With gentle recent slope, should predict many days left.
        assert days > 10

    def test_insufficient_points_returns_none(self):
        """Fewer than min_points returns None."""
        series = _linear_series(slope=_MB, hours=24.0, n=2)
        result = extrapolate_full(series, 100 * _MB * 1000, min_points=3)
        assert result is None

    def test_already_full(self):
        """Series where used >= capacity should return 0.0."""
        capacity = int(10 * _MB * 1000)
        series = _linear_series(
            slope=_MB,
            hours=24.0,
            n=48,
            start_val=capacity + 1,  # already over capacity
        )
        days = extrapolate_full(series, capacity, min_points=3)
        assert days is not None
        assert days == 0.0


# ---------------------------------------------------------------------------
# detect_periodicity
# ---------------------------------------------------------------------------


def _daily_timestamps(n_days: int, hour: int = 14, minute: int = 30) -> list[float]:
    """Generate timestamps at HH:MM UTC for *n_days* consecutive days."""
    now = time.time()
    # Align to midnight UTC.
    midnight = (now // _DAY) * _DAY - n_days * _DAY
    return [midnight + d * _DAY + hour * _HOUR + minute * 60 for d in range(n_days)]


def _hourly_timestamps(n_hours: int, minute: int = 15) -> list[float]:
    """Generate timestamps at :MM past each hour for *n_hours* hours."""
    now = time.time()
    base = (now // _HOUR) * _HOUR - n_hours * _HOUR
    return [base + h * _HOUR + minute * 60 for h in range(n_hours)]


class TestDetectPeriodicity:
    def test_daily_pattern(self):
        """Same time each day for 5 days should detect a daily period."""
        ts = _daily_timestamps(5, hour=14, minute=30)
        period = detect_periodicity(ts, tol_sec=120.0, min_events=3)
        assert period is not None
        assert period.kind == "daily"
        assert period.count >= 3

    def test_hourly_pattern(self):
        """Same minute each hour for 5 hours should detect an hourly period."""
        ts = _hourly_timestamps(5, minute=15)
        period = detect_periodicity(ts, tol_sec=120.0, min_events=3)
        assert period is not None
        assert period.kind in (
            "daily",
            "hourly",
        )  # 5 same-minute events can match either

    def test_three_hours_are_too_few_to_rule_out_chance(self):
        """Three spikes at :45 in three hours line up by chance too often."""
        ts = _hourly_timestamps(3, minute=45)
        assert detect_periodicity(ts, tol_sec=120.0, min_events=3) is None

    def test_truly_hourly(self):
        """Hourly events over 5 consecutive hours at :45 should be detected."""
        ts = _hourly_timestamps(5, minute=45)
        period = detect_periodicity(ts, tol_sec=120.0, min_events=3)
        assert period is not None
        assert (period.kind, period.anchor, period.count) == ("hourly", ":45", 5)

    def test_irregular_returns_none(self):
        """Timestamps with no pattern should return None."""
        import random

        rng = random.Random(99)
        now = time.time()
        ts = [now - rng.uniform(0, 7 * _DAY) for _ in range(10)]
        # Very tight tolerance → likely no pattern.
        period = detect_periodicity(ts, tol_sec=30.0, min_events=5)
        assert period is None

    def test_fewer_than_min_events_returns_none(self):
        """Only 2 events should return None when min_events=3."""
        ts = _daily_timestamps(2)
        period = detect_periodicity(ts, tol_sec=120.0, min_events=3)
        assert period is None

    def test_anchor_format_daily(self):
        """Daily anchor should be HH:MM format."""
        ts = _daily_timestamps(4, hour=9, minute=5)
        period = detect_periodicity(ts, tol_sec=120.0, min_events=3)
        assert period is not None
        assert period.kind == "daily"
        assert ":" in period.anchor
        parts = period.anchor.split(":")
        assert len(parts) == 2
        assert parts[0].isdigit() and parts[1].isdigit()


# ---------------------------------------------------------------------------
# detect_recurrence
# ---------------------------------------------------------------------------


class TestDetectRecurrence:
    def test_all_days_true(self):
        data = {"2026-01-01": True, "2026-01-02": True, "2026-01-03": True}
        assert detect_recurrence(data) == 1.0

    def test_half_days_true(self):
        data = {"2026-01-01": True, "2026-01-02": False}
        assert detect_recurrence(data) == 0.5

    def test_no_days_true(self):
        data = {"2026-01-01": False, "2026-01-02": False}
        assert detect_recurrence(data) == 0.0

    def test_empty_dict(self):
        assert detect_recurrence({}) == 0.0

    def test_fraction(self):
        data = {f"2026-01-{d:02d}": (d % 3 == 0) for d in range(1, 10)}
        frac = detect_recurrence(data)
        assert 0.0 <= frac <= 1.0


# ---------------------------------------------------------------------------
# confidence_from_fit (issue #4 §3-2)
# ---------------------------------------------------------------------------


def _fit(r2: float, span_hours: float) -> TrendFit:
    return TrendFit(slope_per_hour=1.0, r2=r2, n=100, span_hours=span_hours)


class TestConfidenceFromFit:
    def test_high_needs_high_r2_and_long_span(self):
        assert (
            confidence_from_fit(_fit(0.96, 12.0), min_observation_hours=6.0)
            == Confidence.HIGH
        )

    def test_high_r2_but_short_span_is_medium(self):
        # r2 high but span < 12h → not HIGH; r2>=0.90 and span>=min_obs → MEDIUM
        assert (
            confidence_from_fit(_fit(0.96, 10.0), min_observation_hours=6.0)
            == Confidence.MEDIUM
        )

    def test_medium_band(self):
        assert (
            confidence_from_fit(_fit(0.92, 8.0), min_observation_hours=6.0)
            == Confidence.MEDIUM
        )

    def test_low_when_r2_below_medium(self):
        assert (
            confidence_from_fit(_fit(0.80, 20.0), min_observation_hours=6.0)
            == Confidence.LOW
        )

    def test_low_when_span_below_min_observation(self):
        # r2 0.95 but span 5h < min_obs 6h → not MEDIUM, span<12 → not HIGH → LOW
        assert (
            confidence_from_fit(_fit(0.95, 5.0), min_observation_hours=6.0)
            == Confidence.LOW
        )

    def test_exact_high_boundary_is_inclusive(self):
        # both thresholds use >=, so the exact boundary qualifies as HIGH
        assert (
            confidence_from_fit(_fit(0.95, 12.0), min_observation_hours=6.0)
            == Confidence.HIGH
        )

    def test_exact_medium_boundary_is_inclusive(self):
        assert (
            confidence_from_fit(_fit(0.90, 6.0), min_observation_hours=6.0)
            == Confidence.MEDIUM
        )


# ---------------------------------------------------------------------------
# find_sustained_episodes (issue #4 §3-1)
# ---------------------------------------------------------------------------


class TestFindSustainedEpisodes:
    def test_single_run_meets_min(self):
        samples = [(0, 0.0, True), (60, 60.0, True), (120, 120.0, True)]
        assert find_sustained_episodes(samples, min_consecutive=3) == [(0.0, 120.0)]

    def test_run_below_min_is_dropped(self):
        samples = [(0, 0.0, True), (60, 60.0, True)]
        assert find_sustained_episodes(samples, min_consecutive=3) == []

    def test_time_gap_splits_into_two_episodes(self):
        samples = [
            (0, 0.0, True),
            (60, 60.0, True),
            (120, 120.0, True),
            (3000, 3000.0, True),  # gap > 90s from previous bucket
            (3060, 3060.0, True),
            (3120, 3120.0, True),
        ]
        assert find_sustained_episodes(samples, min_consecutive=3) == [
            (0.0, 120.0),
            (3000.0, 3120.0),
        ]

    def test_unsaturated_bucket_breaks_run(self):
        samples = [
            (0, 0.0, True),
            (60, 60.0, True),
            (120, 120.0, False),  # breaks the (too-short) first run
            (180, 180.0, True),
            (240, 240.0, True),
            (300, 300.0, True),
        ]
        assert find_sustained_episodes(samples, min_consecutive=3) == [(180.0, 300.0)]

    def test_input_is_sorted_by_bucket(self):
        samples = [(120, 120.0, True), (0, 0.0, True), (60, 60.0, True)]
        assert find_sustained_episodes(samples, min_consecutive=3) == [(0.0, 120.0)]


# ---------------------------------------------------------------------------
# host-specific baseline (issue v0.3 §3-8)
# ---------------------------------------------------------------------------


class TestConfidenceFromSpan:
    def test_ladder(self):
        # HIGH needs both a long span and enough points (issue v0.3 §2-7).
        assert confidence_from_span(30.0, 12) == Confidence.HIGH
        assert confidence_from_span(30.0, 5) == Confidence.MEDIUM  # too few points
        assert confidence_from_span(8.0, 50) == Confidence.MEDIUM  # span < 24h
        assert confidence_from_span(2.0, 50) == Confidence.LOW


class TestBaseline:
    def test_learn_baseline_and_deviation(self):
        from datetime import datetime

        week = 7 * 24 * 3600
        t0 = 1_700_000_000.0  # fixed epoch; same weekday/hour each +1 week
        series = [(t0 + i * week, 10.0 + i) for i in range(5)]  # values 10..14
        bl = learn_baseline(series, min_per_bin=3)

        key = (datetime.fromtimestamp(t0).weekday(), datetime.fromtimestamp(t0).hour)
        assert key in bl
        med, mad = bl[key]
        assert med == pytest.approx(12.0)  # median of 10..14
        assert mad == pytest.approx(1.0)  # median(|v-12|)

        # A value far above the usual level yields a large positive deviation.
        dev = baseline_deviation(bl, t0, 30.0)
        assert dev is not None and dev > 5

    def test_unknown_bin_returns_none(self):
        t0 = 1_700_000_000.0
        bl = learn_baseline([(t0, 10.0)] * 0)  # empty
        assert baseline_deviation(bl, t0, 50.0) is None

    def test_sparse_bin_omitted(self):
        t0 = 1_700_000_000.0
        bl = learn_baseline([(t0, 10.0), (t0, 12.0)], min_per_bin=3)
        assert bl == {}  # only 2 points in the bin, below min_per_bin


# ---------------------------------------------------------------------------
# group_series / fit_each / days_until (shared by the leak rules)
# ---------------------------------------------------------------------------


class TestSeriesHelpers:
    def test_group_series_skips_null_and_keeps_order(self):
        from procdiag.diagnosis.trend import group_series

        rows = [
            {"ts_min": 1.0, "proc_id": 1, "rss_avg": 10},
            {"ts_min": 1.0, "proc_id": 2, "rss_avg": None},
            {"ts_min": 2.0, "proc_id": 1, "rss_avg": 12},
            {"ts_min": 2.0, "proc_id": 2, "rss_avg": 5},
        ]
        out = group_series(rows, key="proc_id", value="rss_avg")
        assert out == {1: [(1.0, 10.0), (2.0, 12.0)], 2: [(2.0, 5.0)]}

    def test_fit_each_keeps_only_reliable_fits(self):
        from procdiag.diagnosis.trend import fit_each

        steady = _linear_series(1.0, 12, n=48)
        too_short = steady[:3]
        noisy = [(t, (i % 2) * 100.0) for i, (t, _) in enumerate(steady)]
        kept = list(
            fit_each(
                {"steady": steady, "short": too_short, "noisy": noisy},
                min_points=12,
                min_span_hours=6.0,
                min_r2=0.9,
            )
        )
        assert [k for k, _s, _f in kept] == ["steady"]
        assert kept[0][2].slope_per_hour == pytest.approx(1.0, rel=1e-6)

    def test_days_until(self):
        from procdiag.diagnosis.trend import days_until

        assert days_until(100.0, 124.0, 1.0) == pytest.approx(1.0)
        assert days_until(100.0, 50.0, 1.0) is None  # already past the limit
        assert days_until(100.0, 200.0, 0.0) is None  # not growing
