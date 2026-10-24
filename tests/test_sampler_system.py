"""Tests for procdiag.samplers.system (SystemSampler + parse_psi)
and procdiag.samplers.selfmeter (SelfMeter).

Fixture layout
--------------
tests/fixtures/proc_tree/
    loadavg               — "1.25 0.75 0.50 2/512 12345"
    pressure/
        cpu               — PSI cpu file
        memory            — PSI memory file (some + full)
        io                — PSI io file (some + full)

A second fixture tree ``proc_tree_nopsi/`` has *no* pressure directory to
exercise the PSI-less code path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from procdiag.config import BudgetConfig, ProcdiagConfig
from procdiag.core.sampler import RunContext, SampleBatch, SystemSnapshot
from procdiag.samplers.selfmeter import SelfMeter
from procdiag.samplers.system import SystemSampler, parse_psi
from procdiag.storage import TABLE_COLUMNS

# ---------------------------------------------------------------------------
# Paths to fixture trees
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PROC_TREE = FIXTURES_DIR / "proc_tree"
PROC_TREE_NOPSI = FIXTURES_DIR / "proc_tree_nopsi"  # no pressure dir


@pytest.fixture(scope="session", autouse=True)
def _create_nopsi_tree(tmp_path_factory):
    """Create the no-PSI fixture tree once per test session."""
    nopsi = PROC_TREE_NOPSI
    nopsi.mkdir(parents=True, exist_ok=True)
    loadavg = nopsi / "loadavg"
    if not loadavg.exists():
        loadavg.write_text("0.50 0.30 0.20 1/256 99\n")
    # Deliberately do NOT create nopsi/pressure/


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    proc_root: Path = PROC_TREE,
    config: Optional[ProcdiagConfig] = None,
) -> RunContext:
    if config is None:
        config = ProcdiagConfig()
    return RunContext(config, proc_root=proc_root)


def _col_count() -> int:
    return len(TABLE_COLUMNS["sys_samples"])


# ===========================================================================
# parse_psi — pure function tests
# ===========================================================================


class TestParsePsi:
    def test_cpu_file_some_only(self):
        """CPU PSI file has only a 'some' line (no 'full')."""
        text = "some avg10=2.30 avg60=1.50 avg300=0.80 total=1234567\n"
        result = parse_psi(text)
        assert result["some_avg10"] == pytest.approx(2.30)
        assert result["some_avg60"] == pytest.approx(1.50)
        assert result["some_avg300"] == pytest.approx(0.80)
        assert result["some_total"] == pytest.approx(1234567.0)
        assert "full_avg10" not in result

    def test_memory_file_some_and_full(self):
        """Memory PSI file has both 'some' and 'full' lines."""
        text = (
            "some avg10=1.20 avg60=0.80 avg300=0.40 total=987654\n"
            "full avg10=0.10 avg60=0.05 avg300=0.02 total=111111\n"
        )
        result = parse_psi(text)
        assert result["some_avg10"] == pytest.approx(1.20)
        assert result["full_avg10"] == pytest.approx(0.10)
        assert result["full_total"] == pytest.approx(111111.0)

    def test_keys_allowlist_restricts_parsing(self):
        """keys={'avg10'} parses only the 10s averages (issue v0.3 §4-6)."""
        text = (
            "some avg10=2.30 avg60=1.50 avg300=0.80 total=1234567\n"
            "full avg10=0.10 avg60=0.05 avg300=0.02 total=111111\n"
        )
        result = parse_psi(text, keys=frozenset({"avg10"}))
        assert result["some_avg10"] == pytest.approx(2.30)
        assert result["full_avg10"] == pytest.approx(0.10)
        assert "some_avg60" not in result
        assert "some_avg300" not in result
        assert "some_total" not in result
        assert "full_total" not in result

    def test_empty_string_returns_empty_dict(self):
        """Empty input must not raise; return an empty dict."""
        result = parse_psi("")
        assert result == {}

    def test_broken_lines_skipped(self):
        """Garbled tokens should be ignored; valid tokens still extracted."""
        text = (
            "some avg10=BROKEN avg60=1.00 avg300=nope total=42\n"
            "full avg10=0.50 avg60=0.25 avg300=0.10 total=100\n"
            "noise without equals sign\n"
            "unknown_kind avg10=9.9\n"
        )
        result = parse_psi(text)
        # broken avg10 skipped, but avg60 extracted
        assert "some_avg10" not in result
        assert result["some_avg60"] == pytest.approx(1.00)
        assert result["some_total"] == pytest.approx(42.0)
        assert result["full_avg10"] == pytest.approx(0.50)
        # unknown_kind should not appear
        assert "unknown_kind_avg10" not in result

    def test_io_file(self):
        """Typical io PSI file."""
        text = (
            "some avg10=5.40 avg60=3.20 avg300=1.80 total=2345678\n"
            "full avg10=0.50 avg60=0.30 avg300=0.15 total=222222\n"
        )
        result = parse_psi(text)
        assert result["some_avg10"] == pytest.approx(5.40)
        assert result["full_avg10"] == pytest.approx(0.50)


# ===========================================================================
# SystemSampler — setup / capability detection
# ===========================================================================


class TestSystemSamplerSetup:
    def test_psi_detected_with_fixture(self):
        """With the full fixture tree, PSI capability should be True."""
        sampler = SystemSampler()
        ctx = _make_ctx(proc_root=PROC_TREE)
        sampler.setup(ctx)
        assert ctx.capabilities["psi"] is True
        assert ctx.notes == []

    def test_psi_absent_degrades(self):
        """Without pressure dir, PSI capability is False and a note is added."""
        sampler = SystemSampler()
        ctx = _make_ctx(proc_root=PROC_TREE_NOPSI)
        sampler.setup(ctx)
        assert ctx.capabilities["psi"] is False
        assert len(ctx.notes) == 1
        assert "PSI" in ctx.notes[0]


# ===========================================================================
# SystemSampler — collect returns correct shape
# ===========================================================================


class TestSystemSamplerCollect:
    def _collect_once(self, proc_root=PROC_TREE) -> SampleBatch:
        sampler = SystemSampler()
        ctx = _make_ctx(proc_root=proc_root)
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        assert len(batches) == 1
        batch = batches[0]
        assert batch.table == "sys_samples"
        return batch

    def test_returns_one_row(self):
        """collect() must return exactly one row."""
        batch = self._collect_once()
        assert len(batch.rows) == 1

    def test_row_column_count_matches_schema(self):
        """Row tuple length must match TABLE_COLUMNS["sys_samples"]."""
        batch = self._collect_once()
        row = batch.rows[0]
        assert len(row) == _col_count(), (
            f"Expected {_col_count()} columns, got {len(row)}. "
            f"Columns: {TABLE_COLUMNS['sys_samples']}"
        )

    def test_psi_columns_populated_with_psi_tree(self):
        """With PSI fixture, psi_* columns should be non-None floats."""
        batch = self._collect_once(proc_root=PROC_TREE)
        row = batch.rows[0]
        cols = TABLE_COLUMNS["sys_samples"]
        idx = {name: i for i, name in enumerate(cols)}

        assert row[idx["psi_cpu_some10"]] is not None
        assert row[idx["psi_mem_some10"]] is not None
        assert row[idx["psi_mem_full10"]] is not None
        assert row[idx["psi_io_some10"]] is not None
        assert row[idx["psi_io_full10"]] is not None

    def test_psi_columns_null_without_psi(self):
        """Without PSI support, all psi_* columns must be None."""
        batch = self._collect_once(proc_root=PROC_TREE_NOPSI)
        row = batch.rows[0]
        cols = TABLE_COLUMNS["sys_samples"]
        idx = {name: i for i, name in enumerate(cols)}

        for col in (
            "psi_cpu_some10",
            "psi_mem_some10",
            "psi_mem_full10",
            "psi_io_some10",
            "psi_io_full10",
        ):
            assert row[idx[col]] is None, f"Expected {col} to be None"

    def test_self_columns_none_without_selfmeter(self):
        """When ctx.selfmeter is None, self_cpu and self_rss must be None."""
        batch = self._collect_once()
        row = batch.rows[0]
        cols = TABLE_COLUMNS["sys_samples"]
        idx = {name: i for i, name in enumerate(cols)}
        assert row[idx["self_cpu"]] is None
        assert row[idx["self_rss"]] is None

    def test_snapshot_populated(self):
        """After collect(), last_snapshot must be a SystemSnapshot."""
        sampler = SystemSampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        sampler.collect(ctx)
        assert isinstance(sampler.last_snapshot, SystemSnapshot)

    def test_load1_from_fixture(self):
        """load1 column should match the fixture's loadavg first value (1.25)."""
        batch = self._collect_once(proc_root=PROC_TREE)
        row = batch.rows[0]
        cols = TABLE_COLUMNS["sys_samples"]
        idx = {name: i for i, name in enumerate(cols)}
        assert row[idx["load1"]] == pytest.approx(1.25)


