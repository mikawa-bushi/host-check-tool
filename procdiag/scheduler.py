"""Monotonic-clock multi-tier scheduler for the procdiag record loop.

Maps to netdiag's ``scheduler.py`` (Monitor._loop) but generalises to N
named jobs each with an independent interval.  A single-thread approach is
deliberate: procdiag samplers produce negligible I/O waits, so
``threading.Thread`` overhead would exceed the benefit of parallelism.

Design goals:
- Drift-free scheduling: each job's next fire time is updated as
  ``next_t += interval`` rather than ``next_t = now + interval``.  Over a
  72-hour run the cumulative drift would otherwise amount to minutes.
- Promptly stoppable: the run loop checks the stop event every 0.5 s so
  ``SIGTERM`` latency is bounded even with multi-second intervals.
- Gap detection: when the elapsed time since a job's scheduled fire time
  exceeds ``3 × interval``, an ``on_gap`` callback is invoked and
  ``next_t`` is re-anchored to ``now`` to avoid a burst of catch-up fires.
- Resilient: each job is wrapped in ``try/except``; one failing job never
  kills the loop.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Maximum time the loop sleeps between wake-ups.  Keeping this ≤ 0.5 s
# ensures the stop event is noticed within half a second.
_MAX_SLEEP = 0.5


@dataclass
class _Job:
    """Internal state for one registered job."""

    name: str
    interval: float | Callable[[], float]
    fn: Callable[[], None]
    next_t: float = field(default=0.0)

    def current_interval(self) -> float:
        """Evaluate the interval, which may be a callable (e.g. for T2b throttle)."""
        if callable(self.interval):
            return self.interval()
        return self.interval


class TierScheduler:
    """Single-thread monotonic-clock scheduler for multiple periodic jobs.

    Usage::

        sched = TierScheduler()
        sched.add("t1", 1.0, sample_system)
        sched.add("t2b", lambda: proc_interval * selfmeter.factor, sample_process)
        stop = threading.Event()
        sched.run(stop)   # blocks until stop.set()
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_gap: Optional[Callable[[float, str], None]] = None,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._on_gap = on_gap
        self._jobs: list[_Job] = []

    def add(
        self,
        name: str,
        interval: float | Callable[[], float],
        fn: Callable[[], None],
    ) -> None:
        """Register a periodic job.

        *interval* may be a plain ``float`` (seconds) or a zero-argument
        callable that returns ``float``.  The callable form is evaluated
        before each scheduling decision so T2b can react to the selfmeter
        factor in real time.
        """
        job = _Job(name=name, interval=interval, fn=fn)
        # Schedule the first fire immediately (next_t = now so it fires on the
        # first loop iteration).
        job.next_t = self._clock()
        self._jobs.append(job)

    def run(self, stop: threading.Event) -> None:
        """Run the scheduling loop until *stop* is set.

        The loop ticks at most every ``_MAX_SLEEP`` seconds so the stop
        event is noticed promptly regardless of job intervals.
        """
        while not stop.is_set():
            now = self._clock()

            for job in self._jobs:
                if now < job.next_t:
                    continue

                # Check for gap: if we are more than 3× the interval late,
                # record the gap and re-anchor next_t to now.
                interval = job.current_interval()
                delay = now - job.next_t
                if delay > 3.0 * interval:
                    if self._on_gap is not None:
                        try:
                            self._on_gap(delay, job.name)
                        except Exception:
                            logger.exception(
                                "on_gap コールバック例外 (job=%s)", job.name
                            )
                    # Re-anchor to prevent a burst of immediate catch-up fires.
                    job.next_t = now + interval
                else:
                    job.next_t += interval
                    # Skip periods that already passed entirely: replaying
                    # them back-to-back would record near-duplicate samples
                    # and break differential meters (cpu_percent windows of
                    # a few ms).  A missed tick stays missed.
                    while job.next_t <= now:
                        job.next_t += interval

                # Execute the job — never let one failure kill the loop.
                try:
                    job.fn()
                except Exception:
                    logger.exception("ジョブ例外 (job=%s)", job.name)

            # Sleep only until the next job is due, but cap at _MAX_SLEEP so
            # the stop event is checked frequently.
            if self._jobs:
                nearest = min(j.next_t for j in self._jobs)
                wait = max(0.0, min(nearest - self._clock(), _MAX_SLEEP))
            else:
                wait = _MAX_SLEEP

            if wait > 0:
                self._sleep(wait)
