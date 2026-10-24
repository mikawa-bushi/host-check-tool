"""Tests for IO-BOTTLENECK-01, DISK-GROWTH-01, and DISK-JOURNAL-01 rules.

Covers:
- IO-BOTTLENECK-01: detect / not-detect, three-tier I/O demotion
  (proc I/O → cgroup fallback → silence)
- DISK-GROWTH-01: detect / not-detect, INFO vs WARNING severity
- DISK-JOURNAL-01: detect / not-detect, path prefix matching
- DISK-GROWTH-01 + DISK-FULL-01 coexistence → engine fold (absorb into parent)
"""

from __future__ import annotations

import time

from synth import (
    add_cg_io_series,
    add_dir_series,
    add_disk_series,
    add_proc_io_series,
    add_psi_io_series,
    add_sys_baseline,
    make_db,
)

from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Confidence, Severity
from procdiag.diagnosis import Engine

_MB = 1024 * 1024
_GB = 1024**3
_HOUR = 3600.0
_DAY = 86400.0


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _cfg(
    min_obs_hours: float = 0.5,
    disabled: list[str] | None = None,
    journal_size_warn_gb: float = 4.0,
) -> ProcdiagConfig:
    return ProcdiagConfig(
        rules=RulesConfig(
            min_observation_hours=min_obs_hours,
            disabled=disabled or [],
            journal_size_warn_gb=journal_size_warn_gb,
        )
    )


# ---------------------------------------------------------------------------
# IO-BOTTLENECK-01
# ---------------------------------------------------------------------------


