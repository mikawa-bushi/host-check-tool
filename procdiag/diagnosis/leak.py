"""Leak diagnostic rules: FD-LEAK-01, THREAD-LEAK-01, ZOMBIE-01.

This module implements three rules that detect resource leaks and zombie
process accumulation.

- **FD-LEAK-01**: Detect file-descriptor count growing monotonically per
  process, using linear regression on ``proc_rollup_1m.fds_max``.  Mirrors
  the MEM-LEAK-01 pattern from ``memory.py`` exactly.

- **THREAD-LEAK-01**: Detect thread count growing monotonically per process,
  using linear regression on ``proc_rollup_1m.threads_max``.

- **ZOMBIE-01**: Detect zombie process accumulation by fitting a trend to the
  per-scan zombie count in ``proc_counts`` (schema v4, from the real process
  state).  DBs recorded before v4 fall back to approximating zombies as
  ``proc_samples`` rows with ``cpu_pct=0``, ``rss=0`` and ``num_threads=0``,
  reported at ``confidence=LOW``.

Design doc reference: §6.2 FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import (
    Confidence,
    Finding,
    Rule,
    Severity,
    Window,
    register,
)
from .trend import confidence_from_fit, days_until, fit_each, fit_linear, group_series

if TYPE_CHECKING:
    from ..storage import Storage

# ---------------------------------------------------------------------------
# Thresholds — exposed as class attributes so tests can inject alternatives
# ---------------------------------------------------------------------------

# Minimum slope (FDs/h) to emit an FD-LEAK-01 WARNING.
_FD_LEAK_SLOPE_WARNING_FD_H: float = 0.5
# Minimum R² for the regression to be considered reliable.
_FD_LEAK_MIN_R2: float = 0.9

# Minimum slope (threads/h) to emit a THREAD-LEAK-01 WARNING.
_THREAD_LEAK_SLOPE_WARNING_H: float = 0.5
# Minimum R² for the regression to be considered reliable.
_THREAD_LEAK_MIN_R2: float = 0.9

# Common FD soft/hard limits used for "days to limit" estimates when the
# process's real RLIMIT_NOFILE was not recorded (other users' processes when
# recording non-root, or a DB from before schema v4).
_FD_SOFT_LIMIT_DEFAULT: int = 1024
_FD_HARD_LIMIT_DEFAULT: int = 4096

# Approximate system threads-max used when /proc/sys/kernel/threads-max
# is unavailable.
_THREADS_MAX_DEFAULT: int = 32768


# ---------------------------------------------------------------------------
# FD-LEAK-01
# ---------------------------------------------------------------------------


@register
class FdLeakRule(Rule):
    """Detect per-process FD leaks via linear regression on fds_max rollups.

    Queries ``proc_rollup_1m`` for each registered process, fits a line to
    the ``fds_max`` time series, and fires WARNING when the slope and R² both
    exceed their thresholds.  Evidence includes estimated days until the
    process hits the default soft (1024) and hard (4096) FD limits as a
    rough guide; users are encouraged to verify the actual limit with
    ``cat /proc/<pid>/limits``.
    """

    rule_id = "FD-LEAK-01"
    category = "leak"
    title = "ファイルディスクリプタリーク疑い"
    default_severity = Severity.WARNING
    min_observation_hours = 6.0
    what = "proc_rollup_1m の fds_max に線形回帰をかけ、増加傾向と R² で判定する"
    why = (
        "FD リークは open() のクローズ忘れによる単調増加として現れる。"
        "R² > 0.9 は偶発的なスパイクでなく持続的増加であることを担保する"
    )
    manual_commands = [
        "ls /proc/<PID>/fd | wc -l",
        "cat /proc/<PID>/limits",
        "lsof -p <PID>",
    ]

    # Allow tests to override thresholds on the class.
    slope_warning_fd_h: float = _FD_LEAK_SLOPE_WARNING_FD_H
    min_r2: float = _FD_LEAK_MIN_R2

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run FD-LEAK-01 against the observation window."""
        findings: list[Finding] = []

        fds_by_proc = group_series(
            store.rows("proc_rollup_1m", since=window.start_ts, until=window.end_ts),
            key="proc_id",
            value="fds_max",
        )

        for proc_id, series, fit in fit_each(
            fds_by_proc,
            min_points=12,
            min_span_hours=self.min_observation_hours,
            min_r2=self.min_r2,
        ):
            slope_fd_h = fit.slope_per_hour
            if slope_fd_h < self.slope_warning_fd_h:
                continue

            process_name = store.proc_name(proc_id)
            confidence = confidence_from_fit(
                fit, min_observation_hours=self.min_observation_hours
            )

            # Estimate days until the soft/hard FD limit is reached — the
            # process's recorded RLIMIT_NOFILE when known, else common defaults.
            current_fds = series[-1][1]
            limits = store.proc_fd_limits(proc_id)
            soft, hard = limits or (_FD_SOFT_LIMIT_DEFAULT, _FD_HARD_LIMIT_DEFAULT)
            days_to_soft_limit = days_until(current_fds, soft, slope_fd_h)
            days_to_hard_limit = days_until(current_fds, hard, slope_fd_h)

            evidence = {
                "proc_id": proc_id,
                "process_name": process_name,
                "slope_fd_per_h": slope_fd_h,
                "r2": fit.r2,
                "n": fit.n,
                "span_hours": fit.span_hours,
                "current_fds": current_fds,
                "days_to_soft_limit": days_to_soft_limit,
                "days_to_hard_limit": days_to_hard_limit,
                "fd_soft_limit": soft,
                "fd_hard_limit": hard,
                "fd_limit_source": "measured" if limits else "default",
            }

            summary = (
                f"{process_name}: FD数 +{slope_fd_h:.1f}/h で単調増加"
                f" (R²={fit.r2:.2f}, {fit.span_hours:.1f}h観測)"
            )

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.WARNING,
                    subject=process_name,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(window.start_ts, window.end_ts),
                    confidence=confidence,
                )
            )

        return findings


