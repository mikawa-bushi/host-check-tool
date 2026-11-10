"""Integration tests for the diagnosis engine and the four priority rules.

Tests use the synthetic DB builder from ``tests/synth.py`` and verify:
- MEM-LEAK-01: detects leak / does not detect flat series
- DISK-FULL-01: correct severity at warning/critical day boundaries
- CPU-PERIODIC-01: detects daily and hourly patterns / not irregular
- CPU-SINGLECORE-01: detects and does not detect saturation episodes
- fold(): absorbs DISK-GROWTH-01 into DISK-FULL-01 (tested with fake Findings)
- observation time guard: healthy=True when data < min_observation_hours
- broken rule firewall: diagnosis completes even if a rule raises
- ObservationQuality: populated fields
- Finding.proposal: contains all 5 section headers
"""

from __future__ import annotations

import sys
import time

import pytest
from synth import (
    add_disk_series,
    add_proc_series,
    add_spike_events,
    add_sys_baseline,
    make_db,
)

from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Confidence, Finding, Rule, Severity
from procdiag.diagnosis import Engine
from procdiag.diagnosis.engine import fold
from procdiag.storage import TABLE_COLUMNS, Storage

_MB = 1024 * 1024
_GB = 1024**3
_HOUR = 3600.0
_DAY = 86400.0


# ---------------------------------------------------------------------------
# Config helpers
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
# MEM-LEAK-01
# ---------------------------------------------------------------------------


