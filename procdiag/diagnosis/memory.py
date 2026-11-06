"""Memory diagnostic rules.

Currently implements:

- **MEM-LEAK-01**: Detect monotonically growing RSS in a process over the
  observation window using linear regression (via ``trend.fit_linear``).
  Fires WARNING when slope > 1 MB/h and R² > 0.9; CRITICAL when the slope
  exceeds the critical threshold.  Confidence is derived from R² and
  observation time.

- **MEM-PRESSURE-01**: Detect chronic memory pressure by checking whether
  ``psi_mem_max`` in ``sys_rollup_1m`` exceeds a threshold on multiple days
  (using ``trend.detect_recurrence``) and swap-out activity is present.
  Fires WARNING.

- **SWAP-THRASH-01**: Detect simultaneous swap-in and swap-out in the same
  time window combined with ``psi_mem_full10 > 0``, indicating that the kernel
  is thrashing pages.  Fires CRITICAL.

Design doc reference: §6.2 MEM-LEAK-01 / MEM-PRESSURE-01 / SWAP-THRASH-01.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional

from ..core import (
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
from .trend import (
    baseline_deviation,
    confidence_from_fit,
    days_until,
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

        # Total memory for the (very rough) days-to-OOM extrapolation, from the
        # last sys_sample of the window.
        total_mem = None
        sys_rows = store.rows("sys_samples", since=window.start_ts, until=window.end_ts)
        if sys_rows and sys_rows[-1]["mem_available"] is not None:
            total_mem = sys_rows[-1]["mem_used"] + sys_rows[-1]["mem_available"]
        days_to_oom = (
            days_until(obs.evidence["last_value"], total_mem, MiB * slope_mb_h)
            if total_mem
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
        pss_fit = fit_linear(pss_series, min_points=_PSS_MIN_POINTS, min_span_hours=0.0)
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

_MEM_PRESSURE_PSI_THRESHOLD: float = 5.0  # psi_mem_max > this value triggers
_MEM_PRESSURE_MIN_RECURRENCE: float = 0.5  # fraction of days with pressure


@register
class MemPressureRule(Rule):
    """Detect chronically recurring memory pressure with swap activity.

    Queries ``sys_rollup_1m`` for rows where ``psi_mem_max`` exceeds the
    threshold, groups them by calendar day to compute a recurrence fraction
    via ``trend.detect_recurrence``, and fires WARNING when pressure recurs on
    at least half the observed days *and* total swap-out is positive.

    PSI columns absent from the DB (all None) → silent (no finding).
    """

    rule_id = "MEM-PRESSURE-01"
    category = "memory"
    title = "慢性メモリ圧迫"
    default_severity = Severity.WARNING
    min_observation_hours = 24.0
    requires_rows = ("sys_rollup_1m",)
    what = (
        "sys_rollup_1m の psi_mem_max が閾値超の時間帯が日次で反復するかを"
        "detect_recurrence で確認し、swap_out_sum > 0 も合わせて判定する"
    )
    why = (
        "断続的なメモリ圧力はスパイクとして見えず、PSI の反復パターンで初めて"
        "顕在化する。swap_out の存在がページアウト圧力の物的証拠となる"
    )
    manual_commands = [
        "free -h",
        "vmstat 1 10",
        "sar -B 1 10",
        "cat /proc/pressure/memory",
    ]

    psi_threshold: float = _MEM_PRESSURE_PSI_THRESHOLD
    min_recurrence_fraction: float = _MEM_PRESSURE_MIN_RECURRENCE

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

        # Build per-day boolean map: did this day have any psi_mem_max > threshold?
        events_by_day: dict[str, bool] = {}
        pressure_timestamps: list[float] = []
        total_swap_out: int = 0

        for row in rollup_rows:
            ts = row["ts_min"]
            psi = row["psi_mem_max"]
            swap_out = row["swap_out_sum"] or 0
            total_swap_out += swap_out

            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            day_key = dt.strftime("%Y-%m-%d")
            # Mark day as having had pressure if not already True.
            if psi is not None and psi > self.psi_threshold:
                events_by_day[day_key] = True
                pressure_timestamps.append(ts)
            else:
                events_by_day.setdefault(day_key, False)

        # Need swap activity alongside pressure.
        if total_swap_out <= 0:
            return []

        recurrence = detect_recurrence(events_by_day)
        n_days = len(events_by_day)
        n_pressure_days = sum(1 for v in events_by_day.values() if v)

        if recurrence < self.min_recurrence_fraction:
            return []

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
            "swap_out_total_bytes": total_swap_out,
            "pressure_event_count": len(pressure_timestamps),
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
            f"{n_pressure_days}/{n_days} 日で反復"
            f" (swap_out={total_swap_out // MiB} MB)"
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
    requires_rows = ("sys_samples",)
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

        # Aggregate evidence.
        total_swap_in = sum(r["swap_in"] or 0 for r in thrash_rows)
        total_swap_out = sum(r["swap_out"] or 0 for r in thrash_rows)
        peak_psi_full = max(r["psi_mem_full10"] or 0.0 for r in thrash_rows)
        thrash_start = min(r["ts"] for r in thrash_rows)
        thrash_end = max(r["ts"] for r in thrash_rows)
        duration_minutes = max(1, round((thrash_end - thrash_start) / 60.0) + 1)

        evidence = {
            "thrash_event_count": len(thrash_rows),
            "swap_in_total_pages": total_swap_in,
            "swap_out_total_pages": total_swap_out,
            "peak_psi_mem_full10": round(peak_psi_full, 2),
            "period_start": thrash_start,
            "period_end": thrash_end,
            "duration_minutes": duration_minutes,
        }

        summary = (
            f"スワップスラッシング: {len(thrash_rows)} サンプルで swap_in/out 同時発生"
            f" (psi_mem_full10 最大 {peak_psi_full:.1f}%、{duration_minutes}分間)"
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
