"""Spike-trigger: PSI threshold watcher with debounce and storm aggregation.

Maps to netdiag's ``alerting.StateMachine`` but is tailored for the
*procdiag* multi-type scenario (cpu / memory / io) rather than a single
healthy/degraded channel.

Design decisions that mirror alerting.py:

* ``observe`` receives the *current* system snapshot and the caller's
  monotonic clock value rather than using ``time.monotonic()`` directly,
  so the caller (and tests) retain full control of the clock.

* Per-type debounce: a given ``trigger_type`` cannot re-fire until
  ``spike_cooldown_sec`` seconds have elapsed since its last firing
  (monotonic clock).  Different types are tracked independently.

* Storm aggregation: once the total fire count within the trailing 5-minute
  window reaches ``spike_storm_threshold``, individual triggers are suppressed
  and a single ``trigger_type="storm"`` decision is emitted.  The system
  stays in storm mode (silent except for the one storm event already emitted)
  until the window count falls below ``storm_threshold / 2`` (hysteresis).
  After the storm clears, normal operation resumes.

PSI fallback (``psi_available=False``): instead of reading PSI fields the
trigger uses coarse system metrics from ``SystemSnapshot``:

  * cpu_total > 85  →  "cpu"
  * mem_available / mem_total < 0.10  →  "memory"
  * swap_out_delta > 0  →  "io"  (I/O proxy; same bucket as io PSI)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque

from ..config import TriggerConfig
from ..core.sampler import SystemSnapshot

# Length of the storm-detection sliding window in seconds.
_STORM_WINDOW_SEC = 300.0  # 5 minutes


@dataclass
class SpikeDecision:
    """Describes a single trigger event emitted by :class:`SpikeTrigger`.

    ``trigger_type`` is one of ``"cpu"``, ``"memory"``, ``"io"``, or
    ``"storm"`` (the last is produced only during storm mode).
    ``reason`` is a short Japanese string suitable for logging and the
    spike_events table.  ``psi_snapshot`` carries the raw PSI dict from the
    triggering ``SystemSnapshot``; it is ``None`` when PSI is unavailable.
    """

    trigger_type: str  # "cpu" | "memory" | "io" | "storm"
    reason: str  # 日本語の発火理由
    psi_snapshot: dict | None  # SystemSnapshot.psi at fire time


class SpikeTrigger:
    """Monitor a stream of ``SystemSnapshot`` observations for resource spikes.

    Instantiate once and call :meth:`observe` on every Tier-1 tick.

    Parameters
    ----------
    cfg:
        ``TriggerConfig`` from the main ``ProcdiagConfig``.
    psi_available:
        Set to ``True`` when ``/proc/pressure/*`` is readable.  The trigger
        then uses PSI ``some/full avg10`` values.  When ``False`` it falls
        back to the coarse CPU %, memory-available ratio, and swap-out delta
        from the snapshot.
    """

    def __init__(self, cfg: TriggerConfig, *, psi_available: bool) -> None:
        self._cfg = cfg
        self._psi_available = psi_available

        # Per-type: monotonic timestamp of the last successful fire.
        # Missing key ↔ never fired.
        self._last_fire: dict[str, float] = {}

        # Sliding window of (mono_now, trigger_type) pairs for storm detection.
        # We only need the timestamps so a plain deque is sufficient.
        self._fire_times: Deque[float] = deque()

        # Storm state:
        # _in_storm       — True while we are in storm mode.
        # _storm_emitted  — True after the single storm event has been returned
        #                   (subsequent ticks in storm mode return None).
        self._in_storm: bool = False
        self._storm_emitted: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def observe(self, snap: SystemSnapshot, mono_now: float) -> SpikeDecision | None:
        """Evaluate *snap* and return a :class:`SpikeDecision` or ``None``.

        ``mono_now`` must be ``time.monotonic()`` (or a synthetic value in
        tests).  It is used both for debounce checks and for maintaining the
        storm-detection window.
        """
        # Purge stale entries from the sliding window.
        self._evict_old(mono_now)

        window_count = len(self._fire_times)
        threshold = self._cfg.spike_storm_threshold
        half = threshold / 2.0

        # --- Storm exit check (hysteresis) -----------------------------------
        if self._in_storm:
            if window_count < half:
                # Storm cleared — resume normal operation.
                self._in_storm = False
                self._storm_emitted = False
            else:
                # Still in storm; stay silent.
                return None

        # --- Storm entry check -----------------------------------------------
        if window_count >= threshold:
            self._in_storm = True
            if not self._storm_emitted:
                self._storm_emitted = True
                return SpikeDecision(
                    trigger_type="storm",
                    reason=f"スパイクストーム: 直近5分で{window_count}回発火 (閾値: {threshold}回)",
                    psi_snapshot=snap.psi,
                )
            return None

        # --- Normal evaluation -----------------------------------------------
        candidate = self._evaluate(snap)
        if candidate is None:
            return None

        # Debounce: suppress if the same type fired too recently.
        cooldown = float(self._cfg.spike_cooldown_sec)
        last = self._last_fire.get(candidate.trigger_type)
        if last is not None and (mono_now - last) < cooldown:
            return None

        # Record this fire in the per-type tracker and the sliding window.
        self._last_fire[candidate.trigger_type] = mono_now
        self._fire_times.append(mono_now)

        # Re-check storm entry after recording (the new fire may tip it over).
        if len(self._fire_times) >= threshold:
            self._in_storm = True
            if not self._storm_emitted:
                self._storm_emitted = True
                return SpikeDecision(
                    trigger_type="storm",
                    reason=(
                        f"スパイクストーム: 直近5分で{len(self._fire_times)}回発火"
                        f" (閾値: {threshold}回)"
                    ),
                    psi_snapshot=snap.psi,
                )
            return None

        return candidate

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _evict_old(self, mono_now: float) -> None:
        """Remove fire records older than the 5-minute storm window."""
        cutoff = mono_now - _STORM_WINDOW_SEC
        while self._fire_times and self._fire_times[0] <= cutoff:
            self._fire_times.popleft()

    def _evaluate(self, snap: SystemSnapshot) -> SpikeDecision | None:
        """Check thresholds and return the first matching decision, or None."""
        if self._psi_available:
            return self._evaluate_psi(snap)
        return self._evaluate_fallback(snap)

    def _evaluate_psi(self, snap: SystemSnapshot) -> SpikeDecision | None:
        """PSI-based evaluation (kernel ≥ 4.20 with CONFIG_PSI=y)."""
        psi = snap.psi or {}

        cpu_val = psi.get("cpu_some10", 0.0)
        if cpu_val > self._cfg.psi_cpu_some10:
            return SpikeDecision(
                trigger_type="cpu",
                reason=f"PSI cpu some10={cpu_val:.1f} > {self._cfg.psi_cpu_some10}",
                psi_snapshot=snap.psi,
            )

        mem_some = psi.get("mem_some10", 0.0)
        mem_full = psi.get("mem_full10", 0.0)
        if mem_some > self._cfg.psi_mem_some10 or mem_full > self._cfg.psi_mem_full10:
            label = (
                f"PSI mem some10={mem_some:.1f} > {self._cfg.psi_mem_some10}"
                if mem_some > self._cfg.psi_mem_some10
                else f"PSI mem full10={mem_full:.1f} > {self._cfg.psi_mem_full10}"
            )
            return SpikeDecision(
                trigger_type="memory",
                reason=label,
                psi_snapshot=snap.psi,
            )

        io_val = psi.get("io_some10", 0.0)
        if io_val > self._cfg.psi_io_some10:
            return SpikeDecision(
                trigger_type="io",
                reason=f"PSI io some10={io_val:.1f} > {self._cfg.psi_io_some10}",
                psi_snapshot=snap.psi,
            )

        return None

    def _evaluate_fallback(self, snap: SystemSnapshot) -> SpikeDecision | None:
        """Coarse fallback when PSI is unavailable (older kernels)."""
        if snap.cpu_total > 85.0:
            return SpikeDecision(
                trigger_type="cpu",
                reason=f"CPU使用率={snap.cpu_total:.1f}% > 85% (PSI非対応フォールバック)",
                psi_snapshot=None,
            )

        if snap.mem_total > 0:
            avail_ratio = snap.mem_available / snap.mem_total
            if avail_ratio < 0.10:
                pct = avail_ratio * 100.0
                return SpikeDecision(
                    trigger_type="memory",
                    reason=(
                        f"メモリavailable={pct:.1f}% < 10% (PSI非対応フォールバック)"
                    ),
                    psi_snapshot=None,
                )

        if snap.swap_out_delta > 0:
            return SpikeDecision(
                trigger_type="io",
                reason=(
                    f"スワップアウト増分={snap.swap_out_delta}bytes > 0"
                    " (PSI非対応フォールバック)"
                ),
                psi_snapshot=None,
            )

        return None
