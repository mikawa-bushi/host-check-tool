"""Self-measurement and automatic throttle for procdiag's own overhead.

Maps to design doc §4.8. The SelfMeter watches procdiag's own CPU usage over a
60-second sliding window.  When the moving average exceeds
``BudgetConfig.overhead_budget_pct``, it widens the Tier-2b interval by
``throttle_factor`` and signals a throttle-down; while the average stays over
budget it widens again once per window (the average then covers the slower
scans), until the interval reaches ``throttle_max_interval``.  Each 10 minutes
under budget takes one step back towards the configured interval.

The first ``BudgetConfig.warmup_sec`` seconds are excluded from the average:
start-up pays one-off costs (sampler setup, a ``psutil.Process`` object for
every PID on the first full scan) that say nothing about the steady-state
overhead.  Measured at ~9% CPU in the first second on an idle host, that alone
used to trip the 1% budget on every start, so every report showed a spurious
throttle event and Tier-2b ran slower for the first 10+ minutes.

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
# Floor below which sample() returns the cached reading instead of calling
# cpu_percent again.  interval=None mode divides CPU time by the wall time
# since the *previous* call, so two calls a few ms apart (collect → update,
# or a scheduler catch-up burst) would report absurd values like 600%.
_MIN_SAMPLE_INTERVAL_SEC = 0.2


class SelfMeter:
    """Monitor procdiag's own CPU overhead and emit throttle signals.

    Usage::

        meter = SelfMeter(config.budget, process_interval=10.0)
        # on every Tier-1 tick:
        event = meter.update(time.monotonic())
        # the Tier-2b interval is process_interval * meter.factor

    Thread-safety: not thread-safe; intended for the single-threaded recorder
    loop.
    """

    def __init__(self, cfg: "BudgetConfig", *, process_interval: float) -> None:
        self._cfg = cfg
        # The factor that widens process_interval to throttle_max_interval; a
        # configured interval already that long is never widened.
        self._max_factor = max(1.0, cfg.throttle_max_interval / process_interval)
        self._proc = psutil.Process(os.getpid())
        # Initialise cpu_percent once so the first call to sample() is valid.
        self._proc.cpu_percent(interval=None)

        # Sliding window: deque of (mono_ts, cpu_pct) tuples within _WINDOW_SEC.
        self._window: Deque[Tuple[float, float]] = deque()

        # Current throttle multiplier applied to the Tier-2b interval.
        self._factor: float = 1.0

        # Monotonic time of the last widening step.
        self._widened_at: Optional[float] = None

        # Monotonic time since when the CPU has stayed under budget while
        # throttled (restarted by each restore step).
        self._under_budget_since: Optional[float] = None

        # Cache for the tiny-interval floor (see _MIN_SAMPLE_INTERVAL_SEC).
        self._last_sample: Optional[Tuple[float, int]] = None
        self._last_sample_mono: float = 0.0

        # Monotonic time of the first update(); anchors the start-up grace.
        self._first_update_mono: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sample(self) -> Tuple[float, int]:
        """Return ``(cpu_pct_since_last_sample, rss_bytes)`` for this process.

        ``cpu_pct`` is the CPU usage since the previous call (psutil interval=None
        differential mode).  ``__init__`` primes the counter, so the first call
        reports the usage since construction — which includes start-up work
        (see the module docstring on the warm-up grace).

        Calls spaced closer than ``_MIN_SAMPLE_INTERVAL_SEC`` return the cached
        reading: with interval=None the percentage is computed over the wall
        time since the previous call, and a near-zero window right after a busy
        collect cycle would amplify into nonsense (hundreds of percent).
        """
        now = time.monotonic()
        if (
            self._last_sample is not None
            and now - self._last_sample_mono < _MIN_SAMPLE_INTERVAL_SEC
        ):
            return self._last_sample
        cpu = self._proc.cpu_percent(interval=None)
        rss = self._proc.memory_info().rss
        self._last_sample = (cpu, rss)
        self._last_sample_mono = now
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
            ``factor`` grew by ``throttle_factor``: the 60-second moving average
            exceeds the budget, and the last step (if any) is a full window ago.
        ``"throttle_restore"``
            ``factor`` shrank by ``throttle_factor`` (not below 1.0) after 10
            consecutive minutes within budget since the last step.
        ``None``
            No state change (always during the start-up warm-up); the caller
            need not act.
        """
        # Obtain the latest CPU reading (also keeps the differential baseline
        # moving during the warm-up).
        cpu, _rss = self.sample()
        if self._first_update_mono is None:
            self._first_update_mono = mono_now
        if mono_now - self._first_update_mono < self._cfg.warmup_sec:
            return None  # start-up cost is not steady-state overhead
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
            if self._factor < self._max_factor and (
                self._widened_at is None or mono_now - self._widened_at >= _WINDOW_SEC
            ):
                self._factor = min(
                    self._factor * self._cfg.throttle_factor, self._max_factor
                )
                self._widened_at = mono_now
                return "throttle_down"
        elif self._factor > 1.0:
            if self._under_budget_since is None:
                self._under_budget_since = mono_now
            elif mono_now - self._under_budget_since >= _RESTORE_PATIENCE_SEC:
                self._factor = max(1.0, self._factor / self._cfg.throttle_factor)
                self._under_budget_since = mono_now
                return "throttle_restore"

        return None

    @property
    def factor(self) -> float:
        """Current interval multiplier for Tier-2b (always >= 1.0)."""
        return self._factor
