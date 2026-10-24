"""Tests for EXT-11: Storage.enforce_size_budget (DB self-defence).

Covers:
- triggered=False when DB is within budget
- Ladder fires in order when max_bytes is tiny (steps recorded in order)
- ops_events rows are created for each ladder step
- monkeypatched shutil.disk_usage triggers the min_free guard
- Recorder._job_flush calls enforce_size_budget via config values
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

from procdiag.config import ProcdiagConfig, RetentionConfig, SamplingConfig
from procdiag.storage import Storage

_MB = 1024 * 1024
_GB = 1024**3
_HOUR = 3600.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_storage(tmp_path: Path, *, fill_mb: int = 0) -> Storage:
    """Create a Storage instance and optionally fill it with proc_samples data."""
    store = Storage(tmp_path / "guard_test.db")
    store.set_meta("hostname", "test")
    store.conn.commit()

    if fill_mb > 0:
        # Insert raw proc_samples rows to inflate the DB.
        now = time.time()
        rows = []
        n = fill_mb * 200  # ~200 rows ≈ 1 MB for the tiny test payloads
        for i in range(n):
            ts = now - i * 10.0
            rows.append((ts, 1, 1000 + i, 5.0, 100 * _MB, None, 4, 20, None, None))
        store.buffer("proc_samples", rows)
        store.flush(reason="synth")

    return store


# ---------------------------------------------------------------------------
# Basic budget guard tests
# ---------------------------------------------------------------------------


class TestEnforceSizeBudget:
    def test_no_trigger_within_budget(self, tmp_path: Path):
        """DB well within max_bytes and free space OK → triggered=False."""
        store = _make_storage(tmp_path)
        result = store.enforce_size_budget(
            max_bytes=500 * _MB,  # very generous
            min_free_bytes=None,
        )
        assert result["triggered"] is False
        store.close()

    def test_trigger_when_max_bytes_exceeded(self, tmp_path: Path):
        """Setting max_bytes below current DB size triggers the ladder."""
        store = _make_storage(tmp_path)
        # Force trigger by setting max_bytes to 1 byte.
        result = store.enforce_size_budget(
            max_bytes=1,
            min_free_bytes=None,
        )
        assert result["triggered"] is True
        assert len(result["steps"]) >= 1
        store.close()

    def test_ladder_runs_in_order(self, tmp_path: Path):
        """Steps appear in the expected raw→rollup order."""
        store = _make_storage(tmp_path)
        result = store.enforce_size_budget(
            max_bytes=1,  # always over budget
            min_free_bytes=None,
        )
        # Steps must follow the ladder definition order.
        expected_order = ["raw_24h", "raw_12h", "raw_6h", "rollup_30d", "rollup_14d"]
        for i, step in enumerate(result["steps"]):
            assert (
                step == expected_order[i]
            ), f"Step {i} should be {expected_order[i]!r}, got {step!r}"
        store.close()

    def test_ops_events_recorded_for_each_step(self, tmp_path: Path):
        """Each ladder step produces an ops_events row with kind='db_guard'."""
        store = _make_storage(tmp_path)
        result = store.enforce_size_budget(
            max_bytes=1,
            min_free_bytes=None,
        )
        rows = store.conn.execute(
            "SELECT * FROM ops_events WHERE kind='db_guard'"
        ).fetchall()
        assert len(rows) == len(result["steps"])
        store.close()

    def test_size_before_and_after_in_result(self, tmp_path: Path):
        """Result contains size_before and size_after keys."""
        store = _make_storage(tmp_path)
        result = store.enforce_size_budget(
            max_bytes=1,
            min_free_bytes=None,
        )
        assert "size_before" in result
        assert "size_after" in result
        assert isinstance(result["size_before"], int)
        assert isinstance(result["size_after"], int)
        store.close()

    def test_min_free_bytes_trigger(self, tmp_path: Path):
        """min_free_bytes guard triggers when shutil.disk_usage reports low free."""
        store = _make_storage(tmp_path)

        class FakeDiskUsage(NamedTuple):
            total: int
            used: int
            free: int

        # Patch disk_usage to report only 10 bytes free.
        with patch("procdiag.storage.shutil.disk_usage") as mock_du:
            mock_du.return_value = FakeDiskUsage(total=1 * _GB, used=1 * _GB, free=10)
            result = store.enforce_size_budget(
                max_bytes=None,
                min_free_bytes=100 * _MB,  # require 100 MB free
            )

        assert result["triggered"] is True
        store.close()

    def test_no_trigger_when_min_free_ok(self, tmp_path: Path):
        """min_free satisfied and max_bytes=None → triggered=False."""
        store = _make_storage(tmp_path)

        class FakeDiskUsage(NamedTuple):
            total: int
            used: int
            free: int

        with patch("procdiag.storage.shutil.disk_usage") as mock_du:
            mock_du.return_value = FakeDiskUsage(
                total=10 * _GB, used=5 * _GB, free=5 * _GB
            )
            result = store.enforce_size_budget(
                max_bytes=None,
                min_free_bytes=100 * _MB,
            )

        assert result["triggered"] is False
        store.close()

    def test_both_limits_none_no_trigger(self, tmp_path: Path):
        """Both limits None → function returns triggered=False without touching DB."""
        store = _make_storage(tmp_path)
        result = store.enforce_size_budget(
            max_bytes=None,
            min_free_bytes=None,
        )
        assert result["triggered"] is False
        store.close()


# ---------------------------------------------------------------------------
# Recorder integration: _job_flush calls enforce_size_budget
# ---------------------------------------------------------------------------


class TestRecorderDbGuard:
    """Verify that Recorder._job_flush invokes enforce_size_budget."""

    def test_job_flush_calls_enforce_size_budget(self, tmp_path: Path):
        """When retention fires, enforce_size_budget is called with config values."""
        from procdiag.core.sampler import RunContext, SampleBatch, Sampler
        from procdiag.recorder import Recorder

        class FakeSampler(Sampler):
            name = "system"
            tier = "T1"

            def collect(self, ctx: RunContext) -> list[SampleBatch]:
                return []

        config = ProcdiagConfig(
            retention=RetentionConfig(
                db_max_size_mb=1024.0,
                min_free_disk_mb=256.0,
            ),
            sampling=SamplingConfig(
                system_interval=0.1,
                cgroup_interval=0.5,
                process_interval=0.5,
                disk_interval=5.0,
                dir_scan_interval=10.0,
                pss_precheck_interval=0.0,
            ),
        )
        store = Storage(tmp_path / "rec_guard.db")
        called_args: list[dict] = []

        def fake_enforce(**kwargs):
            called_args.append(kwargs)
            return {"triggered": False}

        store.enforce_size_budget = fake_enforce  # type: ignore[method-assign]

        rec = Recorder(
            config,
            db_path=tmp_path / "unused.db",
            samplers={"system": FakeSampler()},
            storage=store,
        )
        # Force retention to fire immediately.
        rec._last_retention_mono = rec._clock() - 4000.0
        rec._job_flush()

        assert len(called_args) >= 1
        call = called_args[0]
        assert "max_bytes" in call
        assert "min_free_bytes" in call
        assert call["max_bytes"] == int(1024.0 * _MB)
        assert call["min_free_bytes"] == int(256.0 * _MB)
        store.close()

    def test_job_flush_skips_enforce_when_max_size_none(self, tmp_path: Path):
        """When db_max_size_mb=None, enforce_size_budget is still called but
        max_bytes=None is passed."""
        from procdiag.core.sampler import RunContext, SampleBatch, Sampler
        from procdiag.recorder import Recorder

        class FakeSampler(Sampler):
            name = "system"
            tier = "T1"

            def collect(self, ctx: RunContext) -> list[SampleBatch]:
                return []

        config = ProcdiagConfig(
            retention=RetentionConfig(
                db_max_size_mb=None,
                min_free_disk_mb=256.0,
            ),
            sampling=SamplingConfig(
                system_interval=0.1,
                cgroup_interval=0.5,
                process_interval=0.5,
                disk_interval=5.0,
                dir_scan_interval=10.0,
                pss_precheck_interval=0.0,
            ),
        )
        store = Storage(tmp_path / "rec_guard2.db")
        called_args: list[dict] = []

        def fake_enforce(**kwargs):
            called_args.append(kwargs)
            return {"triggered": False}

        store.enforce_size_budget = fake_enforce  # type: ignore[method-assign]

        rec = Recorder(
            config,
            db_path=tmp_path / "unused.db",
            samplers={"system": FakeSampler()},
            storage=store,
        )
        rec._last_retention_mono = rec._clock() - 4000.0
        rec._job_flush()

        assert len(called_args) >= 1
        assert called_args[0]["max_bytes"] is None
        store.close()
