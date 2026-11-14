"""Memory diagnostic rules.

Currently implements:

- **MEM-LEAK-01**: Detect monotonically growing RSS in a process over the
  observation window using linear regression (via ``trend.fit_linear``).
  Fires WARNING when slope > 1 MB/h and R² > 0.9; CRITICAL when the slope
  exceeds the critical threshold.  Confidence is derived from R² and
  observation time.

- **MEM-PRESSURE-01**: Detect chronic memory pressure: ``psi_mem_max`` in
  ``sys_rollup_1m`` stays above a threshold for a sustained time, with swap-out
  at the same time, on most local days that were observed long enough
  (using ``trend.detect_recurrence``).  Fires WARNING.

- **SWAP-THRASH-01**: Detect simultaneous swap-in and swap-out in the same
  time window combined with ``psi_mem_full10 > 0``, indicating that the kernel
  is thrashing pages.  Isolated events far apart are separate episodes.
  Fires CRITICAL.

Design doc reference: §6.2 MEM-LEAK-01 / MEM-PRESSURE-01 / SWAP-THRASH-01.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional

from ..core import (
    Capability,
    Confidence,
    Finding,
    Headline,
    MeasuredRule,
    MiB,
    Observation,
    Rule,
    Severity,
    Window,
    register,
)
from ..core.util import humanize_bytes
from .trend import (
    baseline_deviation,
    confidence_from_fit,
    detect_recurrence,
    fit_each,
    fit_linear,
    group_series,
    learn_baseline,
    observed_fit,
    proc_trend_observations,
)

if TYPE_CHECKING:
    from ..storage import Storage

# ---------------------------------------------------------------------------
# Thresholds — exposed as class attributes so tests can inject alternatives
# ---------------------------------------------------------------------------

# Minimum slope (MB/h) to emit a WARNING.
_MEM_LEAK_SLOPE_WARNING_MB_H: float = 1.0
# Minimum slope (MB/h) to escalate to CRITICAL.
_MEM_LEAK_SLOPE_CRITICAL_MB_H: float = 50.0
# Minimum R² for the regression to be considered reliable.
_MEM_LEAK_MIN_R2: float = 0.9

# PSS-based confidence promotion / demotion (EXT-02): needs this many PSS
# samples; a PSS slope above HIGH confirms the leak, one at/below FLAT
# suggests the RSS growth is shared memory / page cache instead.
_PSS_MIN_POINTS = 12
_PSS_CONFIRM_R2 = 0.8
_PSS_SLOPE_HIGH_MB_H = 0.5
_PSS_SLOPE_FLAT_MB_H = 0.1

# ---------------------------------------------------------------------------
# PSS scrutiny pre-check (run hourly by the recorder)
# ---------------------------------------------------------------------------

# smaps_rollup is too expensive to read for every process, so the recorder
# periodically picks a few MEM-LEAK candidates from the recent RSS rollups and
# reads PSS only for them.  The bar is deliberately lower than MEM-LEAK-01's
# (shorter window, R² 0.8) so PSS evidence is already being collected by the
# time the rule itself can fire.
PSS_PRECHECK_WINDOW_HOURS = 6.0
_PSS_PRECHECK_MIN_POINTS = 60
_PSS_PRECHECK_MIN_SPAN_HOURS = 2.0
_PSS_PRECHECK_MIN_R2 = 0.8
_PSS_PRECHECK_SLOPE_MB_H = 1.0
_PSS_MAX_TARGETS = 5


def select_pss_targets(proc_rollup_rows: Iterable[Mapping[str, Any]]) -> list[int]:
    """Pick the proc_ids whose RSS grows fastest, as PSS scrutiny targets.

    *proc_rollup_rows* are ``proc_rollup_1m`` rows covering the last
    ``PSS_PRECHECK_WINDOW_HOURS``.  Returns at most ``_PSS_MAX_TARGETS``
    proc_ids, steepest RSS slope first.
    """
    candidates: list[tuple[float, int]] = []  # (slope_mb_h, proc_id)
    for proc_id, _series, fit in fit_each(
        group_series(proc_rollup_rows, key="proc_id", value="rss_avg"),
        min_points=_PSS_PRECHECK_MIN_POINTS,
        min_span_hours=_PSS_PRECHECK_MIN_SPAN_HOURS,
        min_r2=_PSS_PRECHECK_MIN_R2,
    ):
        slope_mb_h = fit.slope_per_hour / MiB
        if slope_mb_h >= _PSS_PRECHECK_SLOPE_MB_H:
            candidates.append((slope_mb_h, proc_id))
    candidates.sort(reverse=True)
    return [proc_id for _, proc_id in candidates[:_PSS_MAX_TARGETS]]


def _is_python_process(name: str) -> bool:
    """Return True when *name* looks like a CPython interpreter.

    Matches ``python`` / ``python3`` / ``python3.11`` etc. but not unrelated
    names that merely contain ``py`` (``copy``, ``scapy``, ``pypy``), which the
    old substring heuristic wrongly flagged for a py-spy suggestion.
    """
    return name.lower().startswith("python")


@register
class MemLeakRule(MeasuredRule):
    """Detect per-process memory leaks via linear regression on RSS rollups.

    Queries ``proc_rollup_1m`` for each registered process, fits a line to
    the ``rss_avg`` time series, and fires when the slope and R² both exceed
    their thresholds.
    """

    rule_id = "MEM-LEAK-01"
    category = "memory"
    title = "メモリリーク疑い"
    default_severity = Severity.WARNING
    min_observation_hours = 6.0
    requires_rows = ("proc_rollup_1m",)
    headline = Headline("RSS 増加率", "MB/h", threshold="slope_warning_mb_h")
    what = "proc_rollup_1m の rss_avg に線形回帰をかけ、増加傾向と R² で判定する"
    why = (
        "リークは『右肩上がりが続く』こと自体が証拠。"
        "R² > 0.9 は偶発的なスパイクでなく持続的増加であることを担保する"
    )
    manual_commands = [
        "watch -n 60 'ps -o rss=,vsz= -p <PID>'",
        "smem -P <name>",
    ]

    # Allow tests to override thresholds on the class.
    slope_warning_mb_h: float = _MEM_LEAK_SLOPE_WARNING_MB_H
    slope_critical_mb_h: float = _MEM_LEAK_SLOPE_CRITICAL_MB_H
    min_r2: float = _MEM_LEAK_MIN_R2

    def observe(self, store: "Storage", window: Window) -> list[Observation]:
        return proc_trend_observations(
            store.rows("proc_rollup_1m", since=window.start_ts, until=window.end_ts),
            store.proc_identities(),
            "rss_avg",
            min_span_hours=self.min_observation_hours,
            unit=MiB,
            holes=window.holes,
        )

    def judge(
        self, store: "Storage", window: Window, obs: Observation
    ) -> Optional[Finding]:
        fit = observed_fit(obs, MiB)
        slope_mb_h = obs.value
        if fit.r2 < self.min_r2 or slope_mb_h < self.slope_warning_mb_h:
            return None

        proc_id = obs.evidence["proc_id"]
        process_name = obs.subject

        severity = (
            Severity.CRITICAL
            if slope_mb_h >= self.slope_critical_mb_h
            else Severity.WARNING
        )

        confidence = confidence_from_fit(
            fit, min_observation_hours=self.min_observation_hours
        )

        # Days until this process alone eats the memory that is still available
        # at the end of the window (MemAvailable of the last sys_sample).
        sys_rows = store.rows("sys_samples", since=window.start_ts, until=window.end_ts)
        mem_available = sys_rows[-1]["mem_available"] if sys_rows else None
        days_to_oom = (
            max(mem_available, 0) / (MiB * slope_mb_h * 24.0)
            if mem_available is not None and slope_mb_h > 0
            else None
        )

        evidence = {
            "proc_id": proc_id,
            "process_name": process_name,
            "slope_mb_per_h": slope_mb_h,
            "r2": fit.r2,
            "n": fit.n,
            "span_hours": fit.span_hours,
            "is_python": _is_python_process(process_name),
            "days_to_oom": days_to_oom,
            "mem_available_mb": (
                None if mem_available is None else round(mem_available / MiB, 1)
            ),
        }

        confidence = self._apply_pss_evidence(
            store, window, proc_id, confidence, evidence
        )

        try:
            boot_time: float | None = float(store.meta().get("boot_time", "")) or None
        except (TypeError, ValueError):
            boot_time = None
        if boot_time is not None:
            first_seen = store.proc_first_seen(proc_id)
            if first_seen is not None and first_seen < boot_time:
                evidence["predates_current_boot"] = True
                if confidence == Confidence.MEDIUM:
                    confidence = Confidence.HIGH

        summary = (
            f"{process_name}: RSS +{slope_mb_h:.1f} MB/h で単調増加"
            f" (R²={fit.r2:.2f}, {fit.span_hours:.1f}h観測)"
        )

        return Finding(
            rule_id=self.rule_id,
            severity=severity,
            subject=process_name,
            summary=summary,
            evidence=evidence,
            detected_at=(window.start_ts, window.end_ts),
            confidence=confidence,
        )

    @staticmethod
    def _apply_pss_evidence(
        store: "Storage",
        window: Window,
        proc_id: int,
        confidence: Confidence,
        evidence: dict,
    ) -> Confidence:
        """Promote / demote *confidence* from the PSS series (EXT-02).

        PSS is only sampled for processes the recorder's pre-check selected
        (see :func:`select_pss_targets`).  A clearly rising PSS confirms the
        leak (HIGH); a flat PSS means the RSS growth is likely shared memory or
        page cache (LOW).  Without enough PSS samples *confidence* is returned
        unchanged.  *evidence* is annotated in place.
        """
        pss_series = store.series(
            "proc_samples",
            "pss",
            subject_col="proc_id",
            subject=proc_id,
            since=window.start_ts,
            until=window.end_ts,
        )
        if len(pss_series) < _PSS_MIN_POINTS:
            return confidence
        pss_fit = fit_linear(
            pss_series,
            min_points=_PSS_MIN_POINTS,
            min_span_hours=0.0,
            holes=window.holes,
        )
        if pss_fit is None:
            return confidence
        pss_slope_mb_h = pss_fit.slope_per_hour / MiB
        if pss_fit.r2 > _PSS_CONFIRM_R2 and pss_slope_mb_h > _PSS_SLOPE_HIGH_MB_H:
            evidence["pss_slope_mb_per_h"] = round(pss_slope_mb_h, 3)
            return Confidence.HIGH
        if pss_slope_mb_h <= _PSS_SLOPE_FLAT_MB_H:
            evidence["pss_note"] = (
                "PSSでは増加が確認できず (共有メモリ/キャッシュの可能性)"
            )
            return Confidence.LOW
        return confidence


# ---------------------------------------------------------------------------
# Thresholds for MEM-PRESSURE-01
# ---------------------------------------------------------------------------

_MEM_PRESSURE_PSI_THRESHOLD: float = (
    5.0  # psi_mem_max > this value is a pressure minute
)
# A local day counts as observed only when it has this many hours of PSI rows,
# so the slivers at the edges of a recording are not days.
_MEM_PRESSURE_MIN_DAY_HOURS: float = 6.0
# Consecutive pressure minutes for a pressure day: chronic pressure lasts, a
# blip or scattered minutes do not.
_MEM_PRESSURE_MIN_MINUTES: int = 10
# Swap-out within a minute of such a run (a stray page is not).
_MEM_PRESSURE_MIN_SWAP_OUT: int = MiB
# Pressure days needed, and the share of the observed days they must be.
_MEM_PRESSURE_MIN_DAYS: int = 2
_MEM_PRESSURE_MIN_RECURRENCE: float = 0.6


def _local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _observed_minutes_by_day(
    rollup_rows: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    """PSI-bearing ``sys_rollup_1m`` rows per local calendar day."""
    counts: dict[str, int] = {}
    for row in rollup_rows:
        if row["psi_mem_max"] is not None:
            day = _local_day(row["ts_min"])
            counts[day] = counts.get(day, 0) + 1
    return counts


def _consecutive_runs(buckets: list[int]) -> list[list[int]]:
    """Group sorted minute buckets into runs of consecutive minutes."""
    runs: list[list[int]] = []
    for bucket in buckets:
        if runs and bucket == runs[-1][-1] + 1:
            runs[-1].append(bucket)
        else:
            runs.append([bucket])
    return runs


@register
class MemPressureRule(Rule):
    """Detect chronically recurring memory pressure with swap activity.

    Queries ``sys_rollup_1m`` and groups the rows by the host's local calendar
    day.  A day is *observed* with at least ``min_day_observed_hours`` of PSI
    rows, and a *pressure day* when ``psi_mem_max`` exceeds the threshold for at
    least ``min_pressure_minutes`` minutes in a row and ``min_swap_out_bytes``
    were swapped out within a minute of that run.  Fires WARNING when at least
    ``min_pressure_days`` days are pressure days and they are at least
    ``min_recurrence_fraction`` of the observed days.  A recording with fewer
    than ``min_pressure_days`` observed days is reported as not evaluable
    (``recording_shortfall_hours``), never as healthy.

    PSI columns absent from the DB (all None) → silent (no finding).
    """

    rule_id = "MEM-PRESSURE-01"
    category = "memory"
    title = "慢性メモリ圧迫"
    default_severity = Severity.WARNING
    min_observation_hours = 24.0
    requires_rows = ("sys_rollup_1m", "sys_rollup_1m.psi_mem_max")
    requires_observed = (Capability.PSI,)
    what = (
        "sys_rollup_1m の psi_mem_max が閾値超の状態が 1 日に一定時間以上途切れず続き、"
        "その前後の swap_out_sum も一定量以上ある日が、観測が十分なローカル日の"
        "過半数で反復するかを判定する"
    )
    why = (
        "断続的なメモリ圧力はスパイクとして見えず、PSI の反復パターンで初めて"
        "顕在化する。圧迫中の swap_out がページアウト圧力の物的証拠となる。"
        "一瞬の圧迫や無関係なスワップでは判定しない"
    )
    manual_commands = [
        "free -h",
        "vmstat 1 10",
        "sar -B 1 10",
        "cat /proc/pressure/memory",
    ]

    psi_threshold: float = _MEM_PRESSURE_PSI_THRESHOLD
    min_recurrence_fraction: float = _MEM_PRESSURE_MIN_RECURRENCE
    min_day_observed_hours: float = _MEM_PRESSURE_MIN_DAY_HOURS
    min_pressure_minutes: int = _MEM_PRESSURE_MIN_MINUTES
    min_swap_out_bytes: int = _MEM_PRESSURE_MIN_SWAP_OUT
    min_pressure_days: int = _MEM_PRESSURE_MIN_DAYS

    def recording_shortfall_hours(self, store: "Storage", window: Window) -> float:
        """Hours still to record before ``min_pressure_days`` days are observed.

        A day needs ``min_day_observed_hours`` of PSI rows to count, so a
        recording of 24 hours can hold a single observed day, and "recurs on
        most days" cannot be judged from it.  The answer assumes the recording
        goes on without gaps.
        """
        rows = store.rows("sys_rollup_1m", since=window.start_ts, until=window.end_ts)
        observed = _observed_minutes_by_day(rows)
        if not observed:
            return 0.0
        need = self.min_day_observed_hours * 60

        def enough() -> bool:
            return sum(1 for m in observed.values() if m >= need) >= (
                self.min_pressure_days
            )

        if enough():
            return 0.0
        last = max(r["ts_min"] for r in rows if r["psi_mem_max"] is not None)
        for minute in range(1, 4 * 24 * 60):
            day = _local_day(last + minute * 60)
            observed[day] = observed.get(day, 0) + 1
            if enough():
                return max((last + minute * 60 - window.end_ts) / 3600.0, 1 / 60)
        return 4 * 24.0

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run MEM-PRESSURE-01 against the observation window."""
        rollup_rows = store.rows(
            "sys_rollup_1m",
            since=window.start_ts,
            until=window.end_ts,
        )
        if not rollup_rows:
            return []

        # Detect PSI absence: if all psi_mem_max values are None, stay silent.
        all_psi_none = all(r["psi_mem_max"] is None for r in rollup_rows)
        if all_psi_none:
            return []

        observed_minutes = _observed_minutes_by_day(rollup_rows)
        swap_out_by_bucket: dict[int, int] = {}
        pressure_buckets: set[int] = set()
        for row in rollup_rows:
            bucket = int(row["ts_min"] // 60)
            swap_out_by_bucket[bucket] = swap_out_by_bucket.get(bucket, 0) + (
                row["swap_out_sum"] or 0
            )
            psi = row["psi_mem_max"]
            if psi is not None and psi > self.psi_threshold:
                pressure_buckets.add(bucket)

        sustained_by_day: dict[str, set[int]] = {}
        for run in _consecutive_runs(sorted(pressure_buckets)):
            if len(run) >= self.min_pressure_minutes:
                for bucket in run:
                    sustained_by_day.setdefault(_local_day(bucket * 60), set()).add(
                        bucket
                    )

        pressure_minutes: dict[str, int] = {}
        pressure_swap_out: dict[str, int] = {}
        events_by_day: dict[str, bool] = {}
        for day, minutes in observed_minutes.items():
            if minutes < self.min_day_observed_hours * 60:
                continue
            sustained = sustained_by_day.get(day, set())
            near = {b + d for b in sustained for d in (-1, 0, 1)}
            swap_out = sum(swap_out_by_bucket.get(b, 0) for b in near)
            events_by_day[day] = (
                len(sustained) >= self.min_pressure_minutes
                and swap_out >= self.min_swap_out_bytes
            )
            pressure_minutes[day] = len(sustained)
            pressure_swap_out[day] = swap_out

        n_days = len(events_by_day)
        pressure_days = sorted(d for d, hit in events_by_day.items() if hit)
        n_pressure_days = len(pressure_days)
        if n_days < self.min_pressure_days or n_pressure_days < self.min_pressure_days:
            return []
        recurrence = detect_recurrence(events_by_day)
        if recurrence < self.min_recurrence_fraction:
            return []

        total_swap_out = sum(pressure_swap_out[d] for d in pressure_days)
        total_pressure_minutes = sum(pressure_minutes[d] for d in pressure_days)

        # Compute representative peak PSI for the summary.
        peak_psi = max(
            (r["psi_mem_max"] for r in rollup_rows if r["psi_mem_max"] is not None),
            default=0.0,
        )

        evidence = {
            "psi_threshold": self.psi_threshold,
            "peak_psi_mem_max": round(peak_psi, 2),
            "recurrence_fraction": round(recurrence, 2),
            "n_pressure_days": n_pressure_days,
            "n_total_days": n_days,
            "pressure_days": pressure_days,
            "swap_out_total_bytes": total_swap_out,
            "pressure_event_count": total_pressure_minutes,
            "min_pressure_minutes": self.min_pressure_minutes,
            "min_swap_out_bytes": self.min_swap_out_bytes,
            "min_day_observed_hours": self.min_day_observed_hours,
        }

        # Annotate the peak against this host's own usual level for the same
        # weekday/hour (evidence only — does not change detection).  Needs
        # enough per-time-bin history, so it stays silent on short windows.
        psi_series = [
            (r["ts_min"], r["psi_mem_max"])
            for r in rollup_rows
            if r["psi_mem_max"] is not None
        ]
        peak_ts = max(
            (r["ts_min"] for r in rollup_rows if r["psi_mem_max"] == peak_psi),
            default=window.end_ts,
        )
        dev = baseline_deviation(learn_baseline(psi_series), peak_ts, peak_psi)
        if dev is not None:
            evidence["baseline_deviation_mads"] = round(dev, 1)

        summary = (
            f"慢性メモリ圧迫: psi_mem_max > {self.psi_threshold} が"
            f" {self.min_pressure_minutes}分以上途切れず続く日が {n_pressure_days}/{n_days} 日で反復"
            f" (圧迫中のスワップアウト {humanize_bytes(total_swap_out)})"
        )

        confidence = (
            Confidence.HIGH if recurrence >= 0.8 and n_days >= 3 else Confidence.MEDIUM
        )

        return [
            Finding(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                subject="mem_pressure/daily",
                summary=summary,
                evidence=evidence,
                detected_at=(window.start_ts, window.end_ts),
                confidence=confidence,
            )
        ]


# ---------------------------------------------------------------------------
# Thresholds for SWAP-THRASH-01
# ---------------------------------------------------------------------------

_SWAP_THRASH_MIN_SWAP_IN: int = 1  # any swap-in activity
_SWAP_THRASH_MIN_SWAP_OUT: int = 1  # any swap-out activity
# Thrash samples further apart than this belong to different episodes.
_SWAP_THRASH_EPISODE_GAP_SECONDS: float = 300.0


@register
class SwapThrashRule(Rule):
    """Detect simultaneous swap-in + swap-out combined with full-stall PSI.

    Scans ``sys_samples`` for rows where both ``swap_in > 0`` and
    ``swap_out > 0`` co-occur with ``psi_mem_full10 > 0``.  A single such
    event is sufficient for CRITICAL — simultaneous in/out with full memory
    stall is unambiguously pathological.

    PSI columns absent from the DB (all None) → silent.
    """

    rule_id = "SWAP-THRASH-01"
    category = "memory"
    title = "スワップスラッシング"
    default_severity = Severity.CRITICAL
    min_observation_hours = 0.0  # even a single event is worth reporting
    requires_rows = ("sys_samples", "sys_samples.psi_mem_full10")
    requires_observed = (Capability.PSI,)
    what = (
        "sys_samples で swap_in > 0 かつ swap_out > 0 かつ psi_mem_full10 > 0 が"
        "同一サンプルで同時発生したかを検出する"
    )
    why = (
        "swap_in と swap_out の同時発生はカーネルが使えるメモリを使い果たし"
        "ページを双方向に入れ替えているスラッシング状態の確証。"
        "psi_mem_full10 > 0 は全タスクが I/O 待ちであることを示す"
    )
    manual_commands = [
        "free -h",
        "vmstat 1 10",
        "sar -W 1 10",
        "cat /proc/pressure/memory",
    ]

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run SWAP-THRASH-01 against the observation window."""
        raw_rows = store.rows(
            "sys_samples",
            since=window.start_ts,
            until=window.end_ts,
        )
        if not raw_rows:
            return []

        # Detect PSI absence: if all psi_mem_full10 values are None, stay silent.
        all_psi_none = all(r["psi_mem_full10"] is None for r in raw_rows)
        if all_psi_none:
            return []

        thrash_rows = [
            r
            for r in raw_rows
            if (r["swap_in"] or 0) > 0
            and (r["swap_out"] or 0) > 0
            and (r["psi_mem_full10"] or 0.0) > 0.0
        ]

        if not thrash_rows:
            return []

        # swap_in / swap_out are byte counts (psutil swap_memory sin/sout deltas).
        total_swap_in = sum(r["swap_in"] or 0 for r in thrash_rows)
        total_swap_out = sum(r["swap_out"] or 0 for r in thrash_rows)
        peak_psi_full = max(r["psi_mem_full10"] or 0.0 for r in thrash_rows)
        thrash_start = min(r["ts"] for r in thrash_rows)
        thrash_end = max(r["ts"] for r in thrash_rows)

        episode_minutes: list[int] = []
        first = prev = thrash_rows[0]["ts"]
        for row in thrash_rows[1:]:
            if row["ts"] - prev > _SWAP_THRASH_EPISODE_GAP_SECONDS:
                episode_minutes.append(max(1, round((prev - first) / 60.0) + 1))
                first = row["ts"]
            prev = row["ts"]
        episode_minutes.append(max(1, round((prev - first) / 60.0) + 1))
        duration_minutes = sum(episode_minutes)
        episode_count = len(episode_minutes)

        evidence = {
            "thrash_event_count": len(thrash_rows),
            "swap_in_total_bytes": total_swap_in,
            "swap_out_total_bytes": total_swap_out,
            "peak_psi_mem_full10": round(peak_psi_full, 2),
            "period_start": thrash_start,
            "period_end": thrash_end,
            "episode_count": episode_count,
            "longest_episode_minutes": max(episode_minutes),
            "duration_minutes": duration_minutes,
        }

        summary = (
            f"スワップスラッシング: {len(thrash_rows)} サンプルで swap_in/out 同時発生"
            f" ({episode_count}回、合計{duration_minutes}分間、"
            f"psi_mem_full10 最大 {peak_psi_full:.1f}%)"
        )

        return [
            Finding(
                rule_id=self.rule_id,
                severity=Severity.CRITICAL,
                subject="swap_thrash",
                summary=summary,
                evidence=evidence,
                detected_at=(thrash_start, thrash_end),
                confidence=Confidence.HIGH,
            )
        ]
