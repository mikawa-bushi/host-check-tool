"""Tests for EXT-02: PSS automatic wiring and MEM-LEAK-01 confidence promotion.

Part 1 — Recorder._job_pss_precheck:
  - Candidate extraction from proc_rollup_1m (slope > 1 MB/h, r2 > 0.8)
  - Top-5 cap, slope-descending order
  - Full replacement of pss_targets (not additive)
  - No-op when the sampler has no pss_targets attribute (fake sampler guard)
  - ops_events row recorded when targets change

Part 2 — MemLeakRule PSS confidence promotion:
  - PSS series present with slope > 0.5 MB/h + r2 > 0.8 → HIGH confidence
  - PSS series present with slope <= 0.1 MB/h → LOW confidence + pss_note
  - No PSS data → original confidence unchanged
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from procdiag.config import ProcdiagConfig, SamplingConfig
from procdiag.core import Confidence, Window
from procdiag.recorder import Recorder
from procdiag.storage import Storage

_MB = 1024 * 1024
_HOUR = 3600.0


# ---------------------------------------------------------------------------
# Helpers: build a Storage with synthetic proc_rollup_1m data
# ---------------------------------------------------------------------------


def _make_storage(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "pss_test.db")
    store.set_meta("hostname", "test-host")
    store.conn.commit()
    return store


def _register_proc(store: Storage, name: str) -> int:
    proc_id = store.proc_id_for(
        name=name,
        cmdline_hash=f"hash_{name}",
        cmdline_sample=f"/usr/bin/{name}",
        ts=time.time() - 6 * _HOUR,
    )
    store.conn.commit()
    return proc_id


def _add_rollup_leak(
    store: Storage,
    proc_id: int,
    *,
    rss_start: int,
    slope_bytes_per_h: float,
    hours: float = 6.0,
    n_points: int = 120,
) -> None:
    """Insert proc_rollup_1m rows with a deterministic linear RSS ramp."""
    now = time.time()
    start_ts = now - hours * _HOUR
    rows = []
    for i in range(n_points):
        ts = start_ts + i * (hours * _HOUR / n_points)
        elapsed_h = i * hours / n_points
        rss_avg = int(rss_start + slope_bytes_per_h * elapsed_h)
        rows.append(
            (
                ts,
                proc_id,
                10.0,  # cpu_avg
                12.0,  # cpu_max
                rss_avg,  # rss_avg
                int(rss_avg * 1.05),  # rss_max
                50,  # fds_max
                8,  # threads_max
            )
        )
    store.buffer("proc_rollup_1m", rows)
    store.flush(reason="synth")


# ---------------------------------------------------------------------------
# Helpers: fake sampler with and without pss_targets
# ---------------------------------------------------------------------------


class FakeSamplerWithPss:
    """Minimal fake that exposes pss_targets."""

    def __init__(self) -> None:
        self.pss_targets: set[int] = set()


class FakeSamplerWithoutPss:
    """Fake sampler that does NOT expose pss_targets (mirrors test_recorder fakes)."""

    pass


# ---------------------------------------------------------------------------
# Part 1: _job_pss_precheck behaviour
# ---------------------------------------------------------------------------


class TestPssPrecheck:
    def _make_recorder_with_storage(
        self,
        store: Storage,
        sampler_obj: Any,
        tmp_path: Path,
    ) -> Recorder:
        """Build a Recorder with the given storage and a single fake sampler."""
        config = ProcdiagConfig(
            sampling=SamplingConfig(
                system_interval=0.1,
                cgroup_interval=0.5,
                process_interval=0.5,
                disk_interval=5.0,
                dir_scan_interval=10.0,
                pss_precheck_interval=0.0,  # disable auto-scheduling for unit tests
            )
        )
        return Recorder(
            config,
            db_path=tmp_path / "unused.db",
            samplers={"process": sampler_obj},  # type: ignore[arg-type]
            storage=store,
        )

    def test_candidates_above_threshold(self, tmp_path: Path):
        """Processes with slope > 1 MB/h and r2 > 0.8 are selected."""
        store = _make_storage(tmp_path)
        proc_id = _register_proc(store, "leaker")
        _add_rollup_leak(store, proc_id, rss_start=100 * _MB, slope_bytes_per_h=5 * _MB)

        sampler = FakeSamplerWithPss()
        rec = self._make_recorder_with_storage(store, sampler, tmp_path)
        rec._job_pss_precheck()

        assert proc_id in sampler.pss_targets
        store.close()

    def test_flat_process_not_selected(self, tmp_path: Path):
        """Flat RSS (slope ≈ 0) must not appear in pss_targets."""
        store = _make_storage(tmp_path)
        proc_id = _register_proc(store, "stable")
        _add_rollup_leak(store, proc_id, rss_start=200 * _MB, slope_bytes_per_h=0.0)

        sampler = FakeSamplerWithPss()
        rec = self._make_recorder_with_storage(store, sampler, tmp_path)
        rec._job_pss_precheck()

        assert proc_id not in sampler.pss_targets
        store.close()

    def test_top5_cap(self, tmp_path: Path):
        """At most 5 candidates are returned even when more qualify."""
        store = _make_storage(tmp_path)
        proc_ids = []
        for i in range(8):
            pid = _register_proc(store, f"leaker_{i}")
            proc_ids.append(pid)
            _add_rollup_leak(
                store,
                pid,
                rss_start=100 * _MB,
                slope_bytes_per_h=(2 + i) * _MB,
            )

        sampler = FakeSamplerWithPss()
        rec = self._make_recorder_with_storage(store, sampler, tmp_path)
        rec._job_pss_precheck()

        assert len(sampler.pss_targets) == 5
        store.close()

    def test_targets_fully_replaced(self, tmp_path: Path):
        """Calling precheck twice replaces targets, not appends."""
        store = _make_storage(tmp_path)
        pid_a = _register_proc(store, "proc_a")
        _add_rollup_leak(store, pid_a, rss_start=100 * _MB, slope_bytes_per_h=5 * _MB)

        sampler = FakeSamplerWithPss()
        sampler.pss_targets = {9999}  # pre-existing stale entry

        rec = self._make_recorder_with_storage(store, sampler, tmp_path)
        rec._job_pss_precheck()

        # Stale proc 9999 must not survive.
        assert 9999 not in sampler.pss_targets
        store.close()

    def test_no_attr_sampler_is_noop(self, tmp_path: Path):
        """Sampler without pss_targets attribute → job does nothing (no error)."""
        store = _make_storage(tmp_path)
        sampler = FakeSamplerWithoutPss()
        rec = self._make_recorder_with_storage(store, sampler, tmp_path)
        # Must not raise.
        rec._job_pss_precheck()
        store.close()

    def test_ops_events_recorded_on_change(self, tmp_path: Path):
        """ops_events row kind='pss_scrutiny' is recorded when targets change."""
        store = _make_storage(tmp_path)
        proc_id = _register_proc(store, "monitored")
        _add_rollup_leak(store, proc_id, rss_start=100 * _MB, slope_bytes_per_h=5 * _MB)

        sampler = FakeSamplerWithPss()
        rec = self._make_recorder_with_storage(store, sampler, tmp_path)
        rec._job_pss_precheck()

        rows = store.conn.execute(
            "SELECT * FROM ops_events WHERE kind='pss_scrutiny'"
        ).fetchall()
        assert len(rows) >= 1
        store.close()

    def test_no_ops_event_when_targets_unchanged(self, tmp_path: Path):
        """No ops_events row when pss_targets did not change."""
        store = _make_storage(tmp_path)
        proc_id = _register_proc(store, "steady_leak")
        _add_rollup_leak(store, proc_id, rss_start=100 * _MB, slope_bytes_per_h=5 * _MB)

        sampler = FakeSamplerWithPss()
        rec = self._make_recorder_with_storage(store, sampler, tmp_path)

        # First call sets targets and logs the event.
        rec._job_pss_precheck()
        count_after_first = store.conn.execute(
            "SELECT COUNT(*) AS c FROM ops_events WHERE kind='pss_scrutiny'"
        ).fetchone()["c"]

        # Second call with unchanged data must NOT add another event.
        rec._job_pss_precheck()
        count_after_second = store.conn.execute(
            "SELECT COUNT(*) AS c FROM ops_events WHERE kind='pss_scrutiny'"
        ).fetchone()["c"]

        assert count_after_second == count_after_first
        store.close()

    def test_slope_descending_order(self, tmp_path: Path):
        """When > 5 candidates exist, the 5 with highest slope are chosen."""
        store = _make_storage(tmp_path)
        # Insert 6 processes; the one with the smallest slope should be excluded.
        slopes = [10, 9, 8, 7, 6, 2]  # MB/h; last one is the smallest
        pids = []
        for i, slope in enumerate(slopes):
            pid = _register_proc(store, f"p{i}")
            pids.append(pid)
            _add_rollup_leak(
                store, pid, rss_start=100 * _MB, slope_bytes_per_h=slope * _MB
            )

        sampler = FakeSamplerWithPss()
        rec = self._make_recorder_with_storage(store, sampler, tmp_path)
        rec._job_pss_precheck()

        assert len(sampler.pss_targets) == 5
        # The smallest-slope process (slopes[5]=2, pids[5]) should be dropped.
        assert pids[5] not in sampler.pss_targets
        store.close()


# ---------------------------------------------------------------------------
# Part 2: MEM-LEAK-01 PSS confidence promotion
# ---------------------------------------------------------------------------


def _add_proc_samples_pss(
    store: Storage,
    proc_id: int,
    *,
    pss_start: int,
    pss_slope_per_h: float,
    hours: float = 4.0,
    n_points: int = 30,
) -> None:
    """Insert proc_samples rows with a linear PSS series for one proc_id."""
    now = time.time()
    start_ts = now - hours * _HOUR
    rows = []
    for i in range(n_points):
        ts = start_ts + i * (hours * _HOUR / n_points)
        elapsed_h = i * hours / n_points
        pss_val = int(pss_start + pss_slope_per_h * elapsed_h)
        rss_val = int(pss_val * 1.5)
        rows.append(
            (
                ts,
                proc_id,
                1000 + i,  # pid
                5.0,  # cpu_pct
                rss_val,  # rss
                pss_val,  # pss
                4,  # num_threads
                20,  # num_fds
                None,  # io_read_delta
                None,  # io_write_delta
            )
        )
    store.buffer("proc_samples", rows)
    store.flush(reason="synth")


def _add_rollup_and_register(
    store: Storage,
    name: str,
    *,
    rss_slope_per_h: float = 5 * _MB,
    hours: float = 18.0,
    n_points: int = 200,
) -> int:
    """Register a process and add proc_rollup_1m rows."""
    proc_id = _register_proc(store, name)
    _add_rollup_leak(
        store,
        proc_id,
        rss_start=100 * _MB,
        slope_bytes_per_h=rss_slope_per_h,
        hours=hours,
        n_points=n_points,
    )
    return proc_id


class TestMemLeakPssPromotion:
    """Test that MEM-LEAK-01 promotes / demotes confidence using PSS data."""

    def _run_rule(self, store: Storage, *, hours: float) -> list:
        from procdiag.diagnosis.memory import MemLeakRule

        rule = MemLeakRule()
        now = time.time()
        window = Window(start_ts=now - hours * _HOUR, end_ts=now)
        return rule.evaluate(store, window)

    def test_pss_high_slope_promotes_to_high(self, tmp_path: Path):
        """PSS slope > 0.5 MB/h with r2 > 0.8 → confidence=HIGH."""
        store = _make_storage(tmp_path)
        proc_id = _add_rollup_and_register(store, "leaker", hours=18.0)
        # Add 30 PSS samples with a steep slope (2 MB/h over 4 hours → r2 ≈ 1).
        _add_proc_samples_pss(
            store,
            proc_id,
            pss_start=80 * _MB,
            pss_slope_per_h=2 * _MB,
            hours=4.0,
            n_points=30,
        )
        store.conn.commit()

        findings = self._run_rule(store, hours=18.0)
        leak = [f for f in findings if f.rule_id == "MEM-LEAK-01"]
        assert len(leak) >= 1
        f = leak[0]
        assert f.confidence == Confidence.HIGH
        assert "pss_slope_mb_per_h" in f.evidence
        store.close()

    def test_pss_flat_demotes_to_low(self, tmp_path: Path):
        """PSS slope <= 0.1 MB/h → confidence=LOW + pss_note."""
        store = _make_storage(tmp_path)
        proc_id = _add_rollup_and_register(store, "cached", hours=18.0)
        # Flat PSS (0 slope) → shared memory / cache hypothesis.
        _add_proc_samples_pss(
            store,
            proc_id,
            pss_start=50 * _MB,
            pss_slope_per_h=0.0,  # completely flat
            hours=4.0,
            n_points=30,
        )
        store.conn.commit()

        findings = self._run_rule(store, hours=18.0)
        leak = [f for f in findings if f.rule_id == "MEM-LEAK-01"]
        assert len(leak) >= 1
        f = leak[0]
        assert f.confidence == Confidence.LOW
        assert "pss_note" in f.evidence
        store.close()

    def test_no_pss_data_unchanged_confidence(self, tmp_path: Path):
        """No PSS samples → confidence follows original RSS-based logic."""
        store = _make_storage(tmp_path)
        _add_rollup_and_register(store, "norss", hours=18.0)
        # No proc_samples inserted → PSS series is empty.
        store.conn.commit()

        findings = self._run_rule(store, hours=18.0)
        leak = [f for f in findings if f.rule_id == "MEM-LEAK-01"]
        assert len(leak) >= 1
        f = leak[0]
        # Confidence is based on RSS fit alone (HIGH/MEDIUM/LOW depending on
        # r2 and span_hours); pss_slope_mb_per_h must not be present.
        assert "pss_slope_mb_per_h" not in f.evidence
        assert "pss_note" not in f.evidence
        store.close()
