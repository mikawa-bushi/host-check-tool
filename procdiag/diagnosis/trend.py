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

from ..core import Confidence

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
# confidence_from_fit
# ---------------------------------------------------------------------------


def confidence_from_fit(
    fit: TrendFit,
    *,
    min_observation_hours: float,
    high_r2: float = 0.95,
    high_span_hours: float = 12.0,
    medium_r2: float = 0.90,
) -> Confidence:
    """Map a :class:`TrendFit` to a :class:`~procdiag.core.Confidence` level.

    Shared by the regression-based leak rules (MEM-LEAK / FD-LEAK /
    THREAD-LEAK) so the R²×observation-span ladder lives in one place:

    * ``HIGH``   — ``r2 >= high_r2`` **and** ``span_hours >= high_span_hours``
    * ``MEDIUM`` — ``r2 >= medium_r2`` **and** ``span_hours >= min_observation_hours``
    * ``LOW``    — otherwise
    """
    if fit.r2 >= high_r2 and fit.span_hours >= high_span_hours:
        return Confidence.HIGH
    if fit.r2 >= medium_r2 and fit.span_hours >= min_observation_hours:
        return Confidence.MEDIUM
    return Confidence.LOW


# ---------------------------------------------------------------------------
# find_sustained_episodes
# ---------------------------------------------------------------------------


def find_sustained_episodes(
    samples: list[tuple[int, float, bool]],
    *,
    min_consecutive: int,
    max_gap_seconds: float = 90.0,
) -> list[tuple[float, float]]:
    """Find runs of consecutive "saturated" minute buckets.

    *samples* is a list of ``(bucket, ts, is_saturated)`` triples (sorted here
    by ``bucket``).  Two saturated buckets are part of the same run while their
    ``bucket`` values are within *max_gap_seconds* of each other; a larger gap
    or an unsaturated bucket ends the current run.  A run qualifies as an
    episode only when it spans at least *min_consecutive* buckets.

    Returns ``(start_ts, end_ts)`` for each qualifying episode.  Shared by
    CPU-SINGLECORE-01 and CPU-SATURATION-01 so the run-detection state machine
    lives in one place.
    """
    episodes: list[tuple[float, float]] = []
    run_start: float | None = None
    run_end: float | None = None
    run_len = 0
    prev_bucket: int | None = None

    for bucket, ts, is_saturated in sorted(samples, key=lambda s: s[0]):
        is_consecutive = (
            prev_bucket is None or (bucket - prev_bucket) <= max_gap_seconds
        )

        if is_saturated and is_consecutive:
            if run_start is None:
                run_start = ts
            run_end = ts
            run_len += 1
        elif is_saturated and not is_consecutive:
            if run_start is not None and run_len >= min_consecutive:
                episodes.append((run_start, run_end))  # type: ignore[arg-type]
            run_start = ts
            run_end = ts
            run_len = 1
        else:
            if run_start is not None and run_len >= min_consecutive:
                episodes.append((run_start, run_end))  # type: ignore[arg-type]
            run_start = None
            run_end = None
            run_len = 0

        prev_bucket = bucket

    if run_start is not None and run_len >= min_consecutive:
        episodes.append((run_start, run_end))  # type: ignore[arg-type]

    return episodes


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


# ---------------------------------------------------------------------------
# host-specific baseline (weekday × hour median / MAD)
# ---------------------------------------------------------------------------


def confidence_from_span(
    span_hours: float,
    n_points: int,
    *,
    high_hours: float = 24.0,
    high_points: int = 10,
    medium_hours: float = 6.0,
) -> Confidence:
    """Confidence for an *extrapolation* rule from observation span / points.

    HIGH needs both a long span and enough points; MEDIUM a moderate span; LOW
    otherwise.  Shared by the time-extrapolation rules (e.g. DISK-FULL-01) so the
    span/points policy lives next to ``confidence_from_fit`` (regression) instead
    of being re-inlined per rule (issue v0.3 §2-7).
    """
    if span_hours >= high_hours and n_points >= high_points:
        return Confidence.HIGH
    if span_hours >= medium_hours:
        return Confidence.MEDIUM
    return Confidence.LOW


def learn_baseline(
    series: list[tuple[float, float]],
    *,
    min_per_bin: int = 3,
) -> dict[tuple[int, int], tuple[float, float]]:
    """Learn a ``(weekday, hour) -> (median, MAD)`` baseline from a value series.

    *series* is ``(ts_epoch, value)`` pairs — e.g. ``cpu_avg`` from many days of
    ``sys_rollup_1m``.  Each point is binned by the host-local weekday (0 = Mon)
    and hour; bins with at least *min_per_bin* points get a median and a median
    absolute deviation (MAD).  Sparser bins are omitted.

    Robust statistics (median/MAD) absorb the strong weekday/business-hour
    periodicity of factory edge PCs, so "is 45% CPU normal for *this* host at
    *this* time?" can be answered relative to its own history.  stdlib only.
    """
    bins: dict[tuple[int, int], list[float]] = {}
    for ts, val in series:
        if val is None:
            continue
        dt = datetime.fromtimestamp(ts)
        bins.setdefault((dt.weekday(), dt.hour), []).append(val)

    out: dict[tuple[int, int], tuple[float, float]] = {}
    for key, vals in bins.items():
        if len(vals) < min_per_bin:
            continue
        med = statistics.median(vals)
        mad = statistics.median([abs(v - med) for v in vals])
        out[key] = (med, mad)
    return out


def baseline_deviation(
    baseline: dict[tuple[int, int], tuple[float, float]],
    ts: float,
    value: float,
) -> Optional[float]:
    """Return how many MADs *value* sits from its ``(weekday, hour)`` baseline.

    ``None`` when the bin is unknown or its MAD is zero (no spread to measure
    against).  Positive = above the usual level for this time slot.  Intended as
    *evidence only* — it annotates "+N×普段" without changing detection, so the
    deterministic absolute-threshold rules keep their behaviour.
    """
    dt = datetime.fromtimestamp(ts)
    stats = baseline.get((dt.weekday(), dt.hour))
    if stats is None:
        return None
    med, mad = stats
    if mad <= 0.0:
        return None
    return (value - med) / mad
