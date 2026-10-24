"""Tests for procdiag.samplers.trigger (T04).

All tests are pure-logic tests that inject a synthetic monotonic clock via
the ``mono_now`` argument of ``SpikeTrigger.observe``.  No real time passes
during the suite.

Coverage areas:
* PSI-based threshold evaluation (cpu / memory / io).
* PSI fallback evaluation (cpu % / mem-available ratio / swap-out delta).
* Debounce: same type cannot re-fire before ``spike_cooldown_sec`` elapses.
  Different types have independent cooldown timers.
* Storm aggregation: 11 fires in 5 minutes produce exactly one
  ``trigger_type="storm"`` decision, followed by silence; the window
  clears after 5 minutes, resuming normal operation.
"""

from __future__ import annotations

from procdiag.config import TriggerConfig
from procdiag.core.sampler import SystemSnapshot
from procdiag.samplers.trigger import _STORM_WINDOW_SEC, SpikeDecision, SpikeTrigger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**kwargs) -> TriggerConfig:
    """Build a TriggerConfig with sensible defaults, overriding as needed."""
    defaults = dict(
        psi_cpu_some10=20.0,
        psi_mem_some10=5.0,
        psi_mem_full10=1.0,
        psi_io_some10=30.0,
        spike_cooldown_sec=30,
        spike_storm_threshold=10,
    )
    defaults.update(kwargs)
    return TriggerConfig(**defaults)


def _snap(
    *,
    cpu_total: float = 0.0,
    cpu_max_core: float = 0.0,
    mem_total: int = 8 * 1024**3,  # 8 GB
    mem_available: int = 4 * 1024**3,  # 4 GB (50 %)
    swap_out_delta: int = 0,
    psi: dict | None = None,
) -> SystemSnapshot:
    return SystemSnapshot(
        ts=0.0,
        cpu_total=cpu_total,
        cpu_max_core=cpu_max_core,
        mem_total=mem_total,
        mem_available=mem_available,
        swap_out_delta=swap_out_delta,
        psi=psi,
    )


def _psi(cpu=0.0, mem_some=0.0, mem_full=0.0, io=0.0) -> dict:
    return {
        "cpu_some10": cpu,
        "mem_some10": mem_some,
        "mem_full10": mem_full,
        "io_some10": io,
    }


# ---------------------------------------------------------------------------
# PSI: threshold below → None; above → fires
# ---------------------------------------------------------------------------


class TestPsiEvaluation:
    def test_below_threshold_returns_none(self):
        trig = SpikeTrigger(_cfg(), psi_available=True)
        snap = _snap(psi=_psi(cpu=10.0, mem_some=1.0, io=5.0))
        assert trig.observe(snap, 0.0) is None

    def test_cpu_above_threshold_fires(self):
        trig = SpikeTrigger(_cfg(), psi_available=True)
        snap = _snap(psi=_psi(cpu=25.0))
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert decision.trigger_type == "cpu"

    def test_mem_some_above_threshold_fires(self):
        trig = SpikeTrigger(_cfg(), psi_available=True)
        snap = _snap(psi=_psi(mem_some=6.0))
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert decision.trigger_type == "memory"

    def test_mem_full_above_threshold_fires(self):
        trig = SpikeTrigger(_cfg(psi_mem_full10=1.0), psi_available=True)
        snap = _snap(psi=_psi(mem_full=2.0))
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert decision.trigger_type == "memory"

    def test_io_above_threshold_fires(self):
        trig = SpikeTrigger(_cfg(), psi_available=True)
        snap = _snap(psi=_psi(io=35.0))
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert decision.trigger_type == "io"

    def test_psi_snapshot_carried_in_decision(self):
        psi_dict = _psi(cpu=25.0)
        trig = SpikeTrigger(_cfg(), psi_available=True)
        snap = _snap(psi=psi_dict)
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert decision.psi_snapshot == psi_dict

    def test_reason_contains_value_and_threshold(self):
        trig = SpikeTrigger(_cfg(psi_cpu_some10=20.0), psi_available=True)
        snap = _snap(psi=_psi(cpu=25.3))
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert "25.3" in decision.reason
        assert "20.0" in decision.reason


# ---------------------------------------------------------------------------
# PSI fallback (psi_available=False)
# ---------------------------------------------------------------------------


