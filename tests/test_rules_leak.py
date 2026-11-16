"""Tests for the FD-LEAK-01, THREAD-LEAK-01, and ZOMBIE-01 rules.

Uses the synthetic DB builder from ``tests/synth.py`` and verifies:
- FD-LEAK-01: detects FD leak / does not detect flat series / short observation guard
- THREAD-LEAK-01: detects thread leak / does not detect flat series / short observation guard
- ZOMBIE-01: detects zombie accumulation / does not detect flat zombie count /
  short observation guard
- Proposal 5-section headers for all three rules
"""

from __future__ import annotations

import time

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

    @staticmethod
    def _approx_growth(tmp_path, name):
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_zombie_series(
            store, hours=18.0, zombie_slope_per_h=2.0, base_zombie_count=1
        )
        store.conn.execute(
            "UPDATE proc_registry SET name = ? WHERE name = '<defunct>'", (name,)
        )
        store.conn.commit()
        diag = Engine(_cfg(min_obs_hours=6.0)).run(store, only=["ZOMBIE-01"])
        store.close()
        assert diag.evaluated_rules == ["ZOMBIE-01"]
        return diag.findings

    def test_a_growing_pool_of_kernel_threads_is_not_zombie_accumulation(
        self, tmp_path
    ):
        assert self._approx_growth(tmp_path, "kworker/u8:3") == []

    def test_processes_with_a_program_name_still_count_as_zombies(self, tmp_path):
        (f,) = self._approx_growth(tmp_path, "uploader.sh")

        assert (f.confidence, f.evidence["method"]) == (Confidence.LOW, "approx")

    def test_the_approximation_note_says_kernel_threads_are_left_out(self, tmp_path):
        (f,) = self._approx_growth(tmp_path, "uploader.sh")

        assert "kworker/0:1" in f.proposal

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


# ---------------------------------------------------------------------------
# Sparse samples and recordings too short for a line
# ---------------------------------------------------------------------------

_LEAK_RULES = ["MEM-LEAK-01", "FD-LEAK-01", "THREAD-LEAK-01"]


def _leaking_program(tmp_path, hours, step_s, *, lead_s=0):
    """A program leaking in every metric, one rollup row each *step_s* seconds.

    Its first row is *lead_s* seconds into the recording.
    """
    store = make_db(tmp_path, hours=hours)
    start = time.time() // 60 * 60 - hours * 3600 - 120
    add_sys_baseline(store, hours=hours, start_ts=start)
    proc_id = store.proc_id_for(
        name="collector", cmdline_hash="h", cmdline_sample="/opt/collector", ts=start
    )
    store.conn.commit()
    rows = []
    for k in range(int((hours * 3600 - lead_s) / step_s)):
        h = (lead_s + k * step_s) / 3600
        rss = int(100 * _MB + 20 * _MB * h)
        rows.append(
            (
                start + (lead_s + k * step_s) // 60 * 60,
                proc_id,
                5.0,
                6.0,
                rss,
                rss,
                50 + int(300 * h),
                8 + int(120 * h),
            )
        )
    store.buffer("proc_rollup_1m", list(dict.fromkeys(rows)))
    store.flush(reason="synth")
    return store


class TestSparseSamples:
    @pytest.mark.parametrize("step_s", [90, 120, 300])
    def test_a_leak_is_judged_when_samples_are_taken_less_often_than_every_minute(
        self, tmp_path, step_s
    ):
        store = _leaking_program(tmp_path, 6.05, step_s, lead_s=240)

        diag = Engine(_cfg(min_obs_hours=6.0)).run(store, only=_LEAK_RULES)

        assert sorted(f.rule_id for f in diag.findings) == sorted(_LEAK_RULES)
        assert diag.skipped_rules == []
        store.close()

    def test_a_leak_found_on_sparse_samples_is_not_low_confidence(self, tmp_path):
        store = _leaking_program(tmp_path, 6.05, 300, lead_s=240)

        diag = Engine(_cfg(min_obs_hours=6.0)).run(store, only=["MEM-LEAK-01"])

        (f,) = diag.findings
        assert f.confidence == Confidence.MEDIUM
        store.close()

    def test_samples_every_minute_still_need_the_six_hours(self, tmp_path):
        store = _leaking_program(tmp_path, 5.5, 60, lead_s=240)

        diag = Engine(_cfg(min_obs_hours=6.0)).run(store, only=["MEM-LEAK-01"])

        assert diag.findings == []
        (skipped,) = diag.skipped_rules
        assert skipped.remaining_hours == pytest.approx(0.5, abs=0.05)
        store.close()


