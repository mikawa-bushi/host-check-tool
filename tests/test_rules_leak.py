"""Tests for the FD-LEAK-01, THREAD-LEAK-01, and ZOMBIE-01 rules.

Uses the synthetic DB builder from ``tests/synth.py`` and verifies:
- FD-LEAK-01: detects FD leak / does not detect flat series / short observation guard
- THREAD-LEAK-01: detects thread leak / does not detect flat series / short observation guard
- ZOMBIE-01: detects zombie accumulation / does not detect flat zombie count /
  short observation guard
- Proposal 5-section headers for all three rules
"""

from __future__ import annotations

import pytest
from synth import (
    add_proc_series,
    add_sys_baseline,
    add_zombie_series,
    make_db,
)

from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Confidence, Severity
from procdiag.diagnosis import Engine

_MB = 1024 * 1024


# ---------------------------------------------------------------------------
# Config helper
# ---------------------------------------------------------------------------


def _cfg(
    min_obs_hours: float = 6.0, disabled: list[str] | None = None
) -> ProcdiagConfig:
    return ProcdiagConfig(
        rules=RulesConfig(
            min_observation_hours=min_obs_hours,
            disabled=disabled or [],
        )
    )


# ---------------------------------------------------------------------------
# FD-LEAK-01
# ---------------------------------------------------------------------------


