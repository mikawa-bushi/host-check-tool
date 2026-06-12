"""Pure-function statistical helpers for the diagnosis rules.

Mirrors the design of netdiag's ``diagnosis/baseline.py`` and
``changepoint.py``: stdlib ``statistics`` only, dataclass return types, and
a strict "data insufficient → return None" policy so callers never have to
guard against nonsensical results from thin observations.

All functions are stateless and have no side-effects; they are safe to call
from any rule's ``evaluate()`` without concern for ordering or concurrency.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# TrendFit — result of linear regression
# ---------------------------------------------------------------------------


@dataclass
class TrendFit:
    """Result of a least-squares linear fit on a time series.

    ``slope_per_hour`` is the rate of change in the same unit as the series
    values (e.g. bytes/hour for RSS).  ``r2`` quantifies how well a straight
    line explains the variance (0–1; 1 is perfect).
    """

    slope_per_hour: float
    r2: float
    n: int
    span_hours: float


# ---------------------------------------------------------------------------
# Period — result of periodicity detection
# ---------------------------------------------------------------------------


@dataclass
class Period:
    """A detected periodic firing pattern.

    ``kind`` is ``"daily"`` (same clock-time each day) or ``"hourly"`` (same
    minute each hour).  ``anchor`` is a human-readable representative
    timestamp (HH:MM for daily, :MM for hourly).  ``count`` is the number of
    qualifying events.
    """

    kind: str  # "daily" | "hourly"
    anchor: str  # representative time string
    count: int


# ---------------------------------------------------------------------------
# fit_linear
# ---------------------------------------------------------------------------


def fit_linear(
    series: list[tuple[float, float]],
    *,
    min_points: int = 12,
    min_span_hours: float = 6.0,
) -> Optional[TrendFit]:
    """Fit a least-squares line to *(timestamp, value)* pairs.

    Returns ``None`` when there are fewer than *min_points* samples or the
    observation span is shorter than *min_span_hours*.

    ``slope_per_hour`` is in value-units per hour.  ``r2`` is the coefficient
    of determination: 0 = flat / random, 1 = perfect line.
    """
    pts = [(t, v) for t, v in series if v is not None]
    if len(pts) < min_points:
        return None

    ts_min = min(t for t, _ in pts)
    ts_max = max(t for t, _ in pts)
    span_hours = (ts_max - ts_min) / 3600.0
    if span_hours < min_span_hours:
        return None

    n = len(pts)
    # Convert timestamps to hours relative to the earliest point so the
    # slope is already in (value / hour) without needing large-number arithmetic.
    xs = [(t - ts_min) / 3600.0 for t, _ in pts]
    ys = [v for _, v in pts]

    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)

    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    ss_xx = sum((x - x_mean) ** 2 for x in xs)

    if ss_xx == 0.0:
        # All timestamps identical — degenerate; cannot fit a line.
        return None

    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean

    # R² = 1 - SS_res / SS_tot
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else 1.0

    return TrendFit(slope_per_hour=slope, r2=r2, n=n, span_hours=span_hours)


# ---------------------------------------------------------------------------
# extrapolate_full
# ---------------------------------------------------------------------------


def extrapolate_full(
    series: list[tuple[float, float]],
    capacity: float,
    *,
    min_points: int = 12,
) -> Optional[float]:
    """Extrapolate a *used* series to predict when it hits *capacity*.

    Returns the number of days from the last observation until full, or
    ``None`` when the slope is ≤ 0 (shrinking or flat) or data is
    insufficient.

    Per design doc §6.2 DISK-FULL-01: if 7 days of data are available, the
    most-recent 7-day window is preferred for the slope calculation (to avoid
    a brief historical spike distorting the forecast).
    """
    pts = sorted((t, v) for t, v in series if v is not None)
    if len(pts) < min_points:
        return None

    last_ts = pts[-1][0]
    seven_days_ago = last_ts - 7 * 86400.0
    recent_pts = [(t, v) for t, v in pts if t >= seven_days_ago]

    # Prefer 7-day window if it has enough points; fall back to full series.
    work_pts = recent_pts if len(recent_pts) >= min_points else pts

    fit = fit_linear(work_pts, min_points=min_points, min_span_hours=0.0)
    if fit is None or fit.slope_per_hour <= 0.0:
        return None

    current_used = work_pts[-1][1]
    remaining = capacity - current_used
    if remaining <= 0.0:
        # Already full — report 0 days.
        return 0.0

    # slope_per_hour → slope_per_day
    slope_per_day = fit.slope_per_hour * 24.0
    if slope_per_day <= 0.0:
        return None

    return remaining / slope_per_day


# ---------------------------------------------------------------------------
# detect_periodicity
# ---------------------------------------------------------------------------


def detect_periodicity(
    timestamps: list[float],
    *,
    tol_sec: float = 120.0,
    min_events: int = 3,
) -> Optional[Period]:
    """Detect daily or hourly periodicity in a list of event timestamps.

    **Daily**: at least *min_events* distinct calendar days each have an event
    within ±*tol_sec* of the same wall-clock time (HH:MM).

    **Hourly**: at least *min_events* distinct clock-hours each have an event
    within ±*tol_sec* of the same minute mark (:MM).

    Returns a :class:`Period` or ``None`` if no pattern is found.
    Daily patterns are checked first and take priority.
    """
    if len(timestamps) < min_events:
        return None

    # --- daily check ---
    # Group events by calendar date.  For each date, collect the seconds-since-midnight.
    day_seconds: dict[str, list[float]] = {}
    for ts in timestamps:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")
        sod = dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6
        day_seconds.setdefault(day_key, []).append(sod)

    # Try every observed second-of-day as a candidate anchor.
    all_sods = [s for sods in day_seconds.values() for s in sods]
    for anchor_sod in all_sods:
        matching_days = 0
        for sods in day_seconds.values():
            if any(abs(s - anchor_sod) <= tol_sec for s in sods):
                matching_days += 1
        if matching_days >= min_events:
            h = int(anchor_sod // 3600)
            m = int((anchor_sod % 3600) // 60)
            return Period(kind="daily", anchor=f"{h:02d}:{m:02d}", count=matching_days)

    # --- hourly check ---
    # Group events by (date, hour).  For each hour-slot, collect the
    # seconds-within-the-hour.
    hour_seconds: dict[str, list[float]] = {}
    for ts in timestamps:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour_key = dt.strftime("%Y-%m-%dT%H")
        soh = dt.minute * 60 + dt.second + dt.microsecond / 1e6
        hour_seconds.setdefault(hour_key, []).append(soh)

    all_sohs = [s for sohs in hour_seconds.values() for s in sohs]
    for anchor_soh in all_sohs:
        matching_hours = 0
        for sohs in hour_seconds.values():
            if any(abs(s - anchor_soh) <= tol_sec for s in sohs):
                matching_hours += 1
        if matching_hours >= min_events:
            m = int(anchor_soh // 60)
            return Period(kind="hourly", anchor=f":{m:02d}", count=matching_hours)

    return None


# ---------------------------------------------------------------------------
# detect_recurrence
# ---------------------------------------------------------------------------


def detect_recurrence(events_by_day: dict[str, bool]) -> float:
    """Return the fraction of days on which the event occurred.

    *events_by_day* maps date-string keys (YYYY-MM-DD) to a boolean indicating
    whether the event occurred on that day.  Used by MEM-PRESSURE-01 to
    measure how often chronic memory pressure was observed.

    Returns 0.0 for an empty dict.
    """
    if not events_by_day:
        return 0.0
    total = len(events_by_day)
    hits = sum(1 for v in events_by_day.values() if v)
    return hits / total