# ---------------------------------------------------------------------------
# THREAD-LEAK-01
# ---------------------------------------------------------------------------


@register
class ThreadLeakRule(Rule):
    """Detect per-process thread leaks via linear regression on threads_max rollups.

    Queries ``proc_rollup_1m`` for each registered process, fits a line to
    the ``threads_max`` time series, and fires WARNING when the slope and R²
    both exceed their thresholds.
    """

    rule_id = "THREAD-LEAK-01"
    category = "leak"
    title = "スレッドリーク疑い"
    default_severity = Severity.WARNING
    min_observation_hours = 6.0
    what = "proc_rollup_1m の threads_max に線形回帰をかけ、増加傾向と R² で判定する"
    why = (
        "スレッドリークはスレッドの生成後に join/detach が漏れる典型的バグ。"
        "R² > 0.9 は偶発的なスパイクでなく持続的増加であることを担保する"
    )
    manual_commands = [
        "cat /proc/<PID>/status | grep Threads",
        "pstack <PID>",
    ]

    # Allow tests to override thresholds on the class.
    slope_warning_h: float = _THREAD_LEAK_SLOPE_WARNING_H
    min_r2: float = _THREAD_LEAK_MIN_R2

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run THREAD-LEAK-01 against the observation window."""
        findings: list[Finding] = []

        threads_by_proc = group_series(
            store.rows("proc_rollup_1m", since=window.start_ts, until=window.end_ts),
            key="proc_id",
            value="threads_max",
        )

        for proc_id, series, fit in fit_each(
            threads_by_proc,
            min_points=12,
            min_span_hours=self.min_observation_hours,
            min_r2=self.min_r2,
        ):
            slope_h = fit.slope_per_hour
            if slope_h < self.slope_warning_h:
                continue

            process_name = store.proc_name(proc_id)
            confidence = confidence_from_fit(
                fit, min_observation_hours=self.min_observation_hours
            )

            # Estimate days until a nominal thread-max limit is reached.
            current_threads = series[-1][1]
            days_to_thread_max = days_until(
                current_threads, _THREADS_MAX_DEFAULT, slope_h
            )

            evidence = {
                "proc_id": proc_id,
                "process_name": process_name,
                "slope_threads_per_h": slope_h,
                "r2": fit.r2,
                "n": fit.n,
                "span_hours": fit.span_hours,
                "current_threads": current_threads,
                "days_to_thread_max": days_to_thread_max,
                "thread_max_estimate": _THREADS_MAX_DEFAULT,
            }

            summary = (
                f"{process_name}: スレッド数 +{slope_h:.1f}/h で単調増加"
                f" (R²={fit.r2:.2f}, {fit.span_hours:.1f}h観測)"
            )

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.WARNING,
                    subject=process_name,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(window.start_ts, window.end_ts),
                    confidence=confidence,
                )
            )

        return findings


# ---------------------------------------------------------------------------
# ZOMBIE-01
# ---------------------------------------------------------------------------


@register
class ZombieRule(Rule):
    """Detect accumulating zombie processes from the per-scan zombie count.

    The process sampler records the number of processes in the zombie state on
    every scan (``proc_counts``); a linear trend is fitted to that series and a
    positive slope fires INFO (confidence MEDIUM — the counts are exact, the
    trend is a regression).  For DBs recorded before schema v4 the count is
    approximated as rows with ``cpu_pct=0``, ``rss=0`` and ``num_threads=0``,
    and the finding is marked ``confidence=LOW`` / ``method="approx"``.
    """

    rule_id = "ZOMBIE-01"
    category = "leak"
    title = "ゾンビプロセス増加疑い"
    default_severity = Severity.INFO
    min_observation_hours = 6.0
    what = (
        "プロセス走査ごとのゾンビ数 (proc_counts) を時系列化し、"
        "増加傾向を線形回帰で検出する"
    )
    why = (
        "ゾンビプロセスは親が wait() を呼ばないことで蓄積し、PID の枠を占有する。"
        "旧形式の DB では cpu_pct/rss/num_threads=0 の組み合わせで近似する"
    )
    manual_commands = [
        "ps aux | grep ' Z '",
        "ps -o ppid= -p <ZOMBIE_PID>",
    ]

    # Minimum slope (zombies/h) to emit INFO.
    slope_warning_h: float = 0.0

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run ZOMBIE-01 against the observation window."""
        series = self._counted_series(store, window)
        exact = bool(series)
        if not exact:
            series = self._approx_series(store, window)
        if not series:
            return []

        fit = fit_linear(
            series,
            min_points=12,
            min_span_hours=self.min_observation_hours,
        )
        if fit is None:
            return []

        slope_h = fit.slope_per_hour
        if slope_h <= self.slope_warning_h:
            return []

        if exact:
            summary = (
                f"ゾンビプロセス数が +{slope_h:.2f}/h の傾向で増加中"
                f" (現在 {series[-1][1]:.0f} 個, R²={fit.r2:.2f}, {fit.span_hours:.1f}h観測)"
            )
        else:
            summary = (
                f"ゾンビ近似プロセス数が +{slope_h:.2f}/h の傾向で増加中"
                f" (R²={fit.r2:.2f}, {fit.span_hours:.1f}h観測、近似値)"
            )
        confidence = Confidence.MEDIUM if exact else Confidence.LOW

        evidence = {
            "slope_per_h": slope_h,
            "r2": fit.r2,
            "n": fit.n,
            "span_hours": fit.span_hours,
            "method": "status" if exact else "approx",
            "current_zombies": series[-1][1] if exact else None,
            "confidence": confidence.value,
        }

        return [
            Finding(
                rule_id=self.rule_id,
                severity=Severity.INFO,
                subject="zombie_processes",
                summary=summary,
                evidence=evidence,
                detected_at=(window.start_ts, window.end_ts),
                confidence=confidence,
            )
        ]

    @staticmethod
    def _counted_series(store: "Storage", window: Window) -> list[tuple[float, float]]:
        """Exact per-scan zombie counts (schema v4); [] when not recorded."""
        if not store.has_table("proc_counts"):
            return []
        rows = store.rows("proc_counts", since=window.start_ts, until=window.end_ts)
        return [(float(r["ts"]), float(r["zombies"])) for r in rows]

    @staticmethod
    def _approx_series(store: "Storage", window: Window) -> list[tuple[float, float]]:
        """Pre-v4 fallback: count rows that look like zombies per scan."""
        # Query proc_samples for zombie approximation:
        # rows with cpu_pct=0 (or NULL), rss=0 (or NULL), num_threads=0 (or NULL).
        # We aggregate per distinct timestamp.
        try:
            rows = store.conn.execute(
                """
                SELECT ts,
                       COUNT(*) AS zombie_approx
                FROM proc_samples
                WHERE ts >= ? AND ts <= ?
                  AND (cpu_pct = 0 OR cpu_pct IS NULL)
                  AND (rss = 0 OR rss IS NULL)
                  AND (num_threads = 0 OR num_threads IS NULL)
                GROUP BY ts
                ORDER BY ts
                """,
                (window.start_ts, window.end_ts),
            ).fetchall()
        except Exception:
            # Defensive: if query fails, skip gracefully.
            return []
        return [(float(r["ts"]), float(r["zombie_approx"])) for r in rows]