class TestFdLeak:
    def test_fd_leak_detected(self, tmp_path):
        """Steep FD slope + long observation → FD-LEAK-01 finding."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "leaky_server",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=18.0,
            fds_slope=5.0,  # 5 FDs/h — well above threshold of 0.5
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0), verbose=True)
        diag = engine.run(store, only=["FD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "FD-LEAK-01"]
        assert len(findings) >= 1
        f = findings[0]
        assert f.subject == "leaky_server"
        assert f.severity == Severity.WARNING
        assert f.evidence["slope_fd_per_h"] > 0
        assert f.evidence["r2"] >= 0.9
        store.close()

    def test_no_fd_leak_flat_series(self, tmp_path):
        """Flat FD series should not trigger FD-LEAK-01."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "stable_server",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=18.0,
            fds_slope=0.0,  # flat
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["FD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "FD-LEAK-01"]
        assert len(findings) == 0
        store.close()

    def test_fd_leak_short_observation_not_detected(self, tmp_path):
        """Leaking process with too short observation span → no finding."""
        store = make_db(tmp_path, hours=3.0)
        add_sys_baseline(store, hours=3.0)
        add_proc_series(
            store,
            "short_leaker",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=3.0,
            fds_slope=5.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["FD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "FD-LEAK-01"]
        assert len(findings) == 0
        store.close()

    def test_fd_leak_evidence_fields(self, tmp_path):
        """FD-LEAK-01 evidence must include key fields."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "leaky_app",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=18.0,
            fds_slope=10.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["FD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "FD-LEAK-01"]
        assert len(findings) >= 1
        ev = findings[0].evidence
        for key in (
            "proc_id",
            "process_name",
            "slope_fd_per_h",
            "r2",
            "n",
            "span_hours",
        ):
            assert key in ev, f"Missing evidence key: {key!r}"
        store.close()

    def test_fd_leak_low_slope_not_detected(self, tmp_path):
        """FD slope below threshold should not trigger FD-LEAK-01."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "slow_leaker",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=18.0,
            fds_slope=0.0,  # 0 slope — threshold is 0.5 FDs/h
        )
        store.conn.commit()

        from procdiag.diagnosis.leak import FdLeakRule

        # Temporarily raise the slope threshold to ensure no detection.
        original = FdLeakRule.slope_warning_fd_h
        FdLeakRule.slope_warning_fd_h = 100.0
        try:
            engine = Engine(_cfg(min_obs_hours=6.0))
            diag = engine.run(store, only=["FD-LEAK-01"])
            findings = [f for f in diag.findings if f.rule_id == "FD-LEAK-01"]
            assert len(findings) == 0
        finally:
            FdLeakRule.slope_warning_fd_h = original
        store.close()

    def test_fd_leak_proposal_five_sections(self, tmp_path):
        """FD-LEAK-01 Finding.proposal must contain all 5 section headers."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "fd_leaker",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=18.0,
            fds_slope=5.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["FD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "FD-LEAK-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section header: {header!r}"
        store.close()


# ---------------------------------------------------------------------------
# THREAD-LEAK-01
# ---------------------------------------------------------------------------


class TestThreadLeak:
    def test_thread_leak_detected(self, tmp_path):
        """Steep thread slope + long observation → THREAD-LEAK-01 finding."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "threaded_app",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=18.0,
            threads_slope=5.0,  # 5 threads/h — well above threshold of 0.5
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0), verbose=True)
        diag = engine.run(store, only=["THREAD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "THREAD-LEAK-01"]
        assert len(findings) >= 1
        f = findings[0]
        assert f.subject == "threaded_app"
        assert f.severity == Severity.WARNING
        assert f.evidence["slope_threads_per_h"] > 0
        assert f.evidence["r2"] >= 0.9
        store.close()

    def test_no_thread_leak_flat_series(self, tmp_path):
        """Flat thread count should not trigger THREAD-LEAK-01."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "stable_app",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=18.0,
            threads_slope=0.0,  # flat
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["THREAD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "THREAD-LEAK-01"]
        assert len(findings) == 0
        store.close()

    def test_thread_leak_short_observation_not_detected(self, tmp_path):
        """Leaking process with too short observation span → no finding."""
        store = make_db(tmp_path, hours=3.0)
        add_sys_baseline(store, hours=3.0)
        add_proc_series(
            store,
            "short_thread_leaker",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=3.0,
            threads_slope=5.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["THREAD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "THREAD-LEAK-01"]
        assert len(findings) == 0
        store.close()

    def test_thread_leak_evidence_fields(self, tmp_path):
        """THREAD-LEAK-01 evidence must include key fields."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "thread_leaker",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=18.0,
            threads_slope=10.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["THREAD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "THREAD-LEAK-01"]
        assert len(findings) >= 1
        ev = findings[0].evidence
        for key in (
            "proc_id",
            "process_name",
            "slope_threads_per_h",
            "r2",
            "n",
            "span_hours",
        ):
            assert key in ev, f"Missing evidence key: {key!r}"
        store.close()

    def test_thread_leak_proposal_five_sections(self, tmp_path):
        """THREAD-LEAK-01 Finding.proposal must contain all 5 section headers."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "thread_leaker",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,
            hours=18.0,
            threads_slope=5.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["THREAD-LEAK-01"])

        findings = [f for f in diag.findings if f.rule_id == "THREAD-LEAK-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section header: {header!r}"
        store.close()


# ---------------------------------------------------------------------------
# ZOMBIE-01
# ---------------------------------------------------------------------------


class TestZombie:
    def test_zombie_accumulation_detected(self, tmp_path):
        """Growing zombie approximation series → ZOMBIE-01 finding."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        # Insert zombie-approximate rows with positive slope.
        add_zombie_series(
            store,
            hours=18.0,
            zombie_slope_per_h=1.0,  # 1 new zombie per hour
            base_zombie_count=1,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0), verbose=True)
        diag = engine.run(store, only=["ZOMBIE-01"])

        findings = [f for f in diag.findings if f.rule_id == "ZOMBIE-01"]
        assert len(findings) >= 1
        f = findings[0]
        assert f.severity == Severity.INFO
        assert f.confidence == Confidence.LOW
        assert f.evidence["slope_per_h"] > 0
        store.close()

    def test_zombie_flat_not_detected(self, tmp_path):
        """Constant zombie count should not trigger ZOMBIE-01."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        # Flat zombie count (slope=0) should not fire.
        add_zombie_series(
            store,
            hours=18.0,
            zombie_slope_per_h=0.0,
            base_zombie_count=2,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["ZOMBIE-01"])

        findings = [f for f in diag.findings if f.rule_id == "ZOMBIE-01"]
        assert len(findings) == 0
        store.close()

    def test_zombie_no_proc_samples_not_detected(self, tmp_path):
        """No proc_samples data → no ZOMBIE-01 finding."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["ZOMBIE-01"])

        findings = [f for f in diag.findings if f.rule_id == "ZOMBIE-01"]
        assert len(findings) == 0
        store.close()

    def test_zombie_short_observation_not_detected(self, tmp_path):
        """Too short observation span → no ZOMBIE-01 finding."""
        store = make_db(tmp_path, hours=3.0)
        add_sys_baseline(store, hours=3.0)
        add_zombie_series(
            store,
            hours=3.0,
            zombie_slope_per_h=1.0,
            base_zombie_count=1,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["ZOMBIE-01"])

        findings = [f for f in diag.findings if f.rule_id == "ZOMBIE-01"]
        assert len(findings) == 0
        store.close()

    def test_zombie_confidence_is_low(self, tmp_path):
        """ZOMBIE-01 must always emit confidence=LOW."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_zombie_series(
            store,
            hours=18.0,
            zombie_slope_per_h=2.0,
            base_zombie_count=1,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["ZOMBIE-01"])

        findings = [f for f in diag.findings if f.rule_id == "ZOMBIE-01"]
        assert len(findings) >= 1
        assert findings[0].confidence == Confidence.LOW
        store.close()

    def test_zombie_proposal_five_sections(self, tmp_path):
        """ZOMBIE-01 Finding.proposal must contain all 5 section headers."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_zombie_series(
            store,
            hours=18.0,
            zombie_slope_per_h=1.0,
            base_zombie_count=1,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["ZOMBIE-01"])

        findings = [f for f in diag.findings if f.rule_id == "ZOMBIE-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section header: {header!r}"
        store.close()
