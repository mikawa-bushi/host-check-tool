"""Self-measurement and automatic throttle for procdiag's own overhead.

Maps to design doc §4.8. The SelfMeter watches procdiag's own CPU usage over a
60-second sliding window.  When the moving average exceeds
``BudgetConfig.overhead_budget_pct``, it signals a throttle-down so the
scheduler can widen the Tier-2b interval.  After 10 minutes of staying under
budget, it signals a throttle-restore so the interval gradually returns to the
configured baseline.

The class is deliberately free of I/O: it only reads ``psutil.Process`` (self)
and accepts a ``mono_now`` argument so tests can drive a fake clock without any
monkeypatching of ``time.monotonic``.
"""

from __future__ import annotations

import os
import time
from collections import deque
from typing import TYPE_CHECKING, Deque, Optional, Tuple

import psutil

if TYPE_CHECKING:
    from ..config import BudgetConfig

# Width of the CPU moving-average window in seconds.
_WINDOW_SEC = 60.0
# How many seconds of continuous within-budget time triggers a restore.
_RESTORE_PATIENCE_SEC = 600.0  # 10 minutes


class SelfMeter:
    """Monitor procdiag's own CPU overhead and emit throttle signals.

    Usage::

        meter = SelfMeter(config.budget)
        # inside the Tier-1 collect loop:
        cpu_pct, rss = meter.sample()
        event = meter.update(time.monotonic())
        if event == "throttle_down":
            scheduler.set_process_factor(meter.factor)

    Thread-safety: not thread-safe; intended for the single-threaded recorder
    loop.
    """

    def __init__(self, cfg: "BudgetConfig") -> None:
        self._cfg = cfg
        self._proc = psutil.Process(os.getpid())
        # Initialise cpu_percent once so the first call to sample() is valid.
        self._proc.cpu_percent(interval=None)

        # Sliding window: deque of (mono_ts, cpu_pct) tuples within _WINDOW_SEC.
        self._window: Deque[Tuple[float, float]] = deque()

        # Current throttle multiplier applied to the Tier-2b interval.
        self._factor: float = 1.0

        # Monotonic timestamp when the CPU last went *over* budget.
        # None means we are currently within budget (or never measured yet).
        self._over_budget_since: Optional[float] = None

        # Monotonic timestamp when the CPU returned under budget after a
        # throttle-down event.  Used to count the 10-minute patience period.
        self._under_budget_since: Optional[float] = None

        # Prevent re-emitting the same event on consecutive ticks.
        self._throttled: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sample(self) -> Tuple[float, int]:
        """Return ``(cpu_pct_since_last_sample, rss_bytes)`` for this process.

        ``cpu_pct`` is the CPU usage since the previous call (psutil interval=None
        differential mode).  The first call after ``__init__`` always returns
        0.0 because ``__init__`` already primed the counter.
        """
        cpu = self._proc.cpu_percent(interval=None)
        rss = self._proc.memory_info().rss
        return cpu, rss

    def update(self, mono_now: float) -> Optional[str]:
        """Update the moving average and return a throttle signal if state changed.

        Parameters
        ----------
        mono_now:
            Current ``time.monotonic()`` value (or a fake value in tests).

        Returns
        -------
        ``"throttle_down"``
            First tick where the 60-second moving average exceeds the overhead
            budget.  The caller should widen the Tier-2b interval by
            ``self.factor`` immediately after receiving this.
        ``"throttle_restore"``
            First tick after 10 consecutive minutes of staying within budget
            following a throttle-down.  The caller should restore the Tier-2b
            interval to the baseline.
        ``None``
            No state change; the caller need not act.
        """
        # Obtain the latest CPU reading.
        cpu, _rss = self.sample()
        self._window.append((mono_now, cpu))

        # Evict samples older than the window.
        cutoff = mono_now - _WINDOW_SEC
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

        if not self._window:
            return None

        avg = sum(v for _, v in self._window) / len(self._window)
        over = avg > self._cfg.overhead_budget_pct

        if over:
            self._under_budget_since = None
            if not self._throttled:
                # Ratchet up the throttle factor.
                new_factor = self._factor * self._cfg.throttle_factor
                self._factor = min(new_factor, self._cfg.throttle_max_interval)
                self._throttled = True
                self._over_budget_since = mono_now
                return "throttle_down"
        else:
            # Within budget.
            if self._throttled:
                # Start (or continue) counting patience time.
                if self._under_budget_since is None:
                    self._under_budget_since = mono_now
                elapsed = mono_now - self._under_budget_since
                if elapsed >= _RESTORE_PATIENCE_SEC:
                    # Gradually restore: divide factor by throttle_factor (step
                    # back towards 1.0 rather than snapping immediately).
                    new_factor = self._factor / self._cfg.throttle_factor
                    self._factor = max(1.0, new_factor)
                    if self._factor <= 1.0:
                        self._factor = 1.0
                        self._throttled = False
                        self._under_budget_since = None
                    return "throttle_restore"

        return None

    @property
    def factor(self) -> float:
        """Current interval multiplier for Tier-2b (always >= 1.0)."""
        return self._factor