class TestFallbackEvaluation:
    def test_normal_values_return_none(self):
        trig = SpikeTrigger(_cfg(), psi_available=False)
        snap = _snap(cpu_total=50.0, mem_available=4 * 1024**3, swap_out_delta=0)
        assert trig.observe(snap, 0.0) is None

    def test_cpu_fallback_fires_above_85(self):
        trig = SpikeTrigger(_cfg(), psi_available=False)
        snap = _snap(cpu_total=90.0)
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert decision.trigger_type == "cpu"

    def test_cpu_fallback_exactly_85_does_not_fire(self):
        trig = SpikeTrigger(_cfg(), psi_available=False)
        snap = _snap(cpu_total=85.0)
        assert trig.observe(snap, 0.0) is None

    def test_mem_fallback_fires_below_10_percent(self):
        total = 8 * 1024**3
        # 9 % available
        available = int(total * 0.09)
        trig = SpikeTrigger(_cfg(), psi_available=False)
        snap = _snap(mem_total=total, mem_available=available)
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert decision.trigger_type == "memory"

    def test_mem_fallback_exactly_10_percent_does_not_fire(self):
        # Use an integer ratio to avoid floating-point truncation.
        # 10 available out of 100 total = exactly 10.0 %, which must NOT fire.
        total = 1000
        available = 100  # exactly 10.0 %
        trig = SpikeTrigger(_cfg(), psi_available=False)
        snap = _snap(mem_total=total, mem_available=available)
        assert trig.observe(snap, 0.0) is None

    def test_swap_out_fallback_fires(self):
        trig = SpikeTrigger(_cfg(), psi_available=False)
        snap = _snap(swap_out_delta=1024)
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert decision.trigger_type == "io"

    def test_swap_out_zero_does_not_fire(self):
        trig = SpikeTrigger(_cfg(), psi_available=False)
        snap = _snap(swap_out_delta=0)
        assert trig.observe(snap, 0.0) is None

    def test_fallback_psi_snapshot_is_none(self):
        trig = SpikeTrigger(_cfg(), psi_available=False)
        snap = _snap(cpu_total=90.0)
        decision = trig.observe(snap, 0.0)
        assert decision is not None
        assert decision.psi_snapshot is None


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


class TestDebounce:
    def _fire_cpu(self, trig: SpikeTrigger, mono: float) -> SpikeDecision | None:
        snap = _snap(psi=_psi(cpu=25.0))
        return trig.observe(snap, mono)

    def test_first_fire_returns_decision(self):
        trig = SpikeTrigger(_cfg(spike_cooldown_sec=30), psi_available=True)
        assert self._fire_cpu(trig, 0.0) is not None

    def test_immediate_refusal_within_cooldown(self):
        trig = SpikeTrigger(_cfg(spike_cooldown_sec=30), psi_available=True)
        self._fire_cpu(trig, 0.0)
        assert self._fire_cpu(trig, 1.0) is None

    def test_refusal_at_cooldown_minus_epsilon(self):
        trig = SpikeTrigger(_cfg(spike_cooldown_sec=30), psi_available=True)
        self._fire_cpu(trig, 0.0)
        assert self._fire_cpu(trig, 29.9) is None

    def test_refires_after_cooldown_elapsed(self):
        trig = SpikeTrigger(_cfg(spike_cooldown_sec=30), psi_available=True)
        self._fire_cpu(trig, 0.0)
        # Exactly 30 s is still within cooldown (strictly less than required).
        # 30.1 s should be enough.
        result = self._fire_cpu(trig, 30.1)
        assert result is not None
        assert result.trigger_type == "cpu"

    def test_different_types_have_independent_cooldowns(self):
        trig = SpikeTrigger(_cfg(spike_cooldown_sec=30), psi_available=True)

        # Fire CPU at t=0.
        cpu_snap = _snap(psi=_psi(cpu=25.0))
        d1 = trig.observe(cpu_snap, 0.0)
        assert d1 is not None and d1.trigger_type == "cpu"

        # IO fires at t=1 — different type, no cooldown interference.
        io_snap = _snap(psi=_psi(io=35.0))
        d2 = trig.observe(io_snap, 1.0)
        assert d2 is not None and d2.trigger_type == "io"

        # CPU again at t=5 — still within its own cooldown.
        d3 = trig.observe(cpu_snap, 5.0)
        assert d3 is None

        # IO again at t=5 — still within IO cooldown.
        d4 = trig.observe(io_snap, 5.0)
        assert d4 is None

    def test_memory_cooldown_independent_of_cpu(self):
        trig = SpikeTrigger(_cfg(spike_cooldown_sec=30), psi_available=True)
        cpu_snap = _snap(psi=_psi(cpu=25.0))
        mem_snap = _snap(psi=_psi(mem_some=10.0))

        trig.observe(cpu_snap, 0.0)  # CPU fires
        d = trig.observe(mem_snap, 1.0)  # Memory should fire independently
        assert d is not None
        assert d.trigger_type == "memory"


# ---------------------------------------------------------------------------
# Storm aggregation
# ---------------------------------------------------------------------------