class TestMemLeak:
    def test_leak_detected(self, tmp_path):
        """Steep RSS slope + long observation → MEM-LEAK-01 finding."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "mysqld",
            rss_start=200 * _MB,
            rss_slope_per_h=5 * _MB,  # 5 MB/h — well above threshold
            hours=18.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0), verbose=True)
        diag = engine.run(store, only=["MEM-LEAK-01"])

        leak_findings = [f for f in diag.findings if f.rule_id == "MEM-LEAK-01"]
        assert len(leak_findings) >= 1
        f = leak_findings[0]
        assert f.subject == "mysqld"
        assert f.severity in (Severity.WARNING, Severity.CRITICAL)
        store.close()

    def test_no_leak_flat_series(self, tmp_path):
        """Flat RSS series should not trigger MEM-LEAK-01."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "nginx",
            rss_start=100 * _MB,
            rss_slope_per_h=0.0,  # flat
            hours=18.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["MEM-LEAK-01"])

        leak_findings = [f for f in diag.findings if f.rule_id == "MEM-LEAK-01"]
        assert len(leak_findings) == 0
        store.close()

    def test_critical_severity_for_large_slope(self, tmp_path):
        """Very large slope should escalate to CRITICAL."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "bigprocess",
            rss_start=100 * _MB,
            rss_slope_per_h=100 * _MB,  # 100 MB/h — critical threshold
            hours=18.0,
        )
        store.conn.commit()

        from procdiag.diagnosis.memory import MemLeakRule

        # Lower the critical threshold for the test to guarantee escalation.
        original = MemLeakRule.slope_critical_mb_h
        MemLeakRule.slope_critical_mb_h = 50.0
        try:
            engine = Engine(_cfg(min_obs_hours=6.0))
            diag = engine.run(store, only=["MEM-LEAK-01"])
            leak_findings = [f for f in diag.findings if f.rule_id == "MEM-LEAK-01"]
            assert any(f.severity == Severity.CRITICAL for f in leak_findings)
        finally:
            MemLeakRule.slope_critical_mb_h = original
        store.close()

    def test_short_observation_not_detected(self, tmp_path):
        """Leaking process with too short observation span → no finding."""
        store = make_db(tmp_path, hours=3.0)
        add_sys_baseline(store, hours=3.0)
        add_proc_series(
            store,
            "leaker",
            rss_start=100 * _MB,
            rss_slope_per_h=5 * _MB,
            hours=3.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["MEM-LEAK-01"])
        # Either the observation guard fires (healthy=True, no findings)
        # or fit_linear rejects the short span.
        leak_findings = [f for f in diag.findings if f.rule_id == "MEM-LEAK-01"]
        assert len(leak_findings) == 0
        store.close()


# ---------------------------------------------------------------------------
# DISK-FULL-01
# ---------------------------------------------------------------------------


class TestDiskFull:
    def _make_disk_store(self, tmp_path, days_left: float, days_history: float = 14.0):
        """Helper: create a DB with a disk series that has *days_left* days remaining."""
        capacity = int(100 * _GB)
        daily_growth = int(capacity / (days_left + days_history))
        used_start = int(capacity * 0.1)

        store = make_db(tmp_path, hours=days_history * 24)
        add_sys_baseline(store, hours=days_history * 24)
        add_disk_series(
            store,
            "/var",
            total=capacity,
            used_start=used_start,
            daily_growth=daily_growth,
            days=days_history,
        )
        store.conn.commit()
        return store

    def test_warning_at_20_days(self, tmp_path):
        """~20 days remaining should produce a WARNING."""
        store = self._make_disk_store(tmp_path, days_left=20.0)
        engine = Engine(_cfg())
        diag = engine.run(store, only=["DISK-FULL-01"])
        disk_findings = [f for f in diag.findings if f.rule_id == "DISK-FULL-01"]
        assert len(disk_findings) >= 1
        assert disk_findings[0].severity == Severity.WARNING
        store.close()

    def test_critical_at_5_days(self, tmp_path):
        """~5 days remaining should produce a CRITICAL finding."""
        store = self._make_disk_store(tmp_path, days_left=5.0)
        engine = Engine(_cfg())
        diag = engine.run(store, only=["DISK-FULL-01"])
        disk_findings = [f for f in diag.findings if f.rule_id == "DISK-FULL-01"]
        assert len(disk_findings) >= 1
        assert disk_findings[0].severity == Severity.CRITICAL
        store.close()

    def test_no_finding_for_stable_disk(self, tmp_path):
        """A stable (non-growing) disk should produce no DISK-FULL-01 finding."""
        capacity = int(100 * _GB)
        store = make_db(tmp_path, hours=14 * 24)
        add_sys_baseline(store, hours=14 * 24)
        add_disk_series(
            store,
            "/data",
            total=capacity,
            used_start=int(50 * _GB),
            daily_growth=0,  # flat
            days=14.0,
        )
        store.conn.commit()

        engine = Engine(_cfg())
        diag = engine.run(store, only=["DISK-FULL-01"])
        disk_findings = [f for f in diag.findings if f.rule_id == "DISK-FULL-01"]
        assert len(disk_findings) == 0
        store.close()

    def test_evidence_contains_days_left(self, tmp_path):
        """DISK-FULL-01 evidence must include days_left and daily_growth_bytes."""
        store = self._make_disk_store(tmp_path, days_left=10.0)
        engine = Engine(_cfg())
        diag = engine.run(store, only=["DISK-FULL-01"])
        disk_findings = [f for f in diag.findings if f.rule_id == "DISK-FULL-01"]
        assert len(disk_findings) >= 1
        ev = disk_findings[0].evidence
        assert "days_left" in ev
        assert "daily_growth_bytes" in ev
        store.close()


# ---------------------------------------------------------------------------
# CPU-PERIODIC-01
# ---------------------------------------------------------------------------


def _daily_spike_times(n_days: int, hour: int = 14, minute: int = 0) -> list[float]:
    now = time.time()
    midnight = (now // _DAY) * _DAY - n_days * _DAY
    return [midnight + d * _DAY + hour * _HOUR + minute * 60 for d in range(n_days)]


def _hourly_spike_times(n_hours: int, minute: int = 30) -> list[float]:
    now = time.time()
    base = (now // _HOUR) * _HOUR - n_hours * _HOUR
    return [base + h * _HOUR + minute * 60 for h in range(n_hours)]


class TestCpuPeriodic:
    def test_daily_pattern_detected(self, tmp_path):
        """Daily spikes at 14:00 for 5 days → CPU-PERIODIC-01 finding."""
        store = make_db(tmp_path, hours=5 * 24)
        add_sys_baseline(store, hours=5 * 24)
        times = _daily_spike_times(5, hour=14)
        add_spike_events(store, "cpu", times)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=1.0))
        diag = engine.run(store, only=["CPU-PERIODIC-01"])
        findings = [f for f in diag.findings if f.rule_id == "CPU-PERIODIC-01"]
        assert len(findings) >= 1
        assert findings[0].evidence["period_kind"] == "daily"
        store.close()

    def test_hourly_pattern_detected(self, tmp_path):
        """Hourly spikes at :30 for 4 hours → CPU-PERIODIC-01 finding."""
        store = make_db(tmp_path, hours=6)
        add_sys_baseline(store, hours=6)
        times = _hourly_spike_times(4, minute=30)
        add_spike_events(store, "cpu", times)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=1.0))
        diag = engine.run(store, only=["CPU-PERIODIC-01"])
        findings = [f for f in diag.findings if f.rule_id == "CPU-PERIODIC-01"]
        assert len(findings) >= 1
        store.close()

    def test_irregular_not_detected(self, tmp_path):
        """Random spike times should not produce a CPU-PERIODIC-01 finding."""
        import random

        store = make_db(tmp_path, hours=5 * 24)
        add_sys_baseline(store, hours=5 * 24)
        rng = random.Random(42)
        now = time.time()
        times = [now - rng.uniform(0, 4 * _DAY) for _ in range(8)]
        add_spike_events(store, "cpu", times)
        store.conn.commit()

        from procdiag.diagnosis.cpu import CpuPeriodicRule

        original_tol = CpuPeriodicRule.tol_sec
        CpuPeriodicRule.tol_sec = 30.0  # tight tolerance to avoid accidental match
        try:
            engine = Engine(_cfg(min_obs_hours=1.0))
            diag = engine.run(store, only=["CPU-PERIODIC-01"])
            findings = [f for f in diag.findings if f.rule_id == "CPU-PERIODIC-01"]
            assert len(findings) == 0
        finally:
            CpuPeriodicRule.tol_sec = original_tol
        store.close()

    def test_no_spike_events(self, tmp_path):
        """No spike events → no CPU-PERIODIC-01 finding."""
        store = make_db(tmp_path, hours=5 * 24)
        add_sys_baseline(store, hours=5 * 24)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=1.0))
        diag = engine.run(store, only=["CPU-PERIODIC-01"])
        findings = [f for f in diag.findings if f.rule_id == "CPU-PERIODIC-01"]
        assert len(findings) == 0
        store.close()


# ---------------------------------------------------------------------------
# CPU-SINGLECORE-01
# ---------------------------------------------------------------------------


class TestCpuSinglecore:
    def _add_singlecore_episode(
        self,
        store: "Storage",
        *,
        start_ts: float,
        duration_minutes: int = 20,
        core_pct: float = 98.0,
        cpu_avg: float = 30.0,
    ) -> None:
        """Insert sys_rollup_1m rows simulating a singlecore saturation episode."""
        rows = []
        for i in range(duration_minutes):
            ts = start_ts + i * 60.0
            rows.append(
                (
                    ts,
                    cpu_avg,
                    cpu_avg + 5,
                    cpu_avg + 8,
                    core_pct,
                    int(2 * _GB),
                    int(2.1 * _GB),
                    0.0,
                    0.0,
                    0.0,
                    0,
                )
            )
        store.buffer("sys_rollup_1m", rows)
        store.flush(reason="synth")

    def test_singlecore_detected(self, tmp_path):
        """A 20-minute singlecore episode should produce a CPU-SINGLECORE-01 finding."""
        hours = 10.0
        store = make_db(tmp_path, hours=hours)
        add_sys_baseline(store, hours=hours)
        now = time.time()
        ep_start = now - 5 * _HOUR
        self._add_singlecore_episode(store, start_ts=ep_start, duration_minutes=20)
        add_proc_series(
            store,
            "singleproc",
            rss_start=200 * _MB,
            rss_slope_per_h=0.0,
            hours=hours,
            cpu_pattern=90.0,
        )
        store.conn.commit()

        from procdiag.diagnosis.cpu import CpuSinglecoreRule

        orig = CpuSinglecoreRule.min_consecutive_minutes
        CpuSinglecoreRule.min_consecutive_minutes = 15
        try:
            engine = Engine(_cfg(min_obs_hours=1.0))
            diag = engine.run(store, only=["CPU-SINGLECORE-01"])
            findings = [f for f in diag.findings if f.rule_id == "CPU-SINGLECORE-01"]
            assert len(findings) >= 1
        finally:
            CpuSinglecoreRule.min_consecutive_minutes = orig
        store.close()

    def test_no_singlecore_when_cpu_avg_high(self, tmp_path):
        """When cpu_avg is also high, it's general saturation, not singlecore — no finding."""
        hours = 10.0
        store = make_db(tmp_path, hours=hours)
        add_sys_baseline(store, hours=hours, cpu=80.0)
        store.conn.commit()
        # All sys_rollup rows have cpu_avg ≈ 80, which exceeds the 50% ceiling.
        engine = Engine(_cfg(min_obs_hours=1.0))
        diag = engine.run(store, only=["CPU-SINGLECORE-01"])
        findings = [f for f in diag.findings if f.rule_id == "CPU-SINGLECORE-01"]
        assert len(findings) == 0
        store.close()


