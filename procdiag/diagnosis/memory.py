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

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..core import (
    Confidence,
    Finding,
    Rule,
    Severity,
    Window,
    register,
)
from .trend import TrendFit, fit_linear, detect_recurrence

if TYPE_CHECKING:
    from ..storage import Storage

# ---------------------------------------------------------------------------
# Thresholds — exposed as class attributes so tests can inject alternatives
# ---------------------------------------------------------------------------

_BYTES_PER_MB = 1024 * 1024

# Minimum slope (MB/h) to emit a WARNING.
_MEM_LEAK_SLOPE_WARNING_MB_H: float = 1.0
# Minimum slope (MB/h) to escalate to CRITICAL.
_MEM_LEAK_SLOPE_CRITICAL_MB_H: float = 50.0
# Minimum R² for the regression to be considered reliable.
_MEM_LEAK_MIN_R2: float = 0.9


@register
class MemLeakRule(Rule):
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

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run MEM-LEAK-01 against the observation window."""
        findings: list[Finding] = []

        # Fetch all distinct proc_ids from the rollup table within the window.
        proc_rows = store.rows(
            "proc_rollup_1m",
            since=window.start_ts,
            until=window.end_ts,
        )

        # Group rss_avg by proc_id.
        rss_by_proc: dict[int, list[tuple[float, float]]] = {}
        for row in proc_rows:
            pid = row["proc_id"]
            rss = row["rss_avg"]
            ts = row["ts_min"]
            if rss is not None:
                rss_by_proc.setdefault(pid, []).append((ts, float(rss)))

        for proc_id, series in rss_by_proc.items():
            fit = fit_linear(
                series,
                min_points=12,
                min_span_hours=self.min_observation_hours,
            )
            if fit is None:
                continue
            if fit.r2 < self.min_r2:
                continue

            slope_mb_h = fit.slope_per_hour / _BYTES_PER_MB
            if slope_mb_h < self.slope_warning_mb_h:
                continue

            # Look up process name from proc_registry (no time column; direct query).
            reg = store.conn.execute(
                "SELECT name FROM proc_registry WHERE proc_id = ?", (proc_id,)
            ).fetchone()
            process_name = reg["name"] if reg else f"proc_{proc_id}"

            # Severity escalation.
            severity = (
                Severity.CRITICAL
                if slope_mb_h >= self.slope_critical_mb_h
                else Severity.WARNING
            )

            # Confidence from R² × observation time.
            if fit.r2 >= 0.95 and fit.span_hours >= 12.0:
                confidence = Confidence.HIGH
            elif fit.r2 >= 0.90 and fit.span_hours >= self.min_observation_hours:
                confidence = Confidence.MEDIUM
            else:
                confidence = Confidence.LOW

            # Estimate days until OOM (very rough: extrapolate to total mem).
            days_to_oom: float | None = None
            mem_row = store.rows(
                "sys_rollup_1m", since=window.start_ts, until=window.end_ts
            )
            if mem_row:
                # Use last known total memory from sys_samples.
                sys_last = store.rows(
                    "sys_samples",
                    since=window.start_ts,
                    until=window.end_ts,
                )
                if sys_last:
                    last_sys = sys_last[-1]
                    total_mem = (
                        last_sys["mem_used"] + last_sys["mem_available"]
                        if last_sys["mem_available"] is not None
                        else None
                    )
                    if total_mem and slope_mb_h > 0:
                        current_rss = series[-1][1]
                        remaining_bytes = total_mem - current_rss
                        if remaining_bytes > 0:
                            days_to_oom = remaining_bytes / (
                                _BYTES_PER_MB * slope_mb_h * 24
                            )

            # Detect if the process is Python-based.
            is_python = "python" in process_name.lower() or "py" in process_name.lower()

            evidence = {
                "proc_id": proc_id,
                "process_name": process_name,
                "slope_mb_per_h": slope_mb_h,
                "r2": fit.r2,
                "n": fit.n,
                "span_hours": fit.span_hours,
                "is_python": is_python,
                "days_to_oom": days_to_oom,
            }

            summary = (
                f"{process_name}: RSS +{slope_mb_h:.1f} MB/h で単調増加"
                f" (R²={fit.r2:.2f}, {fit.span_hours:.1f}h観測)"
            )

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=severity,
                    subject=process_name,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(window.start_ts, window.end_ts),
                    confidence=confidence,
                )
            )

        return findings


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

        summary = (
            f"慢性メモリ圧迫: psi_mem_max > {self.psi_threshold} が"
            f"{n_pressure_days}/{n_days} 日で反復"
            f" (swap_out={total_swap_out // (1024 * 1024)} MB)"
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