# ===========================================================================
# SystemSampler — delta accumulation (2 consecutive collects)
# ===========================================================================


class TestSystemSamplerDeltas:
    def test_deltas_non_negative_after_two_collects(self):
        """swap and disk delta columns must be >= 0 after two collects."""
        sampler = SystemSampler()
        ctx = _make_ctx()
        sampler.setup(ctx)

        # First collect — deltas are 0 (baseline established).
        sampler.collect(ctx)

        # Second collect — should yield non-negative deltas.
        batches2 = sampler.collect(ctx)
        row2 = batches2[0].rows[0]
        cols = TABLE_COLUMNS["sys_samples"]
        idx = {name: i for i, name in enumerate(cols)}

        assert row2[idx["swap_in"]] >= 0
        assert row2[idx["swap_out"]] >= 0
        assert row2[idx["disk_read_bytes"]] >= 0
        assert row2[idx["disk_write_bytes"]] >= 0


# ===========================================================================
# SystemSampler — real /proc (live machine sanity check)
# ===========================================================================


@pytest.mark.real_proc
def test_collect_real_proc_no_exception():
    """collect() against the real /proc must not raise an exception."""
    sampler = SystemSampler()
    ctx = RunContext(ProcdiagConfig())  # uses real /proc
    sampler.setup(ctx)
    batches = sampler.collect(ctx)
    assert len(batches) == 1
    assert len(batches[0].rows) == 1
    assert len(batches[0].rows[0]) == _col_count()


