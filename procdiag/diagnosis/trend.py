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
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import accumulate
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence

from ..core import Confidence, Finding, Measurement, Observation, Window, covers
from ..core.util import CMDLINE_SAMPLE_CHARS

if TYPE_CHECKING:
    from ..storage import ProcIdentity, Storage

# ---------------------------------------------------------------------------
# TrendFit — result of linear regression
# ---------------------------------------------------------------------------


@dataclass
class TrendFit:
    """Result of a least-squares linear fit on a time series.

    ``slope_per_hour`` is the rate of change in the same unit as the series
    values (e.g. bytes/hour for RSS).  ``r2`` quantifies how well a straight
    line explains the variance (0–1; 1 is perfect).  ``span_hours`` is the
    time the points cover, holes in the recording left out; ``step_seconds`` is
    the usual spacing of the points.
    """

    slope_per_hour: float
    r2: float
    n: int
    span_hours: float
    step_seconds: float = 60.0


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
# as disk rows or proc_counts this only adds leniency.  Each segment between
# holes in the recording loses that much at its edges.  Rows sampled less often
# than every minute (``process_interval``) lose two steps and a minute instead.
def _series_covers(
    span_hours: float, hours: float, segments: int = 1, step: float = 60.0
) -> bool:
    lost_minutes = 2 * max(step, 60.0) / 60.0 + 1
    return covers(span_hours + segments * lost_minutes / 60, hours)


# A line needs this many points whatever hours a rule asks for.
_FIT_MIN_POINTS = 12
# Past this long a recording holds the points of any sampling interval worth
# using, so it is never what a rule that finds none is short of.
_FIT_SHORTFALL_MAX_HOURS = 6.0


