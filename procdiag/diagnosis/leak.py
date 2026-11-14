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
  ``proc_samples`` rows with ``cpu_pct=0``, ``rss=0`` and at most one thread
  (what a zombie reads as; kernel threads read the same), reported at
  ``confidence=LOW``.

Design doc reference: §6.2 FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..core import (
    Confidence,
    Finding,
    Headline,
    MeasuredRule,
    Observation,
    Severity,
    Window,
    register,
)
from .trend import (
    confidence_from_fit,
    days_until,
    fit_linear,
    observed_fit,
    proc_trend_observations,
)

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

# System-wide thread limit.  It is not recorded, so it is only read when the
# check runs on the host that made the recording.
_THREADS_MAX_PATH = Path("/proc/sys/kernel/threads-max")

# ZOMBIE-01 fires only on a real accumulation: a clear upward line, not a blip.
_ZOMBIE_SLOPE_WARNING_H: float = 0.5
_ZOMBIE_MIN_R2: float = 0.7


# ---------------------------------------------------------------------------
# FD-LEAK-01
# ---------------------------------------------------------------------------


@register
class FdLeakRule(MeasuredRule):
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
    requires_rows = ("proc_rollup_1m",)
    headline = Headline("FD 増加率", "/h", threshold="slope_warning_fd_h")
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

    def observe(self, store: "Storage", window: Window) -> list[Observation]:
        return proc_trend_observations(
            store.rows("proc_rollup_1m", since=window.start_ts, until=window.end_ts),
            store.proc_identities(),
            "fds_max",
            min_span_hours=self.min_observation_hours,
            holes=window.holes,
        )

    def judge(
        self, store: "Storage", window: Window, obs: Observation
    ) -> Optional[Finding]:
        fit = observed_fit(obs)
        slope_fd_h = obs.value
        if fit.r2 < self.min_r2 or slope_fd_h < self.slope_warning_fd_h:
            return None

        proc_id = obs.evidence["proc_id"]
        process_name = obs.subject
        confidence = confidence_from_fit(
            fit, min_observation_hours=self.min_observation_hours
        )

        current_fds = obs.evidence["last_value"]
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

        return Finding(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            subject=process_name,
            summary=summary,
            evidence=evidence,
            detected_at=(window.start_ts, window.end_ts),
            confidence=confidence,
        )


# ---------------------------------------------------------------------------
# THREAD-LEAK-01
# ---------------------------------------------------------------------------


def _host_threads_max(store: "Storage") -> Optional[int]:
    """This host's ``threads-max``, or None when it is not known.

    The limit is not part of the recording, so it is read at check time and
    only when the recording was made on this host (``meta.hostname``).
    """
    if store.meta().get("hostname") != socket.gethostname():
        return None
    try:
        return int(_THREADS_MAX_PATH.read_text().strip()) or None
    except (OSError, ValueError):
        return None


@register
class ThreadLeakRule(MeasuredRule):
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
    requires_rows = ("proc_rollup_1m",)
    headline = Headline("スレッド増加率", "/h", threshold="slope_warning_h")
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

    def observe(self, store: "Storage", window: Window) -> list[Observation]:
        return proc_trend_observations(
            store.rows("proc_rollup_1m", since=window.start_ts, until=window.end_ts),
            store.proc_identities(),
            "threads_max",
            min_span_hours=self.min_observation_hours,
            holes=window.holes,
        )

    def judge(
        self, store: "Storage", window: Window, obs: Observation
    ) -> Optional[Finding]:
        fit = observed_fit(obs)
        slope_h = obs.value
        if fit.r2 < self.min_r2 or slope_h < self.slope_warning_h:
            return None

        process_name = obs.subject
        confidence = confidence_from_fit(
            fit, min_observation_hours=self.min_observation_hours
        )

        current_threads = obs.evidence["last_value"]
        thread_max = _host_threads_max(store)
        days_to_thread_max = (
            days_until(current_threads, thread_max, slope_h) if thread_max else None
        )

        evidence = {
            "proc_id": obs.evidence["proc_id"],
            "process_name": process_name,
            "slope_threads_per_h": slope_h,
            "r2": fit.r2,
            "n": fit.n,
            "span_hours": fit.span_hours,
            "current_threads": current_threads,
            "days_to_thread_max": days_to_thread_max,
            "thread_max_estimate": thread_max,
            "thread_max_source": "this_host" if thread_max else "unknown",
        }

        summary = (
            f"{process_name}: スレッド数 +{slope_h:.1f}/h で単調増加"
            f" (R²={fit.r2:.2f}, {fit.span_hours:.1f}h観測)"
        )

        return Finding(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            subject=process_name,
            summary=summary,
            evidence=evidence,
            detected_at=(window.start_ts, window.end_ts),
            confidence=confidence,
        )