# ---------------------------------------------------------------------------
# fold()
# ---------------------------------------------------------------------------


class TestFold:
    """Test the fold() function in isolation with fake Findings."""

    def _make_finding(
        self,
        rule_id: str,
        subject: str,
        severity: Severity = Severity.WARNING,
    ) -> Finding:
        return Finding(
            rule_id=rule_id,
            severity=severity,
            subject=subject,
            summary="test",
            evidence={},
            detected_at=(0.0, 1.0),
            confidence=Confidence.HIGH,
        )

    def test_growth_folded_into_full(self):
        """DISK-GROWTH-01 with same subject as DISK-FULL-01 should be absorbed."""
        full = self._make_finding("DISK-FULL-01", "/var")
        growth = self._make_finding("DISK-GROWTH-01", "/var")

        result = fold([full, growth])
        assert len(result) == 1
        assert result[0].rule_id == "DISK-FULL-01"
        assert "DISK-GROWTH-01" in result[0].related

    def test_growth_kept_when_no_matching_full(self):
        """DISK-GROWTH-01 with no matching DISK-FULL-01 should remain in list."""
        growth = self._make_finding("DISK-GROWTH-01", "/data")
        result = fold([growth])
        assert len(result) == 1
        assert result[0].rule_id == "DISK-GROWTH-01"

    def test_different_subjects_not_folded(self):
        """DISK-GROWTH-01 on /data should not be folded into DISK-FULL-01 on /var."""
        full = self._make_finding("DISK-FULL-01", "/var")
        growth = self._make_finding("DISK-GROWTH-01", "/data")
        result = fold([full, growth])
        assert len(result) == 2

    def test_unrelated_findings_unchanged(self):
        """Findings with no fold relationship should pass through unchanged."""
        f1 = self._make_finding("MEM-LEAK-01", "mysqld")
        f2 = self._make_finding("CPU-PERIODIC-01", "cpu_spike/daily")
        result = fold([f1, f2])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Observation time guard