class TestIoBottleneck:
    """Tests for IO-BOTTLENECK-01."""

    def test_detected_with_proc_io(self, tmp_path):
        """Sustained PSI I/O > threshold for 15+ minutes with proc I/O data."""
        hours = 2.0
        store = make_db(tmp_path, hours=hours)
        # Insert sys_samples with high psi_io_some10 (30 min = 1800 rows at 1/sec).
        add_psi_io_series(store, hours=hours, psi_io_some10=50.0)
        add_proc_io_series(
            store,
            "mysqld",
            hours=hours,
            io_write_per_min=10 * _MB,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import IoBottleneckRule

        orig_min = IoBottleneckRule.min_duration_minutes
        IoBottleneckRule.min_duration_minutes = 15.0
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["IO-BOTTLENECK-01"])
        finally:
            IoBottleneckRule.min_duration_minutes = orig_min

        findings = [f for f in diag.findings if f.rule_id == "IO-BOTTLENECK-01"]
        assert len(findings) >= 1
        f = findings[0]
        assert f.severity == Severity.WARNING
        assert f.confidence == Confidence.HIGH
        assert f.evidence["top_writers"], "top_writers should not be empty"
        assert f.evidence["top_writers"][0]["name"] == "mysqld"
        store.close()

    def test_not_detected_when_psi_below_threshold(self, tmp_path):
        """PSI I/O well below threshold → no IO-BOTTLENECK-01 finding."""
        hours = 2.0
        store = make_db(tmp_path, hours=hours)
        # psi_io_some10 = 5.0, well below default threshold of 30.
        add_psi_io_series(store, hours=hours, psi_io_some10=5.0)
        add_proc_io_series(
            store,
            "lighthttpd",
            hours=hours,
            io_write_per_min=1 * _MB,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=0.5))
        diag = engine.run(store, only=["IO-BOTTLENECK-01"])
        findings = [f for f in diag.findings if f.rule_id == "IO-BOTTLENECK-01"]
        assert len(findings) == 0
        store.close()

    def test_not_detected_when_duration_too_short(self, tmp_path):
        """Only 5 minutes of high PSI → does not meet 15-minute threshold."""
        hours = 1.0
        store = make_db(tmp_path, hours=hours)
        # Use baseline (psi_io=0) for most of the time.
        add_sys_baseline(store, hours=hours)
        # Inject 5 minutes of high PSI (5 rows * 60 seconds = 300 rows).
        now = time.time()
        spike_start = now - 10 * 60.0  # 10 minutes ago
        add_psi_io_series(
            store,
            hours=5 / 60.0,
            psi_io_some10=50.0,
            start_ts=spike_start,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import IoBottleneckRule

        orig_min = IoBottleneckRule.min_duration_minutes
        IoBottleneckRule.min_duration_minutes = 15.0
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["IO-BOTTLENECK-01"])
        finally:
            IoBottleneckRule.min_duration_minutes = orig_min

        findings = [f for f in diag.findings if f.rule_id == "IO-BOTTLENECK-01"]
        assert len(findings) == 0
        store.close()

    def test_cgroup_fallback_when_proc_io_null(self, tmp_path):
        """When proc I/O is all NULL, rule falls back to cg_samples (confidence=LOW)."""
        hours = 2.0
        store = make_db(tmp_path, hours=hours)
        add_psi_io_series(store, hours=hours, psi_io_some10=50.0)
        # No proc_samples with I/O data → no add_proc_io_series call.
        # Insert cg_samples with I/O data as fallback.
        add_cg_io_series(
            store,
            "system.slice/heavy.service",
            hours=hours,
            io_wbytes_per_min=20 * _MB,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import IoBottleneckRule

        orig_min = IoBottleneckRule.min_duration_minutes
        IoBottleneckRule.min_duration_minutes = 15.0
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["IO-BOTTLENECK-01"])
        finally:
            IoBottleneckRule.min_duration_minutes = orig_min

        findings = [f for f in diag.findings if f.rule_id == "IO-BOTTLENECK-01"]
        assert len(findings) >= 1
        f = findings[0]
        assert f.confidence == Confidence.LOW
        assert f.evidence["top_cg_writers"], "top_cg_writers should not be empty"
        store.close()

    def test_silence_when_no_io_data_at_all(self, tmp_path):
        """High PSI but no proc I/O and no cgroup I/O → rule is silent."""
        hours = 2.0
        store = make_db(tmp_path, hours=hours)
        add_psi_io_series(store, hours=hours, psi_io_some10=50.0)
        # No proc_samples or cg_samples inserted.
        store.conn.commit()

        from procdiag.diagnosis.io_disk import IoBottleneckRule

        orig_min = IoBottleneckRule.min_duration_minutes
        IoBottleneckRule.min_duration_minutes = 15.0
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["IO-BOTTLENECK-01"])
        finally:
            IoBottleneckRule.min_duration_minutes = orig_min

        findings = [f for f in diag.findings if f.rule_id == "IO-BOTTLENECK-01"]
        assert len(findings) == 0
        store.close()

    def test_proposal_five_sections(self, tmp_path):
        """IO-BOTTLENECK-01 proposal must contain all 5 section headers."""
        hours = 2.0
        store = make_db(tmp_path, hours=hours)
        add_psi_io_series(store, hours=hours, psi_io_some10=50.0)
        add_proc_io_series(
            store,
            "pgwriter",
            hours=hours,
            io_write_per_min=5 * _MB,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import IoBottleneckRule

        orig_min = IoBottleneckRule.min_duration_minutes
        IoBottleneckRule.min_duration_minutes = 15.0
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["IO-BOTTLENECK-01"])
        finally:
            IoBottleneckRule.min_duration_minutes = orig_min

        findings = [f for f in diag.findings if f.rule_id == "IO-BOTTLENECK-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section: {header!r}"
        store.close()


# ---------------------------------------------------------------------------
# DISK-GROWTH-01
# ---------------------------------------------------------------------------


class TestDiskGrowth:
    """Tests for DISK-GROWTH-01."""

    def test_detected_info_at_threshold(self, tmp_path):
        """Growth just above threshold should produce INFO finding."""
        days = 7.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        # 150 MB/day — above 100 MB/day threshold, below 200 MB/day WARNING threshold.
        add_dir_series(
            store,
            "/var/log",
            start=int(1 * _GB),
            daily_growth=150 * _MB,
            days=days,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import DiskGrowthRule

        orig = DiskGrowthRule.threshold_bytes_per_day
        DiskGrowthRule.threshold_bytes_per_day = 100 * _MB
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["DISK-GROWTH-01"])
        finally:
            DiskGrowthRule.threshold_bytes_per_day = orig

        findings = [f for f in diag.findings if f.rule_id == "DISK-GROWTH-01"]
        assert len(findings) >= 1
        f = findings[0]
        assert f.severity == Severity.INFO
        assert f.evidence["path"] == "/var/log"
        store.close()

    def test_detected_warning_above_double_threshold(self, tmp_path):
        """Growth > 2x threshold should escalate to WARNING."""
        days = 7.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        # 250 MB/day > 2 * 100 MB/day.
        add_dir_series(
            store,
            "/opt/data",
            start=int(5 * _GB),
            daily_growth=250 * _MB,
            days=days,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import DiskGrowthRule

        orig = DiskGrowthRule.threshold_bytes_per_day
        DiskGrowthRule.threshold_bytes_per_day = 100 * _MB
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["DISK-GROWTH-01"])
        finally:
            DiskGrowthRule.threshold_bytes_per_day = orig

        findings = [f for f in diag.findings if f.rule_id == "DISK-GROWTH-01"]
        assert len(findings) >= 1
        assert findings[0].severity == Severity.WARNING
        store.close()

    def test_not_detected_when_growth_below_threshold(self, tmp_path):
        """Growth below threshold → no DISK-GROWTH-01 finding."""
        days = 7.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        # 50 MB/day < 100 MB/day threshold.
        add_dir_series(
            store,
            "/home",
            start=int(2 * _GB),
            daily_growth=50 * _MB,
            days=days,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import DiskGrowthRule

        orig = DiskGrowthRule.threshold_bytes_per_day
        DiskGrowthRule.threshold_bytes_per_day = 100 * _MB
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["DISK-GROWTH-01"])
        finally:
            DiskGrowthRule.threshold_bytes_per_day = orig

        findings = [f for f in diag.findings if f.rule_id == "DISK-GROWTH-01"]
        assert len(findings) == 0
        store.close()

    def test_not_detected_when_flat(self, tmp_path):
        """A flat (zero-growth) directory → no DISK-GROWTH-01 finding."""
        days = 7.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        add_dir_series(
            store,
            "/srv",
            start=int(10 * _GB),
            daily_growth=0,
            days=days,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=0.5))
        diag = engine.run(store, only=["DISK-GROWTH-01"])
        findings = [f for f in diag.findings if f.rule_id == "DISK-GROWTH-01"]
        assert len(findings) == 0
        store.close()

    def test_evidence_fields(self, tmp_path):
        """DISK-GROWTH-01 evidence must contain required fields."""
        days = 7.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        add_dir_series(
            store,
            "/var/log",
            start=int(1 * _GB),
            daily_growth=200 * _MB,
            days=days,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import DiskGrowthRule

        orig = DiskGrowthRule.threshold_bytes_per_day
        DiskGrowthRule.threshold_bytes_per_day = 100 * _MB
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["DISK-GROWTH-01"])
        finally:
            DiskGrowthRule.threshold_bytes_per_day = orig

        findings = [f for f in diag.findings if f.rule_id == "DISK-GROWTH-01"]
        assert len(findings) >= 1
        ev = findings[0].evidence
        for key in ("path", "daily_growth_mb", "threshold_mb", "observation_days"):
            assert key in ev, f"Missing evidence field: {key!r}"
        store.close()

    def test_proposal_five_sections(self, tmp_path):
        """DISK-GROWTH-01 proposal must contain all 5 section headers."""
        days = 7.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        add_dir_series(
            store,
            "/var/log",
            start=int(1 * _GB),
            daily_growth=200 * _MB,
            days=days,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import DiskGrowthRule

        orig = DiskGrowthRule.threshold_bytes_per_day
        DiskGrowthRule.threshold_bytes_per_day = 100 * _MB
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["DISK-GROWTH-01"])
        finally:
            DiskGrowthRule.threshold_bytes_per_day = orig

        findings = [f for f in diag.findings if f.rule_id == "DISK-GROWTH-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section: {header!r}"
        store.close()


# ---------------------------------------------------------------------------
# DISK-JOURNAL-01
# ---------------------------------------------------------------------------


class TestDiskJournal:
    """Tests for DISK-JOURNAL-01."""

    def test_detected_when_over_threshold(self, tmp_path):
        """Journal directory over warn_gb → DISK-JOURNAL-01 WARNING."""
        days = 3.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        # Start at 3 GB, grow to 5 GB — above default 4 GB threshold.
        add_dir_series(
            store,
            "/var/log/journal",
            start=int(3 * _GB),
            daily_growth=int(0.7 * _GB),
            days=days,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=0.5, journal_size_warn_gb=4.0))
        diag = engine.run(store, only=["DISK-JOURNAL-01"])
        findings = [f for f in diag.findings if f.rule_id == "DISK-JOURNAL-01"]
        assert len(findings) >= 1
        f = findings[0]
        assert f.severity == Severity.WARNING
        assert f.subject == "/var/log/journal"
        assert f.evidence["size_gb"] > 4.0
        store.close()

    def test_not_detected_when_below_threshold(self, tmp_path):
        """Journal directory below warn_gb → no finding."""
        days = 3.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        # 2 GB — below 4 GB threshold.
        add_dir_series(
            store,
            "/var/log/journal",
            start=int(1 * _GB),
            daily_growth=int(0.3 * _GB),
            days=days,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=0.5, journal_size_warn_gb=4.0))
        diag = engine.run(store, only=["DISK-JOURNAL-01"])
        findings = [f for f in diag.findings if f.rule_id == "DISK-JOURNAL-01"]
        assert len(findings) == 0
        store.close()

    def test_prefix_match_subdirectory(self, tmp_path):
        """A path starting with /var/log/journal/ should also trigger."""
        days = 2.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        # Use a machine-specific subdirectory path.
        add_dir_series(
            store,
            "/var/log/journal/abc123",
            start=int(4 * _GB),
            daily_growth=int(0.5 * _GB),
            days=days,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=0.5, journal_size_warn_gb=4.0))
        diag = engine.run(store, only=["DISK-JOURNAL-01"])
        findings = [f for f in diag.findings if f.rule_id == "DISK-JOURNAL-01"]
        assert len(findings) >= 1
        store.close()

    def test_non_journal_path_not_triggered(self, tmp_path):
        """A large /var/log directory that is not journal should not trigger."""
        days = 3.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        add_dir_series(
            store,
            "/var/log",
            start=int(5 * _GB),
            daily_growth=int(0.5 * _GB),
            days=days,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=0.5, journal_size_warn_gb=4.0))
        diag = engine.run(store, only=["DISK-JOURNAL-01"])
        findings = [f for f in diag.findings if f.rule_id == "DISK-JOURNAL-01"]
        assert len(findings) == 0
        store.close()

    def test_custom_warn_threshold(self, tmp_path):
        """A custom journal_size_warn_gb should override the default."""
        days = 2.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        # 2 GB journal — would not trigger at 4 GB default, but should at 1 GB.
        add_dir_series(
            store,
            "/var/log/journal",
            start=int(1 * _GB),
            daily_growth=int(0.5 * _GB),
            days=days,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import DiskJournalRule

        orig = DiskJournalRule.journal_size_warn_gb
        DiskJournalRule.journal_size_warn_gb = 1.0
        try:
            engine = Engine(_cfg(min_obs_hours=0.5, journal_size_warn_gb=1.0))
            diag = engine.run(store, only=["DISK-JOURNAL-01"])
        finally:
            DiskJournalRule.journal_size_warn_gb = orig

        findings = [f for f in diag.findings if f.rule_id == "DISK-JOURNAL-01"]
        assert len(findings) >= 1
        store.close()

    def test_proposal_five_sections(self, tmp_path):
        """DISK-JOURNAL-01 proposal must contain all 5 section headers."""
        days = 3.0
        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)
        add_dir_series(
            store,
            "/var/log/journal",
            start=int(3 * _GB),
            daily_growth=int(0.7 * _GB),
            days=days,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=0.5, journal_size_warn_gb=4.0))
        diag = engine.run(store, only=["DISK-JOURNAL-01"])
        findings = [f for f in diag.findings if f.rule_id == "DISK-JOURNAL-01"]
        assert len(findings) >= 1
        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section: {header!r}"
        store.close()


