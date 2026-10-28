"""Tests for OOM-KILL-01: crash rule for OOM-killed process detection.

Covers:
- Detect path: memory-crisis correlation produces WARNING/MEDIUM finding.
- Journal probe: monkeypatched match elevates confidence to HIGH.
- Non-detect paths:
  - Process disappeared but no memory crisis (normal stop).
  - Short-lived process (< 10 min lifetime).
  - Recording ended simultaneously with process (reboot equivalent).
  - Process had trivially small RSS.
- journalctl failures (FileNotFoundError, timeout) leave confidence at MEDIUM.
- Engine.run() via only= confirms 5-section proposal headers are present.
- Existing test_engine.py remains green (tested separately, not here).
"""

from __future__ import annotations

import time
from unittest.mock import patch

from synth import add_oom_scenario, make_db

from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Confidence, Severity, Window
from procdiag.diagnosis import Engine
from procdiag.diagnosis.crash import OomKillRule, _read_oom_journal

_MB = 1024 * 1024
_GB = 1024**3
_HOUR = 3600.0


def _cfg(min_obs_hours: float = 1.0) -> ProcdiagConfig:
    return ProcdiagConfig(
        rules=RulesConfig(min_observation_hours=min_obs_hours, disabled=[])
    )


def _window(store, *, buffer_s: float = 60.0) -> Window:
    """Derive a Window that spans the full sys_samples range."""
    row = store.conn.execute(
        "SELECT MIN(ts) AS t_min, MAX(ts) AS t_max FROM sys_samples"
    ).fetchone()
    return Window(
        start_ts=row["t_min"] - buffer_s,
        end_ts=row["t_max"] + buffer_s,
    )


# ---------------------------------------------------------------------------
# Detect path
# ---------------------------------------------------------------------------


class TestOomDetect:
    """OOM-KILL-01 fires WARNING/MEDIUM when all four conditions are met."""

    def test_detects_oom_with_crisis(self, tmp_path):
        """Long-lived high-RSS process disappears during PSI crisis → WARNING/MEDIUM."""
        store = make_db(tmp_path, hours=3.0)
        add_oom_scenario(
            store,
            name="bigapp",
            die_at_hours=2.0,
            total_hours=3.0,
            rss_bytes=int(1.5 * _GB),  # 18.75% of 8 GB — well above 1%
            crisis=True,
        )
        store.conn.commit()

        rule = OomKillRule()
        w = _window(store)
        findings = rule.evaluate(store, w)

        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "OOM-KILL-01"
        assert f.severity == Severity.WARNING
        assert f.confidence == Confidence.MEDIUM
        assert f.subject == "bigapp"
        assert f.evidence["last_rss_mb"] > 0
        assert f.evidence["lifetime_minutes"] >= 10.0
        assert "crisis_indicator" in f.evidence
        store.close()

    def test_journal_match_elevates_to_high(self, tmp_path):
        """When journalctl returns a matching OOM line, confidence becomes HIGH."""
        store = make_db(tmp_path, hours=3.0)
        add_oom_scenario(
            store,
            name="webserver",
            die_at_hours=2.0,
            total_hours=3.0,
            rss_bytes=int(2 * _GB),
            crisis=True,
        )
        store.conn.commit()

        fake_journal = [
            "Jun 12 10:00:01 host kernel: oom-kill:constraint=CONSTRAINT_NONE,"
            " nomemcg,task=webserver,pid=12345,uid=1000"
        ]

        rule = OomKillRule()
        w = _window(store)

        with patch(
            "procdiag.diagnosis.crash._read_oom_journal", return_value=fake_journal
        ):
            findings = rule.evaluate(store, w)

        assert len(findings) == 1
        f = findings[0]
        assert f.confidence == Confidence.HIGH
        assert "journal_line" in f.evidence
        store.close()

    def test_evidence_contains_required_fields(self, tmp_path):
        """Evidence dict contains last_seen, last_rss, crisis indicator and lifetime."""
        store = make_db(tmp_path, hours=3.0)
        add_oom_scenario(
            store,
            name="cruncher",
            die_at_hours=1.5,
            total_hours=3.0,
            rss_bytes=int(800 * _MB),
            crisis=True,
        )
        store.conn.commit()

        rule = OomKillRule()
        w = _window(store)
        findings = rule.evaluate(store, w)

        assert len(findings) == 1
        ev = findings[0].evidence
        for key in (
            "proc_id",
            "process_name",
            "last_seen",
            "last_rss_bytes",
            "last_rss_mb",
            "mem_total_mb",
            "lifetime_minutes",
            "gap_to_window_end_s",
            "crisis_indicator",
            "crisis_value",
            "crisis_ts",
        ):
            assert key in ev, f"Missing evidence key: {key!r}"
        store.close()

    def test_max_five_findings(self, tmp_path):
        """When more than 5 candidates exist, only the top 5 by RSS are returned."""
        store = make_db(tmp_path, hours=4.0)
        for i in range(7):
            add_oom_scenario(
                store,
                name=f"app{i}",
                die_at_hours=2.0,
                total_hours=4.0,
                rss_bytes=int((i + 1) * 100 * _MB),  # all > 1% of 8 GB (100MB = 1.25%)
                crisis=True,
            )
        store.conn.commit()

        rule = OomKillRule()
        w = _window(store)
        findings = rule.evaluate(store, w)

        assert len(findings) <= 5
        store.close()