# ---------------------------------------------------------------------------

_LEAK_FITS = ("MEM-LEAK-01", "FD-LEAK-01", "THREAD-LEAK-01")


def _row(table: str, **values: object) -> tuple:
    return tuple(values.get(column, 0) for column in TABLE_COLUMNS[table])


def _record(store: Storage, monkeypatch, *, start: float, seconds: float) -> None:
    """Sample every 10 s from *start* for *seconds* a process whose RSS, threads
    and FDs grow, then roll the samples up as the recorder does when it stops."""
    leaky = store.proc_id_for("leaky", "hash_leaky", "/usr/bin/leaky", start)
    for k in range(int(seconds // 10) + 1):
        t = start + 10 * k
        h = 10 * k / _HOUR
        store.buffer(
            "sys_samples",
            [_row("sys_samples", ts=t, mem_used=2 * _GB, mem_available=6 * _GB)],
        )
        store.buffer(
            "proc_samples",
            [
                _row(
                    "proc_samples",
                    ts=t,
                    proc_id=leaky,
                    pid=200,
                    rss=int(200 * _MB + 50 * _MB * h),
                    pss=None,
                    num_threads=8 + int(10 * h),
                    num_fds=40 + int(20 * h),
                )
            ],
        )
    store.flush(reason="test")
    with monkeypatch.context() as m:
        m.setattr("procdiag.storage.time.time", lambda: start + seconds + 0.3)
        store.rollup_incremental()


class TestObservationGuard:
    def test_healthy_when_insufficient_data(self, tmp_path):
        """Less than min_observation_hours of data → healthy=True, no findings."""
        store = make_db(tmp_path, hours=2.0)
        add_sys_baseline(store, hours=2.0)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store)
        assert diag.healthy is True
        assert len(diag.findings) == 0
        # The reasoning should mention the shortfall.
        assert any("観測時間不足" in r for r in diag.reasoning)
        store.close()

    def test_rules_run_when_sufficient_data(self, tmp_path):
        """With 8 hours of data and min_obs=6, config-default rules run."""
        store = make_db(tmp_path, hours=8.0)
        add_sys_baseline(store, hours=8.0)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store)
        # Rules following the config default (6h) must not be skipped...
        assert not any("MEM-LEAK-01: 観測時間不足" in r for r in diag.reasoning)
        # ...while a rule with a longer explicit minimum still is.
        assert any("MEM-PRESSURE-01: 観測時間不足" in r for r in diag.reasoning)
        store.close()

    @pytest.mark.parametrize("short_by", [0, 90], ids=["exact", "90s-short"])
    @pytest.mark.parametrize(
        ("rule_id", "growth"),
        [
            ("MEM-LEAK-01", {"rss_slope_per_h": 50 * _MB}),
            ("FD-LEAK-01", {"rss_slope_per_h": 0.0, "fds_slope": 5.0}),
            ("THREAD-LEAK-01", {"rss_slope_per_h": 0.0, "threads_slope": 5.0}),
        ],
    )
    def test_a_recording_long_enough_to_evaluate_reports_the_leak(
        self, tmp_path, rule_id, growth, short_by
    ):
        start = time.time() - 7 * _HOUR
        duration = 6 * _HOUR - short_by
        store = make_db(tmp_path, hours=6.0)
        add_sys_baseline(store, hours=duration / _HOUR, start_ts=start)
        add_sys_baseline(store, hours=1 / 60, start_ts=start + duration)
        # The recorder never rolls up the minute in progress at stop, so the
        # process rows end with the last whole minute before it while the
        # system rows run until the stop.
        add_proc_series(
            store,
            "leaky",
            rss_start=200 * _MB,
            **growth,
            hours=duration / _HOUR,
            start_ts=start,
        )

        diag = Engine(_cfg(min_obs_hours=6.0)).run(store, only=[rule_id])
        store.close()

        assert rule_id in diag.evaluated_rules
        found = [f for f in diag.findings if f.rule_id == rule_id]
        assert [f.subject for f in found] == ["leaky"]
        assert found[0].confidence == Confidence.MEDIUM

    def test_a_disk_series_a_hair_under_6h_keeps_medium_confidence(self, tmp_path):
        start = time.time() - 7 * _HOUR
        store = make_db(tmp_path, hours=6.0)
        add_sys_baseline(store, hours=6.0 + 1 / 60, start_ts=start)
        # Real disk sample times drift by milliseconds: the disk rows of a
        # 12-minute recording spanned 12 minutes less 14 ms.
        span = 6 * _HOUR - 0.014
        used_per_minute = 10 * _GB // (24 * 60)
        rows = [
            (
                start + span * i / 360,
                "/var",
                100 * _GB,
                90 * _GB + used_per_minute * i,
                None,
                None,
                None,
            )
            for i in range(361)
        ]
        store.buffer("disk_samples", rows)
        store.flush(reason="test")

        diag = Engine(_cfg()).run(store, only=["DISK-FULL-01"])
        store.close()

        found = [f for f in diag.findings if f.rule_id == "DISK-FULL-01"]
        assert [f.subject for f in found] == ["/var"]
        assert found[0].confidence == Confidence.MEDIUM

    @pytest.mark.parametrize(
        ("hours", "short_by", "overrides"),
        [
            (6.0, 90, {}),
            (3.0, 90, {rule: {"min_observation_hours": 3.0} for rule in _LEAK_FITS}),
            (
                2.99,
                110,
                {rule: {"min_observation_hours": 2.99} for rule in _LEAK_FITS},
            ),
        ],
        ids=["default-6h", "overridden-3h", "overridden-2.99h"],
    )
    def test_a_recording_under_two_minutes_short_reports_the_leaks(
        self, tmp_path, monkeypatch, hours, short_by, overrides
    ):
        # Starting 10 s into a minute leaves the rules rollup rows spanning
        # several minutes less than required: four for a whole-minute
        # requirement, 4.4 for 2.99 h stopped 110 s short.  These are the
        # shortest series a recording long enough to be evaluated can give.
        start = 60 * (int(time.time()) // 60) - 8 * _HOUR + 10
        store = make_db(tmp_path, hours=hours)
        _record(store, monkeypatch, start=start, seconds=hours * _HOUR - short_by)

        config = ProcdiagConfig(rules=RulesConfig(overrides=overrides))
        diag = Engine(config).run(store, only=list(_LEAK_FITS))
        store.close()

        assert {(f.rule_id, f.subject, f.confidence) for f in diag.findings} == {
            (rule, "leaky", Confidence.MEDIUM) for rule in _LEAK_FITS
        }

    def test_instant_rule_runs_on_short_window(self, tmp_path):
        """min_observation_hours=0.0 rules fire even on short recordings.

        Design §6.3: the minimum-observation guard exists for the regression
        rules; instantaneous checks like SWAP-THRASH-01 must not be muted by
        config.rules.min_observation_hours.
        """
        from synth import add_swap_thrash_samples

        store = make_db(tmp_path, hours=1.0)
        add_swap_thrash_samples(store, baseline_hours=0.5)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store)
        assert any(f.rule_id == "SWAP-THRASH-01" for f in diag.findings)
        store.close()


