"""Pure-function statistical helpers for the diagnosis rules.

Mirrors the design of netdiag's ``diagnosis/baseline.py`` and
``changepoint.py``: stdlib ``statistics`` only, dataclass return types, and
a strict "data insufficient → return None" policy so callers never have to
guard against nonsensical results from thin observations.

All functions are stateless and have no side-effects; they are safe to call
from any rule's ``evaluate()`` without concern for ordering or concurrency.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Hashable, Iterable, Iterator, Mapping, Optional

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
    """A detected periodic firing pattern, in the host's local time.

    ``kind`` is ``"daily"`` (same clock-time each day) or ``"hourly"`` (same
    minute each hour).  ``anchor`` is a human-readable representative
    timestamp (HH:MM for daily, :MM for hourly).  ``count`` is the number of
    days (hours) in which a load episode started near the anchor, out of the
    ``cycles`` recorded days (hours) that include the anchor time.
    ``chance`` bounds the probability that episodes starting at random times
    line up this well.
    """

    kind: str  # "daily" | "hourly"
    anchor: str  # representative time string
    count: int
    cycles: int = 0
    chance: float = 1.0


# ---------------------------------------------------------------------------
# fit_linear
# ---------------------------------------------------------------------------


# Rollup rows carry the start of their minute, the row stamped before the
# window start is left out, and the minute in progress at stop is never rolled
# up, so a rollup series spans less than three minutes short of its window.
# The engine gate lets a window two minutes short through, so a series inside
# it spans more than the requirement less five minutes.  On raw series such
# as disk rows or proc_counts this only adds leniency.
def _series_covers(span_hours: float, hours: float) -> bool:
    return covers(span_hours + 3 / 60, hours)


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


# SpikeTrigger re-fires every spike_cooldown_sec under sustained load and stays
# silent for at most its 5-minute storm window; a throttled 1-second tier adds
# up to a minute.  Events closer than this belong to one load episode, and only
# the start of an episode is something a schedule sets.
_EPISODE_GAP_SEC = 360.0
# A schedule recurs in most recorded days (hours), so an hourly pattern needs
# more than the few hours a daily job touches.
_MIN_RECURRENCE = 0.5
_MAX_CHANCE = 0.01


def detect_periodicity(
    timestamps: list[float],
    *,
    tol_sec: float = 120.0,
    min_events: int = 3,
    recorded_minutes: Optional[Iterable[float]] = None,
) -> Optional[Period]:
    """Detect an hourly or daily schedule in a list of event timestamps.

    Events at most 6 minutes apart form one load episode, represented by
    its start, so one long load is one event rather than a pattern.  A clock
    time (host local time, as crontab and systemd timers use) is a schedule
    when episodes start within ±*tol_sec* of it in at least *min_events* days
    (hours), in at least half of the recorded days (hours) that include it,
    and when episode starts at random times would line up that well with
    probability below 1 %.  Days and hours wrap: a start at 00:00:20 matches
    an anchor of 23:59:40.

    *recorded_minutes* are the start times of the minutes the recorder ran;
    by default every minute from the first to the last event.

    Hourly is checked first and wins: an hourly job also recurs daily, but a
    daily job does not recur in half of the hours.
    """
    if len(timestamps) < min_events:
        return None
    ordered = sorted(timestamps)
    starts = [
        _wall_clock(ts)
        for prev, ts in zip([None, *ordered], ordered)
        if prev is None or ts - prev > _EPISODE_GAP_SEC
    ]
    if recorded_minutes is None:
        span = int((ordered[-1] - ordered[0]) // 60)
        recorded_minutes = [ordered[0] + 60.0 * i for i in range(span + 1)]
    minutes = [int(_wall_clock(ts) // 60) for ts in recorded_minutes]
    # Chance that one day (hour) holds an episode start inside a fixed window
    # 3 * tol_sec wide, if starts fell at random times at the recorded rate.
    p_window = min(1.0, len(starts) * 3 * tol_sec / (max(len(minutes), 1) * 60.0))

    for kind, cycle in (("hourly", 3600), ("daily", 86400)):
        found = _recurring_time(starts, minutes, cycle, tol_sec, min_events, p_window)
        if found is not None:
            phase, count, cycles, chance = found
            minute = int(phase // 60)
            anchor = (
                f"{minute // 60:02d}:{minute % 60:02d}"
                if kind == "daily"
                else f":{minute:02d}"
            )
            return Period(kind, anchor, count, cycles, chance)
    return None


def _wall_clock(ts: float) -> float:
    """*ts* as seconds on the host's local clock, counted as if it were UTC."""
    return datetime.fromtimestamp(ts).replace(tzinfo=timezone.utc).timestamp()


def _recurring_time(
    starts: list[float],
    minutes: list[int],
    cycle: int,
    tol_sec: float,
    min_events: int,
    p_window: float,
) -> Optional[tuple[float, int, int, float]]:
    """Find the time of *cycle* at which episode *starts* recur least by chance.

    *starts* and *minutes* (recorded minutes) are on the local clock.  Every
    start's time of cycle is tried as a candidate.  Returns ``(phase, count,
    cycles, chance)`` with *phase* in seconds into the cycle, or ``None``.
    """
    recorded = Counter(m % (cycle // 60) for m in minutes)
    # Copies one cycle earlier and later let a window around 23:59 (:59) see
    # the starts just after 00:00 (:00).
    by_phase = sorted(
        (s % cycle + shift, s) for s in starts for shift in (-cycle, 0, cycle)
    )
    phases = [p for p, _ in by_phase]
    # Any window ±tol_sec around a candidate lies inside one of cycle/tol_sec
    # fixed windows 3 * tol_sec wide, so summing their tails bounds the chance
    # of finding such a time anywhere in the cycle.
    windows = cycle / tol_sec
    best: Optional[tuple[float, int, int, float]] = None
    for phase in sorted({s % cycle for s in starts}):
        first: dict[int, float] = {}
        lo = bisect_left(phases, phase - tol_sec)
        hi = bisect_right(phases, phase + tol_sec)
        for p, s in by_phase[lo:hi]:
            offset = p - phase
            n = round((s - offset - phase) / cycle)
            first[n] = min(first.get(n, offset), offset)
        count = len(first)
        cycles = max(recorded[int(phase // 60)], count)
        if count < min_events or count < _MIN_RECURRENCE * cycles:
            continue
        chance = min(1.0, windows * _binomial_tail(cycles, p_window, count))
        if chance >= _MAX_CHANCE or (best is not None and chance >= best[3]):
            continue
        offsets = sorted(first.values())
        # Spikes trail the scheduled start by seconds to a minute; the lower
        # quartile stays near the start without following one early stray.
        best = ((phase + offsets[(count - 1) // 4]) % cycle, count, cycles, chance)
    return best


def _binomial_tail(n: int, p: float, k: int) -> float:
    """P(X >= k) for X ~ Binomial(n, p), 1 <= k <= n, p > 0, in log space."""
    if p >= 1.0:
        return 1.0
    pmf = math.exp(
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log1p(-p)
    )
    total = 0.0
    for i in range(k, n + 1):
        total += pmf
        pmf *= (n - i) / (i + 1) * p / (1.0 - p)
    return min(1.0, total)


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
