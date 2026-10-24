"""Tests for T10 rules: MEM-PRESSURE-01, SWAP-THRASH-01, CPU-SATURATION-01.

Covers:
- Detect path for each rule.
- Non-detect paths: insufficient data, below threshold, PSI absent.
- engine.run() via only= to confirm proposal is rendered (5-section headers).
"""

from __future__ import annotations

import time

from synth import (
    add_cpu_saturation_samples,
    add_mem_pressure_days,
    add_swap_thrash_samples,
    add_sys_baseline,
    make_db,
)

from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Confidence, Severity, Window
from procdiag.diagnosis import Engine

_MB = 1024 * 1024
_GB = 1024**3
_HOUR = 3600.0


def _cfg(min_obs_hours: float = 1.0) -> ProcdiagConfig:
    return ProcdiagConfig(
        rules=RulesConfig(min_observation_hours=min_obs_hours, disabled=[])
    )


# ---------------------------------------------------------------------------
# MEM-PRESSURE-01
# ---------------------------------------------------------------------------


class TestMemPressure:
    """Tests for MEM-PRESSURE-01 (chronic memory pressure)."""

    def test_detects_chronic_pressure(self, tmp_path):
        """High psi_mem_max recurring on most days + swap_out > 0 → finding."""
        store = make_db(tmp_path, hours=72.0)
        add_sys_baseline(store, hours=72.0)
        add_mem_pressure_days(
            store,
            hours=72.0,
            psi_high=8.0,
            psi_low=0.5,
            pressure_fraction=0.8,
            swap_out_per_row=_MB,
        )
        store.conn.commit()

        from procdiag.diagnosis.memory import MemPressureRule

        rule = MemPressureRule()
        now = time.time()
        window = Window(start_ts=now - 72 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)

        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "MEM-PRESSURE-01"
        assert f.severity == Severity.WARNING
        assert f.evidence["n_pressure_days"] >= 1
        assert f.evidence["swap_out_total_bytes"] > 0
        store.close()

    def test_no_finding_when_psi_below_threshold(self, tmp_path):
        """psi_mem_max always below threshold → no finding."""
        store = make_db(tmp_path, hours=72.0)
        add_sys_baseline(store, hours=72.0)
        # Use psi_high below the default threshold (5.0).
        add_mem_pressure_days(
            store,
            hours=72.0,
            psi_high=3.0,
            psi_low=1.0,
            pressure_fraction=1.0,
            swap_out_per_row=_MB,
        )
        store.conn.commit()

        from procdiag.diagnosis.memory import MemPressureRule

        rule = MemPressureRule()
        now = time.time()
        window = Window(start_ts=now - 72 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_no_finding_when_no_swap_out(self, tmp_path):
        """High psi_mem_max recurring but swap_out == 0 → no finding."""
        store = make_db(tmp_path, hours=72.0)
        add_sys_baseline(store, hours=72.0)
        add_mem_pressure_days(
            store,
            hours=72.0,
            psi_high=8.0,
            pressure_fraction=1.0,
            swap_out_per_row=0,  # ← no swap-out
        )
        store.conn.commit()

        from procdiag.diagnosis.memory import MemPressureRule

        rule = MemPressureRule()
        now = time.time()
        window = Window(start_ts=now - 72 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_no_finding_when_psi_absent(self, tmp_path):
        """All psi_mem_max values are None (PSI absent) → silent."""
        store = make_db(tmp_path, hours=48.0)
        # add_sys_baseline inserts psi_mem_max = 0.0 in rollup rows.
        # We need rollup rows with psi_mem_max = None.
        add_sys_baseline(store, hours=48.0)

        # Overwrite rollup rows with psi_mem_max = NULL.
        store.conn.execute("UPDATE sys_rollup_1m SET psi_mem_max = NULL")
        store.conn.commit()

        from procdiag.diagnosis.memory import MemPressureRule

        rule = MemPressureRule()
        now = time.time()
        window = Window(start_ts=now - 48 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_low_recurrence_fraction_not_detected(self, tmp_path):
        """Pressure on only 1 out of 4 days (25%) → below 50% threshold, no finding."""
        store = make_db(tmp_path, hours=96.0)
        add_sys_baseline(store, hours=96.0)
        add_mem_pressure_days(
            store,
            hours=96.0,
            psi_high=8.0,
            psi_low=0.5,
            pressure_fraction=0.2,  # only 20% of days have pressure
            swap_out_per_row=_MB,
        )
        store.conn.commit()

        from procdiag.diagnosis.memory import MemPressureRule

        rule = MemPressureRule()
        now = time.time()
        window = Window(start_ts=now - 96 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_engine_proposal_five_sections(self, tmp_path):
        """MEM-PRESSURE-01 proposal via Engine.run() contains all 5 section headers."""
        store = make_db(tmp_path, hours=72.0)
        add_sys_baseline(store, hours=72.0)
        add_mem_pressure_days(
            store,
            hours=72.0,
            psi_high=8.0,
            pressure_fraction=0.8,
            swap_out_per_row=_MB,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=1.0))
        diag = engine.run(store, only=["MEM-PRESSURE-01"])
        findings = [f for f in diag.findings if f.rule_id == "MEM-PRESSURE-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section {header!r}"
        store.close()


# ---------------------------------------------------------------------------
# SWAP-THRASH-01
# ---------------------------------------------------------------------------


class TestSwapThrash:
    """Tests for SWAP-THRASH-01 (simultaneous swap in+out with full-stall PSI)."""

    def test_detects_thrashing(self, tmp_path):
        """Simultaneous swap_in + swap_out + psi_mem_full10 > 0 → CRITICAL finding."""
        store = make_db(tmp_path, hours=2.0)
        add_swap_thrash_samples(
            store,
            n_thrash=5,
            psi_full=15.0,
            swap_pages=100,
            baseline_hours=1.0,
        )
        store.conn.commit()

        from procdiag.diagnosis.memory import SwapThrashRule

        rule = SwapThrashRule()
        now = time.time()
        window = Window(start_ts=now - 2 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)

        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "SWAP-THRASH-01"
        assert f.severity == Severity.CRITICAL
        assert f.confidence == Confidence.HIGH
        assert f.evidence["thrash_event_count"] == 5
        assert f.evidence["swap_in_total_pages"] > 0
        assert f.evidence["swap_out_total_pages"] > 0
        store.close()

    def test_no_thrash_when_psi_full_zero(self, tmp_path):
        """swap_in + swap_out present but psi_mem_full10 == 0 → no finding."""
        store = make_db(tmp_path, hours=2.0)
        # Insert thrash-like samples but with psi_full = 0.
        add_swap_thrash_samples(
            store,
            n_thrash=5,
            psi_full=0.0,  # ← no full-stall PSI
            swap_pages=100,
            baseline_hours=1.0,
        )
        store.conn.commit()

        from procdiag.diagnosis.memory import SwapThrashRule

        rule = SwapThrashRule()
        now = time.time()
        window = Window(start_ts=now - 2 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_no_thrash_when_no_swap_activity(self, tmp_path):
        """No swap activity at all → no SWAP-THRASH-01 finding."""
        store = make_db(tmp_path, hours=2.0)
        add_sys_baseline(store, hours=2.0)
        store.conn.commit()

        from procdiag.diagnosis.memory import SwapThrashRule

        rule = SwapThrashRule()
        now = time.time()
        window = Window(start_ts=now - 2 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_no_finding_when_psi_absent(self, tmp_path):
        """All psi_mem_full10 values are None (PSI absent DB) → silent."""
        store = make_db(tmp_path, hours=2.0)
        add_sys_baseline(store, hours=2.0)
        store.conn.execute("UPDATE sys_samples SET psi_mem_full10 = NULL")
        store.conn.commit()

        from procdiag.diagnosis.memory import SwapThrashRule

        rule = SwapThrashRule()
        now = time.time()
        window = Window(start_ts=now - 2 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_engine_proposal_five_sections(self, tmp_path):
        """SWAP-THRASH-01 proposal via Engine.run() contains all 5 section headers."""
        store = make_db(tmp_path, hours=2.0)
        add_swap_thrash_samples(store, n_thrash=5, psi_full=15.0)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=0.1))
        diag = engine.run(store, only=["SWAP-THRASH-01"])
        findings = [f for f in diag.findings if f.rule_id == "SWAP-THRASH-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section {header!r}"
        store.close()


# ---------------------------------------------------------------------------
# CPU-SATURATION-01
# ---------------------------------------------------------------------------


class TestCpuSaturation:
    """Tests for CPU-SATURATION-01 (sustained PSI CPU pressure)."""

    def test_detects_saturation(self, tmp_path):
        """psi_cpu_some10 > 20 for 20 consecutive minutes → WARNING finding."""
        store = make_db(tmp_path, hours=1.0)
        add_cpu_saturation_samples(
            store,
            psi_cpu_high=35.0,
            psi_cpu_low=2.0,
            duration_minutes=20,
            baseline_minutes=5,
        )
        store.conn.commit()

        from procdiag.diagnosis.cpu import CpuSaturationRule

        rule = CpuSaturationRule()
        now = time.time()
        window = Window(start_ts=now - _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)

        assert len(findings) >= 1
        f = findings[0]
        assert f.rule_id == "CPU-SATURATION-01"
        assert f.severity == Severity.WARNING
        assert f.evidence["duration_minutes"] >= 15
        assert f.evidence["peak_psi_cpu_some10"] > 20.0
        store.close()

    def test_no_finding_below_threshold(self, tmp_path):
        """psi_cpu_some10 always below 20% → no finding."""
        store = make_db(tmp_path, hours=1.0)
        add_cpu_saturation_samples(
            store,
            psi_cpu_high=10.0,  # below default threshold of 20
            psi_cpu_low=2.0,
            duration_minutes=30,
            baseline_minutes=5,
        )
        store.conn.commit()

        from procdiag.diagnosis.cpu import CpuSaturationRule

        rule = CpuSaturationRule()
        now = time.time()
        window = Window(start_ts=now - _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_no_finding_short_episode(self, tmp_path):
        """psi_cpu_some10 > 20 for only 5 minutes → below 15-minute threshold."""
        store = make_db(tmp_path, hours=1.0)
        add_cpu_saturation_samples(
            store,
            psi_cpu_high=35.0,
            psi_cpu_low=2.0,
            duration_minutes=5,  # too short
            baseline_minutes=5,
        )
        store.conn.commit()

        from procdiag.diagnosis.cpu import CpuSaturationRule

        rule = CpuSaturationRule()
        now = time.time()
        window = Window(start_ts=now - _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_no_finding_when_psi_absent(self, tmp_path):
        """All psi_cpu_some10 values are None (PSI absent DB) → silent."""
        store = make_db(tmp_path, hours=1.0)
        add_sys_baseline(store, hours=1.0)
        store.conn.execute("UPDATE sys_samples SET psi_cpu_some10 = NULL")
        store.conn.commit()

        from procdiag.diagnosis.cpu import CpuSaturationRule

        rule = CpuSaturationRule()
        now = time.time()
        window = Window(start_ts=now - _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_no_finding_empty_db(self, tmp_path):
        """Empty sys_samples → no CPU-SATURATION-01 finding."""
        store = make_db(tmp_path, hours=1.0)
        store.conn.commit()

        from procdiag.diagnosis.cpu import CpuSaturationRule

        rule = CpuSaturationRule()
        now = time.time()
        window = Window(start_ts=now - _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) == 0
        store.close()

    def test_engine_proposal_five_sections(self, tmp_path):
        """CPU-SATURATION-01 proposal via Engine.run() contains all 5 section headers."""
        store = make_db(tmp_path, hours=1.0)
        add_cpu_saturation_samples(
            store,
            psi_cpu_high=35.0,
            duration_minutes=20,
            baseline_minutes=5,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=0.1))
        diag = engine.run(store, only=["CPU-SATURATION-01"])
        findings = [f for f in diag.findings if f.rule_id == "CPU-SATURATION-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section {header!r}"
        store.close()

    def test_names_culprit_from_recorded_spike_payload(self, tmp_path):
        """Spikes recorded by the recorder name the episode's top process.

        Regression: the rule used to call ``.get()`` on ``sqlite3.Row`` and
        expect a list payload, so any spike inside the episode crashed it
        (the finding was silently dropped by the engine's firewall).
        """
        from procdiag.diagnosis.cpu import CpuSaturationRule
        from procdiag.spikes import encode_top_processes

        store = make_db(tmp_path, hours=1.0)
        start_ts = time.time() - 30 * 60.0
        add_cpu_saturation_samples(
            store,
            start_ts=start_ts,
            psi_cpu_high=35.0,
            duration_minutes=20,
            baseline_minutes=5,
        )
        hog = store.proc_id_for("hogd", "h1", "hogd --busy", start_ts)
        idle = store.proc_id_for("idled", "h2", "idled", start_ts)
        for i in range(3):
            ts = start_ts + (6 + i * 5) * 60.0  # inside the 20-minute episode
            payload = encode_top_processes(
                [
                    {"ts": ts, "proc_id": hog, "pid": 42, "cpu_pct": 97.0, "rss": 1},
                    {"ts": ts, "proc_id": idle, "pid": 7, "cpu_pct": 0.5, "rss": 1},
                ]
            )
            store.conn.execute(
                "INSERT INTO spike_events (ts, trigger_type, psi_snapshot, "
                "top_processes, top_cgroups) VALUES (?, 'cpu', '{}', ?, '{}')",
                (ts, payload),
            )
        store.conn.commit()

        now = time.time()
        findings = CpuSaturationRule().evaluate(
            store, Window(start_ts=now - _HOUR, end_ts=now)
        )

        assert len(findings) == 1
        f = findings[0]
        assert f.subject == "hogd"
        assert f.evidence["top_processes"][0] == "hogd"
        assert f.evidence["spike_event_count"] == 3
        store.close()

    def test_high_confidence_for_long_episode(self, tmp_path):
        """Episode >= 30 minutes should produce HIGH confidence."""
        store = make_db(tmp_path, hours=2.0)
        add_cpu_saturation_samples(
            store,
            psi_cpu_high=35.0,
            duration_minutes=35,  # >= 30 → HIGH
            baseline_minutes=5,
        )
        store.conn.commit()

        from procdiag.diagnosis.cpu import CpuSaturationRule

        rule = CpuSaturationRule()
        now = time.time()
        window = Window(start_ts=now - 2 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, window)
        assert len(findings) >= 1
        assert findings[0].confidence == Confidence.HIGH
        store.close()