# ---------------------------------------------------------------------------
# Broken rule firewall
# ---------------------------------------------------------------------------


class TestBrokenRuleFirewall:
    def test_broken_rule_does_not_abort_diagnosis(self, tmp_path):
        """A rule that raises in evaluate() should not crash the engine."""
        store = make_db(tmp_path, hours=8.0)
        add_sys_baseline(store, hours=8.0)
        store.conn.commit()

        # Temporarily register a rule that always raises.
        class BrokenRule(Rule):
            rule_id = "TEST-BROKEN-99"
            category = "test"

            def evaluate(self, store, window):
                raise RuntimeError("模擬エラー — テスト用")

        # Manually inject without @register decorator to avoid polluting registry.
        from procdiag.core.rule import _REGISTRY

        _REGISTRY["TEST-BROKEN-99"] = BrokenRule
        try:
            engine = Engine(_cfg(min_obs_hours=1.0))
            diag = engine.run(store)
            # Engine should complete and the error should appear in reasoning.
            assert any("TEST-BROKEN-99" in r for r in diag.reasoning)
        finally:
            _REGISTRY.pop("TEST-BROKEN-99", None)
        store.close()


# ---------------------------------------------------------------------------
# ObservationQuality
# ---------------------------------------------------------------------------


class TestObservationQuality:
    def test_quality_populated(self, tmp_path):
        """ObservationQuality should have non-default span_hours after data insertion."""
        store = make_db(tmp_path, hours=10.0)
        add_sys_baseline(store, hours=10.0)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store)
        assert diag.quality.span_hours > 0.0
        store.close()

    def test_quality_self_cpu(self, tmp_path):
        """ObservationQuality.self_cpu_avg should be computed from sys_samples."""
        store = make_db(tmp_path, hours=8.0)
        add_sys_baseline(store, hours=8.0)
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store)
        # self_cpu was set to 0.1 in add_sys_baseline.
        assert diag.quality.self_cpu_avg >= 0.0
        store.close()