class TestRecordingShorterThanALine:
    @pytest.mark.parametrize("rule", _LEAK_RULES)
    def test_a_recording_too_short_to_fit_is_not_evaluated_not_healthy(
        self, tmp_path, rule
    ):
        store = _leaking_program(tmp_path, 0.1, 10)

        diag = Engine(_cfg(min_obs_hours=0.0)).run(store, only=[rule])

        assert diag.findings == []
        assert diag.evaluated_rules == []
        (skipped,) = diag.skipped_rules
        assert skipped.rule_id == rule
        assert skipped.remaining_hours == pytest.approx(0.1, abs=0.06)
        store.close()

    @pytest.mark.parametrize("rule", _LEAK_RULES)
    def test_twelve_points_a_minute_apart_are_enough_for_a_line(self, tmp_path, rule):
        store = _leaking_program(tmp_path, 0.3, 10)

        diag = Engine(_cfg(min_obs_hours=0.0)).run(store, only=[rule])

        assert [f.rule_id for f in diag.findings] == [rule]
        assert diag.skipped_rules == []
        store.close()

    @pytest.mark.parametrize(("hours", "waits"), [(0.19, True), (0.2, False)])
    def test_eleven_minutes_are_the_least_that_hold_twelve_points(
        self, tmp_path, hours, waits
    ):
        store = _leaking_program(tmp_path, hours, 10)

        diag = Engine(_cfg(min_obs_hours=0.0)).run(store, only=["MEM-LEAK-01"])

        assert bool(diag.skipped_rules) is waits
        assert bool(diag.findings) is not waits
        store.close()

    def test_a_long_recording_without_a_long_lived_process_has_nothing_to_wait_for(
        self, tmp_path
    ):
        store = make_db(tmp_path, hours=8.0)
        start = time.time() // 60 * 60 - 8 * 3600 - 120
        add_sys_baseline(store, hours=8.0, start_ts=start)
        proc_id = store.proc_id_for(
            name="job", cmdline_hash="h", cmdline_sample="/opt/job", ts=start
        )
        store.conn.commit()
        store.buffer(
            "proc_rollup_1m",
            [
                (start + 3600 * k + 60 * m, proc_id, 5.0, 6.0)
                + (100 * _MB, 100 * _MB, 50, 8)
                for k in range(8)
                for m in range(5)
            ],
        )
        store.flush(reason="synth")

        diag = Engine(_cfg(min_obs_hours=6.0)).run(store, only=["MEM-LEAK-01"])

        assert diag.skipped_rules == []
        assert diag.evaluated_rules == ["MEM-LEAK-01"]
        store.close()

    @staticmethod
    def _zombie_scans(tmp_path, hours):
        """One ``proc_counts`` row a minute, one more zombie each minute."""
        store = make_db(tmp_path, hours=hours)
        start = time.time() // 60 * 60 - hours * 3600 - 120
        add_sys_baseline(store, hours=hours, start_ts=start)
        proc_id = store.proc_id_for(
            name="sh", cmdline_hash="h", cmdline_sample="sh", ts=start
        )
        store.conn.commit()
        store.buffer(
            "proc_samples", [(start, proc_id, 100, 1.0, 10 * _MB, None, 1, 3, 0, 0)]
        )
        store.buffer(
            "proc_counts",
            [(start + 60 * k, 100, 1 + k) for k in range(int(hours * 60))],
        )
        store.flush(reason="synth")
        diag = Engine(_cfg(min_obs_hours=0.0)).run(store, only=["ZOMBIE-01"])
        store.close()
        return diag

    def test_zombie_counts_of_a_recording_too_short_for_a_line_are_not_healthy(
        self, tmp_path
    ):
        diag = self._zombie_scans(tmp_path, 0.15)

        assert diag.findings == []
        (skipped,) = diag.skipped_rules
        assert skipped.rule_id == "ZOMBIE-01"

    def test_zombie_counts_of_a_quarter_of_an_hour_are_enough_for_a_line(
        self, tmp_path
    ):
        diag = self._zombie_scans(tmp_path, 0.3)

        assert [f.rule_id for f in diag.findings] == ["ZOMBIE-01"]