# ---------------------------------------------------------------------------
# ZOMBIE-01
# ---------------------------------------------------------------------------


@register
class ZombieRule(MeasuredRule):
    """Detect accumulating zombie processes from the per-scan zombie count.

    The process sampler records the number of processes in the zombie state on
    every scan (``proc_counts``); a linear trend is fitted to that series and
    INFO fires when the line rises by ``slope_warning_h`` per hour with
    ``R² >= min_r2`` and zombies are still present at the end of the window
    (confidence MEDIUM — the counts are exact, the trend is a regression).  A
    blip of one or two zombies in one scan is not a trend.  For DBs recorded
    before schema v4 the count is approximated as rows with ``cpu_pct=0``,
    ``rss=0`` and at most one thread, and the finding is marked
    ``confidence=LOW`` / ``method="approx"``.
    """

    rule_id = "ZOMBIE-01"
    category = "leak"
    title = "ゾンビプロセス増加疑い"
    default_severity = Severity.INFO
    min_observation_hours = 6.0
    requires_rows = ("proc_samples",)
    headline = Headline("ゾンビ増加率", "/h", threshold="slope_warning_h", digits=2)
    what = (
        "プロセス走査ごとのゾンビ数 (proc_counts) を時系列化し、"
        "増加傾向を線形回帰で検出する。傾きと R² が足りない一瞬のゾンビや、"
        "すでに回収されたゾンビは増加とみなさない"
    )
    why = (
        "ゾンビプロセスは親が wait() を呼ばないことで蓄積し、PID の枠を占有する。"
        "旧形式の DB では cpu_pct=0・rss=0・スレッド数 1 以下の組み合わせで近似する"
    )
    manual_commands = [
        "ps aux | grep ' Z '",
        "ps -o ppid= -p <ZOMBIE_PID>",
    ]

    # Minimum slope (zombies/h) and R² to emit INFO.
    slope_warning_h: float = _ZOMBIE_SLOPE_WARNING_H
    min_r2: float = _ZOMBIE_MIN_R2

    def observe(self, store: "Storage", window: Window) -> list[Observation]:
        series = self._counted_series(store, window)
        exact = bool(series)
        if not exact:
            series = self._approx_series(store, window)
        fit = fit_linear(
            series,
            min_points=12,
            min_span_hours=self.min_observation_hours,
            holes=window.holes,
        )
        if fit is None:
            return []
        return [
            Observation(
                subject="zombie_processes",
                identity=None,
                value=fit.slope_per_hour,
                evidence={
                    "method": "status" if exact else "approx",
                    "r2": fit.r2,
                    "n": fit.n,
                    "span_hours": fit.span_hours,
                    "last_value": series[-1][1],
                },
            )
        ]

    def judge(
        self, store: "Storage", window: Window, obs: Observation
    ) -> Optional[Finding]:
        fit = observed_fit(obs)
        slope_h = obs.value
        if fit.r2 < self.min_r2 or slope_h < self.slope_warning_h:
            return None

        exact = obs.evidence["method"] == "status"
        current = obs.evidence["last_value"]
        if exact and current < 1:
            return None
        if exact:
            summary = (
                f"ゾンビプロセス数が +{slope_h:.2f}/h の傾向で増加中"
                f" (現在 {current:.0f} 個, R²={fit.r2:.2f}, {fit.span_hours:.1f}h観測)"
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
            "method": obs.evidence["method"],
            "current_zombies": current if exact else None,
            "confidence": confidence.value,
        }

        return Finding(
            rule_id=self.rule_id,
            severity=Severity.INFO,
            subject="zombie_processes",
            summary=summary,
            evidence=evidence,
            detected_at=(window.start_ts, window.end_ts),
            confidence=confidence,
        )

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
        # Query proc_samples for zombie approximation: psutil reads a zombie as
        # cpu_pct=0, rss=0 and one thread, so rows with cpu_pct=0 (or NULL),
        # rss=0 (or NULL) and num_threads<=1 (or NULL).  Kernel threads read the
        # same, which only shifts the level, not the trend.  Aggregated per
        # distinct timestamp.
        try:
            rows = store.conn.execute(
                """
                SELECT ts,
                       COUNT(*) AS zombie_approx
                FROM proc_samples
                WHERE ts >= ? AND ts <= ?
                  AND (cpu_pct = 0 OR cpu_pct IS NULL)
                  AND (rss = 0 OR rss IS NULL)
                  AND (num_threads <= 1 OR num_threads IS NULL)
                GROUP BY ts
                ORDER BY ts
                """,
                (window.start_ts, window.end_ts),
            ).fetchall()
        except Exception:
            # Defensive: if query fails, skip gracefully.
            return []
        return [(float(r["ts"]), float(r["zombie_approx"])) for r in rows]