# ---------------------------------------------------------------------------
# Proposal 5-section headers
# ---------------------------------------------------------------------------


class TestProposal:
    def test_memleak_proposal_five_sections(self, tmp_path):
        """MEM-LEAK-01 Finding.proposal must contain all 5 section headers."""
        store = make_db(tmp_path, hours=18.0)
        add_sys_baseline(store, hours=18.0)
        add_proc_series(
            store,
            "leaker",
            rss_start=100 * _MB,
            rss_slope_per_h=5 * _MB,
            hours=18.0,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=6.0))
        diag = engine.run(store, only=["MEM-LEAK-01"])
        findings = [f for f in diag.findings if f.rule_id == "MEM-LEAK-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal

        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section header: {header!r}"
        store.close()

    def test_diskfull_proposal_five_sections(self, tmp_path):
        """DISK-FULL-01 Finding.proposal must contain all 5 section headers."""
        capacity = int(100 * _GB)
        store = make_db(tmp_path, hours=14 * 24)
        add_sys_baseline(store, hours=14 * 24)
        add_disk_series(
            store,
            "/var",
            total=capacity,
            used_start=int(10 * _GB),
            daily_growth=int(5 * _GB),
            days=14.0,
        )
        store.conn.commit()

        engine = Engine(_cfg())
        diag = engine.run(store, only=["DISK-FULL-01"])
        findings = [f for f in diag.findings if f.rule_id == "DISK-FULL-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal

        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section header: {header!r}"
        store.close()


# ---------------------------------------------------------------------------
# extra_rules wiring (issue #1 §1-2)
# ---------------------------------------------------------------------------


class TestExtraRules:
    """``config.rules.extra_rules`` import paths must actually be loaded."""

    def test_extra_rule_module_is_loaded_and_runs(self, tmp_path, monkeypatch):
        mod = tmp_path / "procdiag_extra_rule_under_test.py"
        mod.write_text(
            "from procdiag.core import (\n"
            "    Confidence, Finding, Rule, Severity, register,\n"
            ")\n"
            "\n"
            "@register\n"
            "class _ExtraRuleUnderTest(Rule):\n"
            "    rule_id = 'TEST-EXTRA-01'\n"
            "    category = 'test'\n"
            "    title = 'extra rule under test'\n"
            "    min_observation_hours = 0.0\n"
            "    def evaluate(self, store, window):\n"
            "        return [Finding(\n"
            "            rule_id=self.rule_id, severity=Severity.INFO,\n"
            "            subject='x', summary='extra rule ran', evidence={},\n"
            "            detected_at=(window.start_ts, window.end_ts),\n"
            "            confidence=Confidence.LOW,\n"
            "        )]\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        from procdiag.core.rule import _REGISTRY

        try:
            store = make_db(tmp_path)
            cfg = ProcdiagConfig(
                rules=RulesConfig(
                    extra_rules=["procdiag_extra_rule_under_test"],
                    min_observation_hours=0.0,
                )
            )
            engine = Engine(cfg, verbose=True)
            diag = engine.run(store, only=["TEST-EXTRA-01"])
            assert any(f.rule_id == "TEST-EXTRA-01" for f in diag.findings)
            store.close()
        finally:
            # Keep the global registry AND the module cache clean so re-running
            # this test in the same process re-imports and re-registers the rule.
            _REGISTRY.pop("TEST-EXTRA-01", None)
            monkeypatch.delitem(
                sys.modules, "procdiag_extra_rule_under_test", raising=False
            )

    def test_invalid_extra_rule_path_does_not_crash(self, tmp_path):
        store = make_db(tmp_path)
        cfg = ProcdiagConfig(
            rules=RulesConfig(
                extra_rules=["nonexistent_pkg.totally.missing"],
                min_observation_hours=0.0,
            )
        )
        engine = Engine(cfg, verbose=True)
        diag = engine.run(store)  # must not raise
        assert any("nonexistent_pkg.totally.missing" in r for r in diag.reasoning)
        store.close()


# ---------------------------------------------------------------------------
# gap_seconds aggregation (issue #1 §4-2)
# ---------------------------------------------------------------------------


class TestQualitySelfCpuAggregate:
    """_build_quality computes self_cpu avg/max via SQL aggregation."""

    def test_self_cpu_avg_and_max_from_sql(self, tmp_path):
        from procdiag.storage import TABLE_COLUMNS

        cols = TABLE_COLUMNS["sys_samples"]

        def _row(ts: float, self_cpu: float) -> tuple:
            d: dict = {c: 0 for c in cols}
            d["ts"] = ts
            d["mem_used"] = 1_000_000
            d["mem_available"] = 1_000_000
            d["self_cpu"] = self_cpu
            return tuple(d[c] for c in cols)

        store = make_db(tmp_path)
        store.buffer(
            "sys_samples",
            [_row(1000.0, 0.0), _row(1060.0, 2.0), _row(1120.0, 4.0)],
        )
        store.flush()

        diag = Engine(_cfg(min_obs_hours=0.0)).run(store)
        # avg = (0 + 2 + 4) / 3 = 2.0 ; max = 4.0
        assert diag.quality.self_cpu_avg == pytest.approx(2.0)
        assert diag.quality.self_cpu_max == pytest.approx(4.0)
        store.close()

    def test_self_cpu_defaults_to_zero_when_no_samples(self, tmp_path):
        # AVG/MAX over zero rows return NULL → must fall back to 0.0.
        store = make_db(tmp_path)  # empty: no sys_samples
        diag = Engine(_cfg(min_obs_hours=0.0)).run(store)
        assert diag.quality.self_cpu_avg == 0.0
        assert diag.quality.self_cpu_max == 0.0
        store.close()


class TestGapSecondsAggregation:
    """ObservationQuality.gap_seconds must be the sum of delay seconds."""

    def test_gap_seconds_sums_delays_not_event_count(self, tmp_path):
        store = make_db(tmp_path)
        # recorder._on_gap writes detail as "<delay>s ジョブ<name>".
        store.record_event("gap", "12.5s ジョブt2b_process")
        store.record_event("gap", "7.0s ジョブt2a_cgroup")
        store.record_event("gap", "30.0s ジョブt1_system")

        engine = Engine(_cfg(min_obs_hours=0.0))
        diag = engine.run(store)

        # If the code counted events instead of summing seconds this would be
        # 3.0.  Correct behaviour: 12.5 + 7.0 + 30.0 = 49.5 seconds.
        assert diag.quality.gap_seconds == pytest.approx(49.5)
        store.close()


# ---------------------------------------------------------------------------
# no_data signal (empty DB must not look healthy) — issue v0.3 §1-1 / A-01
# ---------------------------------------------------------------------------


class TestNoData:
    """An empty DB must set diag.no_data=True, not a false『healthy』."""

    def test_no_data_flag_when_db_empty(self, tmp_path):
        store = make_db(tmp_path)  # schema only, zero samples
        diag = Engine(_cfg(min_obs_hours=0.0)).run(store)
        assert diag.no_data is True
        assert diag.findings == []
        store.close()

    def test_no_data_false_when_samples_present(self, tmp_path):
        store = make_db(tmp_path, hours=8.0)
        add_sys_baseline(store, hours=8.0)
        store.conn.commit()
        diag = Engine(_cfg(min_obs_hours=6.0)).run(store)
        assert diag.no_data is False
        store.close()

    def test_no_data_false_when_only_rollup_present(self, tmp_path):
        """sys_samples empty but sys_rollup_1m present → still counts as data."""
        store = make_db(tmp_path)
        minute = int(time.time() / 60) * 60.0
        store.buffer(
            "sys_rollup_1m",
            [(minute, 20.0, 30.0, 25.0, 40.0, 1_000_000, 1_100_000, 0.0, 0.0, 0.0, 0)],
        )
        store.flush()
        diag = Engine(_cfg(min_obs_hours=0.0)).run(store)
        assert diag.no_data is False
        store.close()

    def test_short_recording_is_not_no_data(self, tmp_path):
        """Data exists but span < min_observation_hours → healthy, NOT no_data.

        no_data is reserved for a genuinely empty DB; a short recording still
        has observations and must keep no_data=False.
        """
        store = make_db(tmp_path, hours=2.0)
        add_sys_baseline(store, hours=2.0)
        store.conn.commit()
        diag = Engine(_cfg(min_obs_hours=6.0)).run(store)
        assert diag.no_data is False
        assert diag.healthy is True
        store.close()


# ---------------------------------------------------------------------------
# Rule coverage — a short recording must not read as "all clear"
# ---------------------------------------------------------------------------


class TestRuleCoverage:
    def _short_diag(self, tmp_path):
        store = make_db(tmp_path, hours=1.0)
        add_sys_baseline(store, hours=1.0)
        diag = Engine(_cfg(min_obs_hours=6.0)).run(store)
        store.close()
        return diag

    def test_short_recording_lists_skipped_rules(self, tmp_path):
        diag = self._short_diag(tmp_path)
        skipped = {r.rule_id: r for r in diag.skipped_rules}
        assert "MEM-LEAK-01" in skipped and "MEM-PRESSURE-01" in skipped
        assert skipped["MEM-PRESSURE-01"].required_hours == 24.0
        assert 22.0 < skipped["MEM-PRESSURE-01"].remaining_hours <= 24.0
        # Instantaneous rules still ran.
        assert "SWAP-THRASH-01" in diag.evaluated_rules
        assert not diag.fully_evaluated
        assert diag.healthy  # coverage is reported separately from health

    def test_crashing_rule_is_counted_as_errored(self, tmp_path, monkeypatch):
        from procdiag.diagnosis.memory import SwapThrashRule

        def _boom(self, store, window):
            raise RuntimeError("boom")

        monkeypatch.setattr(SwapThrashRule, "evaluate", _boom)
        diag = self._short_diag(tmp_path)
        assert diag.errored_rules == ["SWAP-THRASH-01"]
        assert "SWAP-THRASH-01" not in diag.evaluated_rules

    def test_outputs_state_the_partial_coverage(self, tmp_path):
        import json

        from procdiag.report import render_html, render_json, render_terminal

        diag = self._short_diag(tmp_path)
        text = render_terminal(diag, color=False)
        assert "評価できた範囲では問題は見つかりませんでした" in text
        assert "件のみ評価" in text and "未評価: MEM-PRESSURE-01" in text

        data = json.loads(render_json(diag))
        assert {r["rule_id"] for r in data["skipped_rules"]} >= {"MEM-LEAK-01"}
        assert "SWAP-THRASH-01" in data["evaluated_rules"]

        html = render_html(diag, None)
        assert "問題なし (一部未評価)" in html and "未評価ルール" in html