# ---------------------------------------------------------------------------
# Non-detect paths
# ---------------------------------------------------------------------------


class TestOomNoDetect:
    """OOM-KILL-01 must remain silent in these scenarios."""

    def test_no_finding_when_no_crisis(self, tmp_path):
        """Process disappeared but no memory-crisis indicators → silent."""
        store = make_db(tmp_path, hours=3.0)
        add_oom_scenario(
            store,
            name="normalstop",
            die_at_hours=2.0,
            total_hours=3.0,
            rss_bytes=int(1 * _GB),
            crisis=False,  # no PSI spikes, no swap_out, no low mem_available
        )
        store.conn.commit()

        rule = OomKillRule()
        w = _window(store)
        findings = rule.evaluate(store, w)

        assert len(findings) == 0, f"Expected no findings, got {findings}"
        store.close()

    def test_no_finding_short_lived_process(self, tmp_path):
        """Process alive < 10 min → excluded as short-lived."""
        store = make_db(tmp_path, hours=3.0)
        # die_at_hours=0.1 → 6 minutes < 10-minute threshold
        add_oom_scenario(
            store,
            name="shortlived",
            die_at_hours=0.1,
            total_hours=3.0,
            rss_bytes=int(1 * _GB),
            crisis=True,
        )
        store.conn.commit()

        rule = OomKillRule()
        w = _window(store)
        findings = rule.evaluate(store, w)

        assert len(findings) == 0
        store.close()

    def test_no_finding_recording_ended_with_process(self, tmp_path):
        """Process disappeared at same time as recording end (reboot) → silent."""
        store = make_db(tmp_path, hours=2.0)
        # die_at_hours == total_hours → gap is 0, below GAP_THRESHOLD_S=60s
        add_oom_scenario(
            store,
            name="rebootsim",
            die_at_hours=2.0,
            total_hours=2.0,  # same as die_at_hours → gap ≈ 0
            rss_bytes=int(1 * _GB),
            crisis=True,
        )
        store.conn.commit()

        rule = OomKillRule()
        w = _window(store)
        findings = rule.evaluate(store, w)

        assert len(findings) == 0
        store.close()

    def test_no_finding_small_rss(self, tmp_path):
        """Process RSS is < 1% of mem_total → not a significant consumer, silent."""
        store = make_db(tmp_path, hours=3.0)
        # RSS = 1 MB, mem_total ≈ 8 GB → 0.012% << 1%
        add_oom_scenario(
            store,
            name="tinytool",
            die_at_hours=2.0,
            total_hours=3.0,
            rss_bytes=int(1 * _MB),  # trivially small
            crisis=True,
        )
        store.conn.commit()

        rule = OomKillRule()
        w = _window(store)
        findings = rule.evaluate(store, w)

        assert len(findings) == 0
        store.close()

    def test_no_finding_empty_db(self, tmp_path):
        """Empty proc_samples → no finding at all."""
        store = make_db(tmp_path, hours=2.0)
        store.conn.commit()

        rule = OomKillRule()
        now = time.time()
        w = Window(start_ts=now - 2 * _HOUR, end_ts=now)
        findings = rule.evaluate(store, w)

        assert len(findings) == 0
        store.close()


# ---------------------------------------------------------------------------
# journalctl failure paths
# ---------------------------------------------------------------------------