def fit_shortfall_hours(
    store: "Storage",
    window: "Window",
    *sources: tuple[str, str],
    min_points: int = _FIT_MIN_POINTS,
) -> float:
    """Hours of recording still missing before *window* can hold *min_points* points.

    *sources* are ``(table, timestamp column)``; the first with rows in the
    window gives the sampling interval.  *min_points* points stamped every step
    span ``min_points - 1`` steps.  0.0 once the window is that long, so a
    recording that has the points and still no line has nothing to wait for.
    Without this, a rule whose minimum hours were set below that would run, fit
    nothing, and read as healthy.
    """
    if window.observed_hours >= _FIT_SHORTFALL_MAX_HOURS:
        return 0.0
    for table, column in sources:
        if not store.has_table(table):
            continue
        stamps = [
            row[0]
            for row in store.conn.execute(
                f"SELECT DISTINCT {column} FROM {table}"
                f" WHERE {column} BETWEEN ? AND ? ORDER BY 1",
                (window.start_ts, window.end_ts),
            )
        ]
        if stamps:
            break
    else:
        return 0.0
    steps = sorted(b - a for a, b in zip(stamps, stamps[1:]))
    step = steps[len(steps) // 2] if steps else 60.0
    needed = (min_points - 1) * step
    return max(0.0, needed - window.observed_hours * 3600.0) / 3600.0


def fit_linear(
    series: list[tuple[float, float]],
    *,
    min_points: int = _FIT_MIN_POINTS,
    min_span_hours: float = 6.0,
    holes: Sequence[tuple[float, float]] = (),
) -> Optional[TrendFit]:
    """Fit a least-squares line to *(timestamp, value)* pairs.

    *holes* are the stretches with nothing recorded (``Window.holes``).  They
    cut the series into segments, and the slope is fitted within them: each
    segment keeps its own level, so a process that settles at another level
    after the PC was off overnight is not read as growing, and one that grows
    again every day is.  ``span_hours`` adds up the segments.

    Returns ``None`` when there are fewer than *min_points* samples or the
    observation span does not cover *min_span_hours* (see
    :func:`_series_covers`).

    ``slope_per_hour`` is in value-units per hour.  ``r2`` is the coefficient
    of determination: 0 = flat / random, 1 = perfect line.
    """
    pts = [(t, v) for t, v in series if v is not None]
    return fit_segments(
        _split_at_holes(pts, holes),
        min_points=min_points,
        min_span_hours=min_span_hours,
    )


def _split_at_holes(
    points: list[tuple[float, float]], holes: Sequence[tuple[float, float]]
) -> list[list[tuple[float, float]]]:
    hole_starts = [start for start, _ in holes]
    segments: dict[int, list[tuple[float, float]]] = {}
    for t, v in points:
        segments.setdefault(bisect_left(hole_starts, t), []).append((t, v))
    return list(segments.values())


def fit_segments(
    segments: Sequence[Sequence[tuple[float, float]]],
    *,
    min_points: int,
    min_span_hours: float,
) -> Optional[TrendFit]:
    """One slope through *segments* of a series, each at its own level.

    See :func:`fit_linear`; a segment is a stretch between holes in the
    recording, or one process of a program that was restarted.
    """
    segments = [seg for seg in segments if seg]
    n = sum(len(seg) for seg in segments)
    if n < min_points:
        return None

    span_hours = (
        sum(max(t for t, _ in seg) - min(t for t, _ in seg) for seg in segments)
        / 3600.0
    )
    steps = sorted(
        b[0] - a[0] for seg in segments for a, b in zip(seg, seg[1:]) if b[0] > a[0]
    )
    step = steps[len(steps) // 2] if steps else 60.0
    if not _series_covers(span_hours, min_span_hours, len(segments), step):
        return None

    ts_min = min(t for seg in segments for t, _ in seg)
    # Convert timestamps to hours relative to the earliest point so the
    # slope is already in (value / hour) without needing large-number arithmetic.
    hours = [[((t - ts_min) / 3600.0, v) for t, v in seg] for seg in segments]
    means = [
        (statistics.fmean(x for x, _ in seg), statistics.fmean(y for _, y in seg))
        for seg in hours
    ]

    ss_xy = sum(
        (x - mx) * (y - my) for seg, (mx, my) in zip(hours, means) for x, y in seg
    )
    ss_xx = sum((x - mx) ** 2 for seg, (mx, _) in zip(hours, means) for x, _ in seg)

    if ss_xx == 0.0:
        # Each segment is a single instant — degenerate; cannot fit a line.
        return None

    slope = ss_xy / ss_xx

    # R² = 1 - SS_res / SS_tot, both around each segment's own level
    ss_res = sum(
        (y - my - slope * (x - mx)) ** 2
        for seg, (mx, my) in zip(hours, means)
        for x, y in seg
    )
    ss_tot = sum((y - my) ** 2 for seg, (_, my) in zip(hours, means) for _, y in seg)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else 1.0

    return TrendFit(
        slope_per_hour=slope, r2=r2, n=n, span_hours=span_hours, step_seconds=step
    )


# ---------------------------------------------------------------------------
# Per-process series (shared by the regression-based leak rules)
# ---------------------------------------------------------------------------

# A process that ran for less than this, restarted within one stretch of the
# recording, is left out of its program's trend: a program restarted every few
# minutes grows while it starts up, not because it leaks.  One that ran through
# a short stretch on its own (a PC on for half an hour a day) still counts.
_MIN_RUN_HOURS = 1.0
# A process that starts less than this long before the last row of another
# process of its program continues it: a restart that starts the new process
# before the old one has exited.
_RESTART_OVERLAP_SEC = 300.0


@dataclass
class ProcInstance:
    """One running copy of a program, followed across its restarts.

    ``runs`` are its processes in the order they ran, each ``(pid, points)``.
    Two copies of a program that run side by side are two instances.  The pid
    is None for rows rolled up before procdiag kept processes apart.
    """

    proc_id: int
    runs: list[tuple[Optional[int], list[tuple[float, float]]]]

    @property
    def pid(self) -> Optional[int]:
        """The process running last."""
        return self.runs[-1][0]

    @property
    def pids(self) -> list[int]:
        return [pid for pid, _ in self.runs if pid is not None]

    @property
    def last_value(self) -> float:
        return self.runs[-1][1][-1][1]

    @property
    def last_ts(self) -> float:
        return self.runs[-1][1][-1][0]

    def fit(
        self,
        *,
        min_points: int,
        min_span_hours: float,
        holes: Sequence[tuple[float, float]] = (),
    ) -> Optional[TrendFit]:
        """The trend of the instance, each process at its own level."""
        hole_starts = [start for start, _ in holes]
        stretch = [bisect_left(hole_starts, points[0][0]) for _, points in self.runs]
        restarted = {k for k, n in Counter(stretch).items() if n > 1}
        return fit_segments(
            [
                segment
                for k, (_, points) in zip(stretch, self.runs)
                if k not in restarted
                or points[-1][0] - points[0][0] >= _MIN_RUN_HOURS * 3600.0
                for segment in _split_at_holes(points, holes)
            ],
            min_points=min_points,
            min_span_hours=min_span_hours,
        )


def proc_instances(
    rows: Iterable[Mapping[str, Any]], column: str
) -> list[ProcInstance]:
    """Follow the processes of each program in ``proc_rollup_1m`` *rows*.

    A process that starts once another process of the same program has stopped
    continues that one's instance (a restart), even if the old one's last row
    is up to ``_RESTART_OVERLAP_SEC`` later.  One that starts while every
    instance still runs is a new instance (another worker).  When several have
    stopped, the one that stopped last is continued.  Rows rolled up before
    procdiag kept processes apart (no PID) are one stretch of the program that
    a process with a PID continues whatever the clocks say.  Rows whose
    *column* is NULL are skipped.
    """
    runs: dict[tuple[int, Optional[int]], list[tuple[float, float]]] = {}
    for row in rows:
        v = row[column]
        if v is not None:
            runs.setdefault((row["proc_id"], row["pid"]), []).append(
                (row["ts_min"], float(v))
            )

    by_program: dict[int, list[ProcInstance]] = {}
    for (proc_id, pid), points in sorted(
        runs.items(),
        key=lambda kv: (kv[0][1] is not None, kv[1][0][0], kv[0][0], kv[0][1] or 0),
    ):
        instances = by_program.setdefault(proc_id, [])
        stopped = [
            i
            for i in instances
            if i.pid is None or i.last_ts <= points[0][0] + _RESTART_OVERLAP_SEC
        ]
        if stopped:
            max(stopped, key=lambda i: i.last_ts).runs.append((pid, points))
        else:
            instances.append(ProcInstance(proc_id, [(pid, points)]))
    return [i for instances in by_program.values() for i in instances]


def proc_trend_observations(
    rows: Iterable[Mapping[str, Any]],
    identities: Mapping[int, "ProcIdentity"],
    column: str,
    *,
    min_span_hours: float,
    unit: float = 1.0,
    holes: Sequence[tuple[float, float]] = (),
) -> list[Observation]:
    """One Observation per program instance whose *column* trend can be fitted.

    *identities* is ``Storage.proc_identities()``.
    """
    observations = []
    for instance in proc_instances(rows, column):
        fit = instance.fit(
            min_points=_FIT_MIN_POINTS, min_span_hours=min_span_hours, holes=holes
        )
        if fit is None:
            continue
        identity = identities.get(instance.proc_id)
        observations.append(
            Observation(
                subject=identity.name if identity else f"proc_{instance.proc_id}",
                identity=identity.cmdline_hash if identity else None,
                value=fit.slope_per_hour / unit,
                evidence={
                    "proc_id": instance.proc_id,
                    "pid": instance.pid,
                    "pids": instance.pids,
                    "last_seen": instance.last_ts,
                    "script": script_label(identity.cmdline) if identity else None,
                    "r2": fit.r2,
                    "n": fit.n,
                    "span_hours": fit.span_hours,
                    "step_seconds": fit.step_seconds,
                    "last_value": instance.last_value,
                },
            )
        )
    return observations


def proc_details(obs: Observation) -> dict[str, Any]:
    """The evidence that tells which process a per-process finding is about."""
    ev = obs.evidence
    return {
        "proc_id": ev["proc_id"],
        "process_name": obs.subject,
        "script": ev["script"],
        "pid": ev["pid"],
        "pids": ev["pids"],
        "last_seen": ev["last_seen"],
    }


# What a command-line argument must end in to be shown as the script a program
# runs.  Nothing else of the command line is shown: it may carry passwords.
_SCRIPT_SUFFIXES = (
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".rb",
    ".pl",
    ".php",
    ".sh",
    ".jar",
)


def script_label(cmdline: Optional[str]) -> Optional[str]:
    """The file name of the script *cmdline* runs, when it is safe to show.

    Only an argument that is a path (with a ``/``) to a file with a script
    extension counts, never one with ``=``, ``:`` or ``@`` in it, and only a
    file name without digits: *cmdline* is the first process's, and another
    process of the program may differ from it in digits.
    """
    if not cmdline:
        return None
    args = cmdline.split()
    if len(cmdline) >= CMDLINE_SAMPLE_CHARS:
        args = args[:-1]  # possibly cut short
    for arg in args:
        name = arg.rsplit("/", 1)[-1]
        if (
            "/" in arg
            and name.endswith(_SCRIPT_SUFFIXES)
            and not any(c in "=:@" for c in arg)
            and not any(c.isdigit() for c in name)
        ):
            return name
    return None


def proc_label(name: str, script: Optional[str], pid: Optional[int]) -> str:
    """``python3 uploader.py (PID 2102)``, as far as the script and PID are known."""
    label = f"{name} {script}" if script else name
    return label if pid is None else f"{label} (PID {pid})"


# PIDs a finding lists of the other processes of its program that matched too.
_OTHER_PIDS_SHOWN = 5


def worst_process_per_program(measured: Iterable[Measurement]) -> list[Finding]:
    """The findings of per-process *measured*, one per program.

    A program is ``evidence["proc_id"]``: the processes of one program (a pool
    of workers) are judged one by one but reported as one finding, that of the
    process with the largest measured value.  It counts the other processes
    that fired and lists a few of their PIDs.  Rows rolled up before procdiag
    kept processes apart have no PID and are not counted.
    """
    firing = [(m.observation.value, m.finding) for m in measured if m.finding]
    firing.sort(key=lambda pair: pair[0], reverse=True)
    by_program: dict[int, list[Finding]] = {}
    for _, finding in firing:
        by_program.setdefault(finding.evidence["proc_id"], []).append(finding)
    kept = []
    for worst, *others in by_program.values():
        pids = list(
            dict.fromkeys(
                f.evidence["pid"]
                for f in others
                if f.evidence["pid"] not in (None, worst.evidence["pid"])
            )
        )
        if pids:
            shown = pids[:_OTHER_PIDS_SHOWN]
            listed = ", ".join(map(str, shown)) + (
                " ほか" if len(pids) > len(shown) else ""
            )
            worst = replace(
                worst,
                summary=f"{worst.summary}。他 {len(pids)} プロセスも同様 (PID {listed})",
                evidence={
                    **worst.evidence,
                    "other_processes": len(pids),
                    "other_pids": shown,
                },
            )
        kept.append(worst)
    return kept


def observed_fit(obs: Observation, unit: float = 1.0) -> TrendFit:
    ev = obs.evidence
    return TrendFit(
        obs.value * unit,
        ev["r2"],
        ev["n"],
        ev["span_hours"],
        ev.get("step_seconds", 60.0),
    )


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

    def covered(hours: float) -> bool:
        return _series_covers(fit.span_hours, hours, step=fit.step_seconds)

    if fit.r2 >= high_r2 and covered(high_span_hours):
        return Confidence.HIGH
    if fit.r2 >= medium_r2 and covered(min_observation_hours):
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


@dataclass
class Forecast:
    """When a growing value reaches its capacity.

    ``per_day`` is the growth per day of the fit behind ``days_left``, so the
    two always agree: ``days_left * per_day`` is what was left.  ``one_step``
    says most of the growth happened within a short time and the value has not
    risen since, so the line through it says how fast the one step was, not how
    fast the value grows.
    """

    days_left: float
    per_day: float
    one_step: bool = False


# A growth is one step when this share of it falls in a window of this share of
# the series' time and the value then stays where the step took it, for at least
# as long as the window: it rises by less than this share of the step again.
# Growth that goes on to the last samples, or comes and goes as a log does, is a
# trend, not a step.
_ONE_STEP_SHARE = 0.7
_ONE_STEP_WINDOW = 1 / 6
_ONE_STEP_AFTERMATH = 0.1


def _biggest_rise(
    points: Sequence[tuple[float, float]], width: float
) -> tuple[float, int]:
    """The biggest rise of the value within *width* seconds, and where it ends.

    Returns the rise and the index of the first point at the value it rises to.
    """
    best, best_end = 0.0, 0
    after: deque[int] = deque()  # indexes within *width* after i, falling values
    last = 0
    for i, (t, value) in enumerate(points):
        while last + 1 < len(points) and points[last + 1][0] - t <= width:
            last += 1
            while after and points[after[-1]][1] < points[last][1]:
                after.pop()
            after.append(last)
        while after and after[0] <= i:
            after.popleft()
        if after and points[after[0]][1] - value > best:
            best, best_end = points[after[0]][1] - value, after[0]
    return best, best_end


def _largest_rise(points: Sequence[tuple[float, float]]) -> float:
    """The most the value rises from an earlier point to a later one."""
    low, best = points[0][1], 0.0
    for _, value in points:
        low = min(low, value)
        best = max(best, value - low)
    return best


def _is_one_step(points: Sequence[tuple[float, float]]) -> bool:
    """Whether *points* rose mostly within a sixth of their time, and then stayed.

    The value that follows the step may fall (an archive unpacked and removed),
    but must not rise by more than a tenth of the step: a log that is rotated
    rises again after each drop, and growth that has just begun rises up to the
    last sample.
    """
    width = (points[-1][0] - points[0][0]) * _ONE_STEP_WINDOW
    total = points[-1][1] - points[0][1]
    if total <= 0 or width <= 0:
        return False
    rise, end = _biggest_rise(points, width)
    if rise < _ONE_STEP_SHARE * total:
        return False
    after = points[end:]
    return (
        after[-1][0] - after[0][0] >= width
        and _largest_rise(after) <= _ONE_STEP_AFTERMATH * rise
    )


def forecast_full(
    series: list[tuple[float, float]],
    capacity: float,
    *,
    min_points: int = 12,
) -> Optional[Forecast]:
    """Extrapolate a *used* series to predict when it hits *capacity*.

    ``days_left`` counts from the last observation (0.0 when it is already at
    capacity).  ``None`` when the slope is ≤ 0 (shrinking or flat) or data is
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

    per_day = fit.slope_per_hour * 24.0
    remaining = max(0.0, capacity - work_pts[-1][1])
    return Forecast(
        days_left=remaining / per_day,
        per_day=per_day,
        one_step=_is_one_step(work_pts),
    )


def extrapolate_full(
    series: list[tuple[float, float]],
    capacity: float,
    *,
    min_points: int = 12,
) -> Optional[float]:
    """The ``days_left`` of :func:`forecast_full`."""
    forecast = forecast_full(series, capacity, min_points=min_points)
    return None if forecast is None else forecast.days_left


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
# Random load is often confined to working hours or breaks, so the chance of
# a start near a candidate time comes from the hour before or after it when
# either rate is above the overall one.  One side alone still sees the busy
# hour when the candidate sits at the edge of a shift or a break.
_NEARBY_SEC = 3600.0


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
    probability below 1 %.  That chance uses the highest of the start rates in
    the hour before the candidate time, the hour after it, and overall.
    Days and hours wrap: a start at 00:00:20 matches an anchor of 23:59:40.
    *tol_sec* is kept between 1 second (the sampling step) and a third of
    the day (hour), beyond which every start would match every time.

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

    for kind, cycle in (("hourly", 3600), ("daily", 86400)):
        tol = min(tol_sec, cycle / 3) if tol_sec >= 1.0 else 1.0
        found = _recurring_time(starts, minutes, cycle, tol, min_events)
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
) -> Optional[tuple[float, int, int, float]]:
    """Find the time of *cycle* at which episode *starts* recur least by chance.

    *starts* and *minutes* (recorded minutes) are on the local clock.  Every
    start's time of cycle is tried as a candidate.  Returns ``(phase, count,
    cycles, chance)`` with *phase* in seconds into the cycle, or ``None``.
    """
    per_cycle = cycle // 60
    recorded = Counter(m % per_cycle for m in minutes)
    running = list(
        accumulate((recorded[m % per_cycle] for m in range(3 * per_cycle)), initial=0)
    )

    def rate_between(lo: float, hi: float) -> float:
        """Starts per recorded second at times of cycle in [lo, hi)."""
        span = running[int(hi // 60) + per_cycle] - running[int(lo // 60) + per_cycle]
        n = bisect_left(phases, hi) - bisect_left(phases, lo)
        return n / (60.0 * span) if span > 0 else 0.0

    overall_rate = len(starts) / (max(len(minutes), 1) * 60.0)
    nearby = min(_NEARBY_SEC, cycle / 2)
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
        # The candidate's own starts are left out: a real job would otherwise
        # raise the rate it is judged against.
        rate = max(
            overall_rate,
            rate_between(phase - nearby, phase - tol_sec),
            rate_between(phase + tol_sec, phase + nearby),
        )
        # Chance that one day (hour) holds a start inside a fixed window
        # 3 * tol_sec wide, if starts fell at random times at that rate.
        p_window = min(1.0, rate * 3 * tol_sec)
        chance = min(1.0, windows * _binomial_tail(cycles, p_window, count))
        if chance >= _MAX_CHANCE or (best is not None and chance >= best[3]):
            continue
        offsets = sorted(first.values())
        # Spikes trail the scheduled start by seconds to a minute; the lower
        # quartile stays near the start without following one early stray.
        best = ((phase + offsets[(count - 1) // 4]) % cycle, count, cycles, chance)
    return best


def _binomial_tail(n: int, p: float, k: int) -> float:
    """P(X >= k) for X ~ Binomial(n, p), 1 <= k <= n, p > 0, in log space.

    At or below the mean the result is at least about one half; 1.0 is
    returned there, because summing up from pmf(k) underflows to 0 when k is
    far below the mode.
    """
    if p >= 1.0 or k <= n * p:
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


def detect_recurrence(
    events_by_day: dict[str, bool], weights: Optional[Mapping[str, float]] = None
) -> float:
    """Return the fraction of days on which the event occurred.

    *events_by_day* maps date-string keys (YYYY-MM-DD) to a boolean indicating
    whether the event occurred on that day.  Used by MEM-PRESSURE-01 to
    measure how often chronic memory pressure was observed.  A day counts as
    much as its entry in *weights* (1.0 when it has none), so a day observed
    for only part of it, without the event, says less against it.

    Returns 0.0 for an empty dict.
    """
    if not events_by_day:
        return 0.0
    weights = weights or {}
    total = sum(weights.get(day, 1.0) for day in events_by_day)
    hits = sum(weights.get(day, 1.0) for day, hit in events_by_day.items() if hit)
    return hits / total if total > 0 else 0.0


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
