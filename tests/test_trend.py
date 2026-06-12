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

from procdiag.diagnosis.trend import (
    Period,
    TrendFit,
    detect_periodicity,
    detect_recurrence,
    extrapolate_full,
    fit_linear,
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

    def test_truly_hourly(self):
        """Hourly events over 3 consecutive hours at :45 should be detected."""
        ts = _hourly_timestamps(3, minute=45)
        period = detect_periodicity(ts, tol_sec=120.0, min_events=3)
        assert period is not None

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