# ---------------------------------------------------------------------------
# DISK-GROWTH-01 + DISK-FULL-01 coexistence → engine fold
# ---------------------------------------------------------------------------


class TestDiskGrowthFold:
    """DISK-GROWTH-01 findings should be absorbed by DISK-FULL-01 via engine fold."""

    def test_growth_folded_into_full_via_engine(self, tmp_path):
        """When both DISK-FULL-01 and DISK-GROWTH-01 fire on same mountpoint,
        the engine folds DISK-GROWTH-01 into DISK-FULL-01.related.

        DISK-FULL-01 fires on disk_samples (mountpoint=/var).
        DISK-GROWTH-01 fires on dir_samples (path=/var/log → mountpoint=/var).
        The engine fold map has DISK-GROWTH-01 → DISK-FULL-01, and the
        subject of DISK-GROWTH-01 is set to the mountpoint so subjects match.
        """
        days = 14.0
        capacity = int(100 * _GB)

        store = make_db(tmp_path, hours=days * 24)
        add_sys_baseline(store, hours=days * 24)

        # DISK-FULL-01: /var partition filling up (20 days left → WARNING).
        daily_growth_disk = int(capacity / (20.0 + days))
        add_disk_series(
            store,
            "/var",
            total=capacity,
            used_start=int(capacity * 0.1),
            daily_growth=daily_growth_disk,
            days=days,
        )

        # DISK-GROWTH-01: /var/log growing fast within /var.
        add_dir_series(
            store,
            "/var/log",
            start=int(2 * _GB),
            daily_growth=200 * _MB,
            days=days,
        )
        store.conn.commit()

        from procdiag.diagnosis.io_disk import DiskFullRule, DiskGrowthRule

        orig_warn = DiskFullRule.warning_days
        orig_thresh = DiskGrowthRule.threshold_bytes_per_day

        DiskFullRule.warning_days = 30.0
        DiskGrowthRule.threshold_bytes_per_day = 100 * _MB
        try:
            engine = Engine(_cfg(min_obs_hours=0.5))
            diag = engine.run(store, only=["DISK-FULL-01", "DISK-GROWTH-01"])
        finally:
            DiskFullRule.warning_days = orig_warn
            DiskGrowthRule.threshold_bytes_per_day = orig_thresh

        # The engine should fold DISK-GROWTH-01 into DISK-FULL-01.related.
        full_findings = [f for f in diag.findings if f.rule_id == "DISK-FULL-01"]
        growth_findings = [f for f in diag.findings if f.rule_id == "DISK-GROWTH-01"]

        # DISK-GROWTH-01 must NOT appear as a top-level finding.
        assert len(growth_findings) == 0, (
            "DISK-GROWTH-01 should be folded into DISK-FULL-01, "
            "not remain as top-level finding"
        )

        # DISK-FULL-01 must be present and reference DISK-GROWTH-01 in related.
        assert len(full_findings) >= 1
        assert (
            "DISK-GROWTH-01" in full_findings[0].related
        ), "DISK-GROWTH-01 should appear in DISK-FULL-01.related after fold"
        store.close()