# ===========================================================================
# SelfMeter tests
# ===========================================================================


def _make_meter(
    overhead_budget_pct: float = 1.0,
    throttle_factor: float = 1.5,
    throttle_max_interval: float = 60.0,
) -> SelfMeter:
    cfg = BudgetConfig(
        overhead_budget_pct=overhead_budget_pct,
        throttle_factor=throttle_factor,
        throttle_max_interval=throttle_max_interval,
    )
    return SelfMeter(cfg)


class TestSelfMeter:
    def test_factor_starts_at_one(self):
        """Initial factor must be 1.0."""
        meter = _make_meter()
        assert meter.factor == pytest.approx(1.0)

    def test_sample_returns_tuple(self):
        """sample() should return a (float, int) tuple."""
        meter = _make_meter()
        cpu, rss = meter.sample()
        assert isinstance(cpu, float)
        assert isinstance(rss, int)
        assert rss > 0

    def test_throttle_down_when_over_budget(self):
        """When the moving average exceeds the budget, throttle_down is emitted."""
        meter = _make_meter(overhead_budget_pct=1.0, throttle_factor=1.5)

        # Patch sample() to return a high CPU value every call.
        high_cpu = 50.0
        rss_val = 1024 * 1024

        def fake_sample() -> tuple:
            return high_cpu, rss_val

        meter.sample = fake_sample  # type: ignore[method-assign]

        # Drive the clock forward; all calls should be over budget.
        mono_base = 1000.0
        result = None
        for i in range(65):
            event = meter.update(mono_base + i)
            if event is not None:
                result = event
                break

        assert result == "throttle_down"
        assert meter.factor > 1.0

    def test_factor_increases_on_throttle_down(self):
        """factor must be > 1.0 after a throttle_down event."""
        meter = _make_meter(overhead_budget_pct=1.0, throttle_factor=1.5)
        high_cpu = 50.0

        def fake_sample():
            return high_cpu, 1024

        meter.sample = fake_sample  # type: ignore[method-assign]

        mono_base = 2000.0
        for i in range(65):
            meter.update(mono_base + i)

        assert meter.factor > 1.0

    def test_throttle_restore_after_patience(self):
        """After 10 minutes within budget following a throttle-down, throttle_restore is emitted."""
        meter = _make_meter(
            overhead_budget_pct=1.0, throttle_factor=1.5, throttle_max_interval=60.0
        )

        # Phase 1: drive over budget to trigger throttle_down.

        def phase1_sample():
            return 50.0, 1024

        meter.sample = phase1_sample  # type: ignore[method-assign]

        mono_base = 3000.0
        throttled = False
        for i in range(70):
            event = meter.update(mono_base + i)
            if event == "throttle_down":
                throttled = True
                break
        assert throttled, "Expected throttle_down in phase 1"

        # Phase 2: switch to a low CPU reading to stay under budget.
        def phase2_sample():
            return 0.0, 1024

        meter.sample = phase2_sample  # type: ignore[method-assign]

        # Advance the clock by > 10 minutes (600 s) while staying under budget.
        restore_event = None
        for j in range(700):
            # Clock advances at 1 s per tick from the point after throttle
            event = meter.update(mono_base + 70 + j)
            if event == "throttle_restore":
                restore_event = event
                break

        assert restore_event == "throttle_restore"
        assert meter.factor == pytest.approx(1.0)