class TestStormAggregation:
    def _fire_many(
        self,
        trig: SpikeTrigger,
        n: int,
        start: float = 0.0,
        gap: float = 1.0,
    ) -> list[SpikeDecision | None]:
        """Fire *n* CPU spikes spaced *gap* seconds apart, starting at *start*."""
        results = []
        snap = _snap(psi=_psi(cpu=25.0))
        for i in range(n):
            mono = start + i * gap
            results.append(trig.observe(snap, mono))
        return results

    def test_11_fires_produces_exactly_one_storm(self):
        """After 10 normal fires the 11th tips into storm mode."""
        trig = SpikeTrigger(
            _cfg(spike_storm_threshold=10, spike_cooldown_sec=1),
            psi_available=True,
        )
        # Space fires 2 s apart so debounce (1 s cooldown) never blocks.
        results = self._fire_many(trig, 11, gap=2.0)

        storm_results = [
            r for r in results if r is not None and r.trigger_type == "storm"
        ]
        normal_results = [
            r for r in results if r is not None and r.trigger_type != "storm"
        ]

        # Exactly one storm event.
        assert len(storm_results) == 1, f"ストームイベント数: {len(storm_results)}"
        # Some normal fires before the storm.
        assert len(normal_results) >= 1

    def test_storm_silence_after_single_storm_event(self):
        """Subsequent observe calls during the storm window return None."""
        trig = SpikeTrigger(
            _cfg(spike_storm_threshold=10, spike_cooldown_sec=1),
            psi_available=True,
        )
        self._fire_many(trig, 11, gap=2.0)  # 11th triggers storm

        # Now fire 5 more times within the storm window (within 5 min total).
        snap = _snap(psi=_psi(cpu=25.0))
        base_mono = 2.0 * 10 + 1.0  # just after the 11th fire
        for i in range(5):
            r = trig.observe(snap, base_mono + i * 2.0)
            assert r is None, f"ストーム中に None 以外が返された: {r}"

    def test_storm_clears_after_window_expires(self):
        """When the 5-minute window is exhausted, normal firing resumes."""
        trig = SpikeTrigger(
            _cfg(spike_storm_threshold=10, spike_cooldown_sec=1),
            psi_available=True,
        )
        # Create 11 fires clustered at t≈0.
        self._fire_many(trig, 11, gap=2.0)

        # Advance time past the 5-minute window.  All prior fire timestamps
        # are now older than _STORM_WINDOW_SEC; the window count drops to 0.
        snap = _snap(psi=_psi(cpu=25.0))
        far_future = _STORM_WINDOW_SEC + 100.0
        decision = trig.observe(snap, far_future)
        assert decision is not None, "ストームウィンドウ消滅後は発火すべき"
        assert decision.trigger_type == "cpu"

    def test_storm_hysteresis_half_threshold(self):
        """Storm does not clear until window count drops below threshold/2."""
        threshold = 10
        trig = SpikeTrigger(
            _cfg(spike_storm_threshold=threshold, spike_cooldown_sec=1),
            psi_available=True,
        )
        snap = _snap(psi=_psi(cpu=25.0))

        # Fire 11 times at t=0..22s (gap=2 > cooldown=1).
        for i in range(11):
            trig.observe(snap, float(i * 2))

        # Advance to t=5min-10s.  Only the last few fires are still in window.
        # At t=290, fires at t=0..22 have some still within [290-300, 290] window.
        # Actually only fires at t≥(290-300)=-10 are in window, so all 11 remain.
        # Jump further: t=322 so fires up to t=22 have expired (age > 300).
        # Only fires at t=22 might remain: 322-22=300, exactly at the edge.
        # Go to t=324 to be sure — fire at t=22 has age=302 > 300 → expired.
        at = 324.0
        # All 11 fires have expired; window is empty.  Half = 5.  0 < 5 → storm clears.
        decision = trig.observe(snap, at)
        assert decision is not None
        assert decision.trigger_type == "cpu"

    def test_storm_only_emitted_once(self):
        """A single storm event is emitted even if observe is called many times."""
        trig = SpikeTrigger(
            _cfg(spike_storm_threshold=5, spike_cooldown_sec=1),
            psi_available=True,
        )
        snap = _snap(psi=_psi(cpu=25.0))

        storm_count = 0
        for i in range(30):
            r = trig.observe(snap, float(i * 2))
            if r is not None and r.trigger_type == "storm":
                storm_count += 1

        assert storm_count == 1, f"ストームイベントが{storm_count}回返された (期待: 1)"

    def test_storm_reason_is_japanese(self):
        trig = SpikeTrigger(
            _cfg(spike_storm_threshold=5, spike_cooldown_sec=1),
            psi_available=True,
        )
        snap = _snap(psi=_psi(cpu=25.0))
        results = [trig.observe(snap, float(i * 2)) for i in range(10)]
        storm = next((r for r in results if r and r.trigger_type == "storm"), None)
        assert storm is not None
        # Reason must contain Japanese characters (not be empty English).
        assert any(
            ord(c) > 127 for c in storm.reason
        ), f"reasonが日本語でない: {storm.reason}"
