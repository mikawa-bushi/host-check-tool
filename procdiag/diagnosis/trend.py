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
from typing import Any, Callable, Hashable, Iterable, Iterator, Mapping, Optional

from ..core import Confidence, Observation, covers

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


# Rollup rows carry the start of their minute, the row stamped before the
# window start is left out, and the minute in progress at stop is never rolled
# up.  A rollup series inside a window the engine gate let through therefore
# spans at least the requirement less four minutes (for a whole-minute
# requirement).  On raw series such as disk rows or proc_counts this only adds
# leniency.
def _series_covers(span_hours: float, hours: float) -> bool:
    return covers(span_hours + 2 / 60, hours)


def fit_linear(
    series: list[tuple[float, float]],
    *,
    min_points: int = 12,
    min_span_hours: float = 6.0,
) -> Optional[TrendFit]:
    """Fit a least-squares line to *(timestamp, value)* pairs.

    Returns ``None`` when there are fewer than *min_points* samples or the
    observation span does not cover *min_span_hours* (see
    :func:`_series_covers`).

    ``slope_per_hour`` is in value-units per hour.  ``r2`` is the coefficient
    of determination: 0 = flat / random, 1 = perfect line.
    """
    pts = [(t, v) for t, v in series if v is not None]
    if len(pts) < min_points:
        return None

    ts_min = min(t for t, _ in pts)
    ts_max = max(t for t, _ in pts)
    span_hours = (ts_max - ts_min) / 3600.0
    if not _series_covers(span_hours, min_span_hours):
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
# Per-subject series helpers (shared by the regression-based leak rules)
# ---------------------------------------------------------------------------


def group_series(
    rows: Iterable[Mapping[str, Any]],
    *,
    key: str,
    value: str,
    ts: str = "ts_min",
) -> dict[Any, list[tuple[float, float]]]:
    """Group rows into ``{row[key]: [(row[ts], float(row[value])), ...]}``.

    Rows whose *value* is NULL are skipped; row order (time order, as returned
    by ``Storage.rows``) is preserved within each group.
    """
    out: dict[Any, list[tuple[float, float]]] = {}
    for row in rows:
        v = row[value]
        if v is not None:
            out.setdefault(row[key], []).append((row[ts], float(v)))
    return out


def fit_all(
    series_by_key: Mapping[Hashable, list[tuple[float, float]]],
    *,
    min_points: int,
    min_span_hours: float,
) -> Iterator[tuple[Any, list[tuple[float, float]], TrendFit]]:
    for key, series in series_by_key.items():
        fit = fit_linear(series, min_points=min_points, min_span_hours=min_span_hours)
        if fit is not None:
            yield key, series, fit


def fit_each(
    series_by_key: Mapping[Hashable, list[tuple[float, float]]],
    *,
    min_points: int,
    min_span_hours: float,
    min_r2: float,
) -> Iterator[tuple[Any, list[tuple[float, float]], TrendFit]]:
    for key, series, fit in fit_all(
        series_by_key, min_points=min_points, min_span_hours=min_span_hours
    ):
        if fit.r2 >= min_r2:
            yield key, series, fit


def proc_trend_observations(
    rows: Iterable[Mapping[str, Any]],
    identities: Mapping[int, tuple[str, str]],
    column: str,
    *,
    min_span_hours: float,
    unit: float = 1.0,
) -> list[Observation]:
    observations = []
    for proc_id, series, fit in fit_all(
        group_series(rows, key="proc_id", value=column),
        min_points=12,
        min_span_hours=min_span_hours,
    ):
        name, cmdline_hash = identities.get(proc_id, (f"proc_{proc_id}", None))
        observations.append(
            Observation(
                subject=name,
                identity=cmdline_hash,
                value=fit.slope_per_hour / unit,
                evidence={
                    "proc_id": proc_id,
                    "r2": fit.r2,
                    "n": fit.n,
                    "span_hours": fit.span_hours,
                    "last_value": series[-1][1],
                },
            )
        )
    return observations


def observed_fit(obs: Observation, unit: float = 1.0) -> TrendFit:
    ev = obs.evidence
    return TrendFit(obs.value * unit, ev["r2"], ev["n"], ev["span_hours"])


def days_until(current: float, limit: float, slope_per_hour: float) -> Optional[float]:
    """Days until a value growing at *slope_per_hour* reaches *limit*.

    ``None`` when the value is not growing or is already at/over the limit.
    """
    if slope_per_hour <= 0:
        return None
    remaining = limit - current
    if remaining <= 0:
        return None
    return remaining / (slope_per_hour * 24.0)


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

    * ``HIGH``   — ``r2 >= high_r2`` **and** the span covers ``high_span_hours``
    * ``MEDIUM`` — ``r2 >= medium_r2`` **and** the span covers ``min_observation_hours``
    * ``LOW``    — otherwise
    """
    if fit.r2 >= high_r2 and _series_covers(fit.span_hours, high_span_hours):
        return Confidence.HIGH
    if fit.r2 >= medium_r2 and _series_covers(fit.span_hours, min_observation_hours):
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

    times = [datetime.fromtimestamp(ts, tz=timezone.utc) for ts in timestamps]

    # --- daily check: same second-of-day on distinct calendar dates ---
    daily = _recurring_offset(
        times,
        slot=lambda dt: dt.strftime("%Y-%m-%d"),
        offset=lambda dt: (
            dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6
        ),
        tol_sec=tol_sec,
        min_events=min_events,
    )
    if daily is not None:
        sod, count = daily
        h = int(sod // 3600)
        m = int((sod % 3600) // 60)
        return Period(kind="daily", anchor=f"{h:02d}:{m:02d}", count=count)

    # --- hourly check: same second-of-hour in distinct clock-hours ---
    hourly = _recurring_offset(
        times,
        slot=lambda dt: dt.strftime("%Y-%m-%dT%H"),
        offset=lambda dt: dt.minute * 60 + dt.second + dt.microsecond / 1e6,
        tol_sec=tol_sec,
        min_events=min_events,
    )
    if hourly is not None:
        soh, count = hourly
        return Period(kind="hourly", anchor=f":{int(soh // 60):02d}", count=count)

    return None


def _recurring_offset(
    times: list[datetime],
    *,
    slot: Callable[[datetime], str],
    offset: Callable[[datetime], float],
    tol_sec: float,
    min_events: int,
) -> Optional[tuple[float, int]]:
    """Find an offset that recurs (±*tol_sec*) in at least *min_events* slots.

    Events are grouped by ``slot(dt)`` (a calendar day or a clock-hour) and
    reduced to ``offset(dt)`` seconds within that slot.  Every observed offset
    is tried as a candidate anchor, in observation order; the first one matched
    by enough distinct slots is returned as ``(offset, matching_slots)``.
    """
    by_slot: dict[str, list[float]] = {}
    for dt in times:
        by_slot.setdefault(slot(dt), []).append(offset(dt))

    for anchor in (o for offsets in by_slot.values() for o in offsets):
        matching = sum(
            1
            for offsets in by_slot.values()
            if any(abs(o - anchor) <= tol_sec for o in offsets)
        )
        if matching >= min_events:
            return anchor, matching
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
# confidence_from_span
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
    if _series_covers(span_hours, high_hours) and n_points >= high_points:
        return Confidence.HIGH
    if _series_covers(span_hours, medium_hours):
        return Confidence.MEDIUM
    return Confidence.LOW


# ---------------------------------------------------------------------------
# host-specific baseline (weekday × hour median / MAD)
# ---------------------------------------------------------------------------


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