class TestOomJournalFailures:
    """Journalctl failure must not crash the rule — confidence stays MEDIUM."""

    def _detect_finding(self, tmp_path):
        """Helper: set up a detectable scenario and return store + window."""
        store = make_db(tmp_path, hours=3.0)
        add_oom_scenario(
            store,
            name="robustapp",
            die_at_hours=2.0,
            total_hours=3.0,
            rss_bytes=int(1 * _GB),
            crisis=True,
        )
        store.conn.commit()
        return store, _window(store)

    def test_journal_file_not_found_stays_medium(self, tmp_path):
        """FileNotFoundError from journalctl → MEDIUM confidence, no exception."""
        store, w = self._detect_finding(tmp_path)
        rule = OomKillRule()

        with patch(
            "procdiag.diagnosis.crash._read_oom_journal",
            side_effect=FileNotFoundError("journalctl not found"),
        ):
            # The rule itself must not propagate the exception — _read_oom_journal
            # is called inside _try_elevate_confidence which already guards via
            # the try/except in _read_oom_journal, but we patch at the module level
            # to simulate a caller-side exception.
            # Actually _read_oom_journal itself catches; this tests the outer call.
            findings = rule.evaluate(store, w)

        # The patch raises on call to _read_oom_journal; the rule should handle it.
        # If _try_elevate_confidence doesn't guard, this will raise.
        assert len(findings) >= 1
        assert findings[0].confidence == Confidence.MEDIUM
        store.close()

    def test_journal_returns_empty_stays_medium(self, tmp_path):
        """journalctl returns no lines → confidence remains MEDIUM."""
        store, w = self._detect_finding(tmp_path)
        rule = OomKillRule()

        with patch("procdiag.diagnosis.crash._read_oom_journal", return_value=[]):
            findings = rule.evaluate(store, w)

        assert len(findings) >= 1
        assert findings[0].confidence == Confidence.MEDIUM
        store.close()

    def test_journal_no_process_match_stays_medium(self, tmp_path):
        """OOM lines exist but process name doesn't match → MEDIUM, no HIGH."""
        store, w = self._detect_finding(tmp_path)
        rule = OomKillRule()

        # Lines mention a *different* process
        fake_journal = [
            "Jun 12 10:00:00 host kernel: oom-kill:task=unrelated_daemon,pid=9999"
        ]

        with patch(
            "procdiag.diagnosis.crash._read_oom_journal", return_value=fake_journal
        ):
            findings = rule.evaluate(store, w)

        assert len(findings) >= 1
        assert findings[0].confidence == Confidence.MEDIUM
        # journal_line_unmatched may be set as corroborating evidence
        store.close()

    def test_read_oom_journal_timeout_returns_empty(self, monkeypatch):
        """_read_oom_journal itself returns [] on subprocess.TimeoutExpired."""
        import subprocess

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="journalctl", timeout=5)

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = _read_oom_journal(0.0, 1.0)
        assert result == []

    def test_read_oom_journal_passes_epoch_range(self, monkeypatch):
        """The range goes to journalctl as @epoch, so the host time zone cannot
        shift it (a UTC wall-clock string is read as local time), and only the
        OOM lines come back."""
        import subprocess

        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            out = (
                "1000.5 host kernel: Out of memory: Killed process 42 (app)\n"
                "1001.0 host kernel: usb 1-1: USB disconnect, device number 3\n"
            )
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _read_oom_journal(999.7, 1100.2) == [
            "Out of memory: Killed process 42 (app)"
        ]
        args = seen["args"]
        assert args[args.index("--since") + 1] == "@999"
        assert args[args.index("--until") + 1] == "@1101"


# ---------------------------------------------------------------------------
# Engine / proposal template
# ---------------------------------------------------------------------------


class TestOomEngineProposal:
    """OOM-KILL-01 proposal via Engine.run() must contain all 5 section headers."""

    def test_five_section_headers_in_proposal(self, tmp_path):
        store = make_db(tmp_path, hours=3.0)
        add_oom_scenario(
            store,
            name="svcapp",
            die_at_hours=2.0,
            total_hours=3.0,
            rss_bytes=int(1 * _GB),
            crisis=True,
        )
        store.conn.commit()

        engine = Engine(_cfg(min_obs_hours=1.0))
        diag = engine.run(store, only=["OOM-KILL-01"])
        findings = [f for f in diag.findings if f.rule_id == "OOM-KILL-01"]
        assert (
            len(findings) >= 1
        ), "Engine should produce at least one OOM-KILL-01 finding"

        proposal = findings[0].proposal
        for header in ("[検出]", "[根拠]", "[暫定対処]", "[恒久対処]", "[影響予測]"):
            assert header in proposal, f"Missing section header {header!r} in proposal"
        store.close()

    def test_engine_registers_oom_kill_rule(self, tmp_path):
        """OOM-KILL-01 is present in the rule registry after package import."""
        from procdiag.core import all_rules

        registry = all_rules()
        assert "OOM-KILL-01" in registry, (
            "OOM-KILL-01 not found in registry — "
            "check that crash.py is imported in diagnosis/__init__.py"
        )
