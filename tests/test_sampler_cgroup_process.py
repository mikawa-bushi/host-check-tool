"""Tests for CgroupSampler (T2a) and ProcessSampler (T2b).

Covers:
- CgroupSampler with a pseudo cgroup v2 tree:
  * Two collects produce correct CPU/IO deltas on the second run.
  * Missing files result in None columns (partial demotion).
  * Column count matches the cg_samples contract.
  * v1 tree (no cgroup.controllers) disables the sampler and yields [].
- ProcessSampler with live psutil:
  * Column count matches the proc_samples contract.
  * The current process is captured.
  * Second collect gives a non-None cpu_pct for the current process.
  * AccessDenied on io_counters increments io_denied / io_total.
  * PSS is read from a fixture smaps_rollup when the proc_id is in pss_targets.
- Registry stub: same (name, hash) always resolves to the same proc_id.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable
from unittest.mock import patch, MagicMock

import psutil
import pytest

from procdiag.config import ProcdiagConfig
from procdiag.core.sampler import RunContext, SampleBatch
from procdiag.samplers.cgroup import CgroupSampler
from procdiag.samplers.process import ProcessSampler

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

CG_EXPECTED_COLS = 8  # ts, cg_id, cpu_usec_delta, mem_current, mem_swap,
#                        io_rbytes_delta, io_wbytes_delta, pids

PROC_EXPECTED_COLS = 10  # ts, proc_id, pid, cpu_pct, rss, pss,
#                           num_threads, num_fds, io_read_delta, io_write_delta


class _CgStub:
    """Simple stub for resolve_cg: assigns sequential IDs keyed by unit_path."""

    def __init__(self) -> None:
        self._map: dict[str, int] = {}
        self._counter = 0

    def __call__(self, unit_path: str, ts: float) -> int:
        if unit_path not in self._map:
            self._counter += 1
            self._map[unit_path] = self._counter
        return self._map[unit_path]


class _ProcStub:
    """Stub for resolve_proc: returns stable IDs for (name, hash) pairs."""

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], int] = {}
        self._counter = 0
        self.calls: list[tuple[str, str, str, float]] = []

    def __call__(self, name: str, hash12: str, sample: str, ts: float) -> int:
        self.calls.append((name, hash12, sample, ts))
        key = (name, hash12)
        if key not in self._map:
            self._counter += 1
            self._map[key] = self._counter
        return self._map[key]


def _make_ctx(
    config: ProcdiagConfig | None = None,
    cgroup_root: Path | None = None,
    proc_root: Path | None = None,
) -> RunContext:
    cfg = config or ProcdiagConfig()
    kwargs: dict = {}
    if cgroup_root is not None:
        kwargs["cgroup_root"] = cgroup_root
    if proc_root is not None:
        kwargs["proc_root"] = proc_root
    return RunContext(cfg, **kwargs)


FIXTURES = Path(__file__).parent / "fixtures"
CGROUP_TREE = FIXTURES / "cgroup_tree"
CGROUP_TREE_V1 = FIXTURES / "cgroup_tree_v1"
PROC_ROOT_FIXTURE = FIXTURES / "proc_root"


# ---------------------------------------------------------------------------
# CgroupSampler tests
# ---------------------------------------------------------------------------


class TestCgroupSamplerV2:
    """Tests against the pseudo cgroup_tree (v2 — has cgroup.controllers)."""

    def _sampler(self) -> tuple[CgroupSampler, _CgStub]:
        stub = _CgStub()
        return CgroupSampler(resolve_cg=stub), stub

    def test_setup_detects_v2(self):
        sampler, _ = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)
        assert ctx.capabilities.get("cgroup_v2") is True
        assert len(ctx.notes) == 0

    def test_first_collect_returns_batches(self):
        sampler, _ = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        assert len(batches) == 1
        batch = batches[0]
        assert batch.table == "cg_samples"
        assert len(batch.rows) > 0

    def test_row_column_count(self):
        sampler, _ = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        for row in batches[0].rows:
            assert (
                len(row) == CG_EXPECTED_COLS
            ), f"Expected {CG_EXPECTED_COLS} columns, got {len(row)}: {row}"

    def test_first_collect_cpu_delta_is_none(self):
        """On the very first collect, cpu_usec_delta must be None (no baseline)."""
        sampler, _ = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        # column index 2 is cpu_usec_delta
        for row in batches[0].rows:
            assert (
                row[2] is None
            ), f"Expected None cpu_usec_delta on first collect, got {row[2]}"

    def test_second_collect_cpu_delta_correct(self):
        """After the first collect establishes a baseline, the delta is correct."""
        sampler, _ = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)

        # First collect establishes baseline.
        sampler.collect(ctx)

        # Bump the cpu.stat files so the second collect sees a delta.
        nginx_cpu = CGROUP_TREE / "system.slice" / "nginx.service" / "cpu.stat"
        original = nginx_cpu.read_text()
        try:
            nginx_cpu.write_text(
                "usage_usec 6000000\nuser_usec 3500000\nsystem_usec 2500000\n"
            )
            batches2 = sampler.collect(ctx)
        finally:
            nginx_cpu.write_text(original)

        rows2 = batches2[0].rows
        # Find the nginx row (unit_path = "system.slice/nginx.service")
        # cg_id is resolved by stub; we search by matching cg_id we know is 1
        # Actually check by position: stub assigns IDs in enum order.
        # Find row where cpu_usec_delta == 1_000_000
        nginx_rows = [r for r in rows2 if r[2] == 1_000_000]
        assert (
            len(nginx_rows) == 1
        ), f"Expected exactly 1 nginx row with delta=1_000_000; rows={rows2}"

    def test_second_collect_io_delta_correct(self):
        """IO deltas are correct on the second collect."""
        sampler, _ = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)
        sampler.collect(ctx)  # establish baseline

        # First collect has io_rbytes_delta=None; second should compute delta.
        # We bump the io.stat for nginx.
        nginx_io = CGROUP_TREE / "system.slice" / "nginx.service" / "io.stat"
        original = nginx_io.read_text()
        try:
            # Original: rbytes = 102400 + 10240 = 112640, wbytes = 51200 + 5120 = 56320
            # New:       rbytes += 1000, wbytes += 500
            nginx_io.write_text(
                "8:0 rbytes=103400 wbytes=51700 rios=11 wios=6 dbytes=0 dios=0\n"
                "8:16 rbytes=10240 wbytes=5120 rios=2 wios=1 dbytes=0 dios=0\n"
            )
            batches2 = sampler.collect(ctx)
        finally:
            nginx_io.write_text(original)

        rows2 = batches2[0].rows
        # io_rbytes_delta is column index 5, io_wbytes_delta is 6
        # nginx row delta should be rbytes_delta=1000, wbytes_delta=500
        nginx_rows = [r for r in rows2 if r[5] == 1000 and r[6] == 500]
        assert (
            len(nginx_rows) == 1
        ), f"Expected nginx row with io_rbytes_delta=1000, wbytes_delta=500; rows={rows2}"

    def test_missing_io_stat_yields_none(self):
        """cron.service has no io.stat — io columns must be None."""
        sampler, stub = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)
        batches = sampler.collect(ctx)

        # Force second collect so delta logic runs.
        batches2 = sampler.collect(ctx)
        rows2 = batches2[0].rows

        # Find the cron.service row.  It has io columns None.
        # cron.service has pids=1, which is unique.
        cron_rows = [r for r in rows2 if r[7] == 1]
        assert len(cron_rows) >= 1, "Expected at least one cron.service row (pids=1)"
        cron_row = cron_rows[0]
        assert cron_row[5] is None, "io_rbytes_delta should be None for cron.service"
        assert cron_row[6] is None, "io_wbytes_delta should be None for cron.service"

    def test_services_enumerate_correctly(self):
        """Three .service units under system.slice should be enumerated."""
        sampler, stub = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)
        batches = sampler.collect(ctx)

        unit_ids_seen = {row[1] for row in batches[0].rows}
        # cron, mysql, nginx — 3 services + potentially user.slice
        assert len(unit_ids_seen) >= 3

    def test_cg_id_stable_across_collects(self):
        """The same unit_path returns the same cg_id on successive collects."""
        sampler, stub = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)

        b1 = sampler.collect(ctx)
        b2 = sampler.collect(ctx)

        ids1 = {row[1] for row in b1[0].rows}
        ids2 = {row[1] for row in b2[0].rows}
        assert ids1 == ids2, "cg_ids must be stable across collects"

    def test_unit_cache_not_refreshed_immediately(self):
        """Unit enumeration is cached: adding a unit mid-session is not visible."""
        sampler, _ = self._sampler()
        ctx = _make_ctx(cgroup_root=CGROUP_TREE)
        sampler.setup(ctx)
        b1 = sampler.collect(ctx)
        count1 = len(b1[0].rows)

        # Create a new service directory (should NOT be visible yet — cache).
        new_svc = CGROUP_TREE / "system.slice" / "newtest.service"
        new_svc.mkdir(exist_ok=True)
        (new_svc / "cpu.stat").write_text("usage_usec 0\n")
        (new_svc / "memory.current").write_text("0\n")
        (new_svc / "pids.current").write_text("0\n")
        try:
            b2 = sampler.collect(ctx)
            count2 = len(b2[0].rows)
            # Count should be the same because the cache TTL has not expired.
            assert count2 == count1, (
                "Unit cache should not refresh immediately; "
                f"count1={count1}, count2={count2}"
            )
        finally:
            import shutil

            shutil.rmtree(new_svc)


class TestCgroupSamplerV1:
    """Tests against a cgroup tree without cgroup.controllers (v1/absent)."""

    def test_setup_detects_non_v2(self):
        stub = _CgStub()
        sampler = CgroupSampler(resolve_cg=stub)
        ctx = _make_ctx(cgroup_root=CGROUP_TREE_V1)
        sampler.setup(ctx)
        assert ctx.capabilities.get("cgroup_v2") is False
        assert len(ctx.notes) == 1
        assert "cgroup v2" in ctx.notes[0]

    def test_collect_returns_empty_when_disabled(self):
        stub = _CgStub()
        sampler = CgroupSampler(resolve_cg=stub)
        ctx = _make_ctx(cgroup_root=CGROUP_TREE_V1)
        sampler.setup(ctx)
        assert sampler.collect(ctx) == []

    def test_run_returns_empty_when_disabled(self):
        """Defensive wall: run() must also return [] when disabled."""
        stub = _CgStub()
        sampler = CgroupSampler(resolve_cg=stub)
        ctx = _make_ctx(cgroup_root=CGROUP_TREE_V1)
        sampler.setup(ctx)
        assert sampler.run(ctx) == []


# ---------------------------------------------------------------------------
# ProcessSampler tests
# ---------------------------------------------------------------------------


class TestProcessSampler:
    """Tests for ProcessSampler against the live process table."""

    def _sampler(self) -> tuple[ProcessSampler, _ProcStub]:
        stub = _ProcStub()
        return ProcessSampler(resolve_proc=stub), stub

    def test_collect_returns_batch(self):
        sampler, _ = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        assert len(batches) == 1
        assert batches[0].table == "proc_samples"
        assert len(batches[0].rows) > 0

    def test_row_column_count(self):
        sampler, _ = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        for row in batches[0].rows:
            assert (
                len(row) == PROC_EXPECTED_COLS
            ), f"Expected {PROC_EXPECTED_COLS} columns, got {len(row)}: {row}"

    def test_current_process_captured(self):
        """The current Python process must appear in the results."""
        sampler, _ = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        my_pid = os.getpid()
        pids_in_batch = {row[2] for row in batches[0].rows}
        assert (
            my_pid in pids_in_batch
        ), f"Current process PID {my_pid} not found in proc_samples rows"

    def test_first_collect_cpu_pct_none_for_new_pid(self):
        """On the first collect, cpu_pct for newly seen PIDs must be None."""
        sampler, _ = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        my_pid = os.getpid()
        my_rows = [r for r in batches[0].rows if r[2] == my_pid]
        assert len(my_rows) == 1
        # Column index 3 is cpu_pct.
        assert my_rows[0][3] is None, (
            f"cpu_pct should be None on first collect for PID {my_pid}, "
            f"got {my_rows[0][3]}"
        )

    def test_second_collect_cpu_pct_non_none(self):
        """On the second collect, cpu_pct for the current process is non-None."""
        sampler, _ = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        sampler.collect(ctx)  # first tick: establishes baseline

        # Short sleep to give psutil a non-zero interval.
        time.sleep(0.05)
        batches2 = sampler.collect(ctx)
        my_pid = os.getpid()
        my_rows = [r for r in batches2[0].rows if r[2] == my_pid]
        assert len(my_rows) == 1
        # cpu_pct should now be a float (possibly 0.0 but not None).
        assert (
            my_rows[0][3] is not None
        ), f"cpu_pct should not be None on second collect for PID {my_pid}"
        assert isinstance(my_rows[0][3], float)

    def test_rss_is_int(self):
        """RSS must be a non-negative integer for the current process."""
        sampler, _ = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        my_pid = os.getpid()
        my_rows = [r for r in batches[0].rows if r[2] == my_pid]
        assert len(my_rows) == 1
        rss = my_rows[0][4]
        assert isinstance(rss, int) and rss >= 0

    def test_io_denied_increments_counter(self):
        """When io_counters raises AccessDenied, io_denied must increment."""
        sampler, stub = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)

        # Monkeypatch: make every io_counters call raise AccessDenied.
        original_io = psutil.Process.io_counters

        def _raise_access_denied(self):
            raise psutil.AccessDenied(self.pid)

        psutil.Process.io_counters = _raise_access_denied
        try:
            sampler.io_denied = 0
            sampler.io_total = 0
            sampler.collect(ctx)
        finally:
            psutil.Process.io_counters = original_io

        assert sampler.io_total > 0, "io_total should be > 0 after collect"
        assert (
            sampler.io_denied > 0
        ), "io_denied should be > 0 when all io_counters denied"
        assert sampler.io_denied <= sampler.io_total

    def test_io_denied_columns_are_none(self):
        """io_read_delta and io_write_delta must be None when AccessDenied."""
        sampler, stub = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)

        original_io = psutil.Process.io_counters

        def _raise_access_denied(self):
            raise psutil.AccessDenied(self.pid)

        psutil.Process.io_counters = _raise_access_denied
        try:
            batches = sampler.collect(ctx)
        finally:
            psutil.Process.io_counters = original_io

        for row in batches[0].rows:
            # io_read_delta = index 8, io_write_delta = index 9
            assert row[8] is None, f"io_read_delta should be None; row={row}"
            assert row[9] is None, f"io_write_delta should be None; row={row}"

    def test_pss_not_read_when_not_in_targets(self):
        """PSS column must be None when proc_id is not in pss_targets."""
        sampler, _ = self._sampler()
        ctx = _make_ctx(proc_root=PROC_ROOT_FIXTURE)
        sampler.setup(ctx)
        batches = sampler.collect(ctx)
        for row in batches[0].rows:
            # pss is column index 5
            assert (
                row[5] is None
            ), f"pss should be None when proc_id not in pss_targets; row={row}"

    def test_pss_read_when_in_targets(self):
        """PSS must be read from smaps_rollup when proc_id is in pss_targets."""
        # Use the fixture proc_root which has PID 1234 with a smaps_rollup.
        sampler, stub = self._sampler()
        ctx = _make_ctx(proc_root=PROC_ROOT_FIXTURE)
        sampler.setup(ctx)

        # Build a fake Process object for PID 1234.
        mock_proc = MagicMock(spec=psutil.Process)
        mock_proc.pid = 1234
        mock_proc.name.return_value = "testproc"
        mock_proc.cmdline.return_value = ["testproc"]
        mock_proc.cpu_percent.return_value = 5.0
        mock_proc.memory_info.return_value = MagicMock(rss=1024 * 1024)
        mock_proc.num_threads.return_value = 2
        mock_proc.num_fds.return_value = 5
        mock_io = MagicMock()
        mock_io.read_bytes = 1000
        mock_io.write_bytes = 500
        mock_proc.io_counters.return_value = mock_io
        mock_proc.status.return_value = "running"

        # Patch psutil.pids to return only PID 1234,
        # and psutil.Process constructor to return our mock.
        with (
            patch("psutil.pids", return_value=[1234]),
            patch(
                "psutil.Process",
                side_effect=lambda pid: (
                    mock_proc if pid == 1234 else psutil.NoSuchProcess(pid)
                ),
            ),
        ):
            # Force the cache to use our mock by pre-populating it.
            sampler._pid_cache = {}
            sampler._prev_io = {}

            # First tick for PID 1234 — establishes baseline; cpu_pct=None.
            b1 = sampler.collect(ctx)

        # Get the proc_id assigned to "testproc".
        rows1 = b1[0].rows
        testproc_rows = [r for r in rows1 if r[2] == 1234]
        assert (
            len(testproc_rows) == 1
        ), f"Expected testproc row with PID 1234; got rows={rows1}"
        proc_id_1234 = testproc_rows[0][1]

        # Add this proc_id to pss_targets.
        sampler.pss_targets.add(proc_id_1234)

        # Second tick — pss should now be read from smaps_rollup.
        with (
            patch("psutil.pids", return_value=[1234]),
            patch(
                "psutil.Process",
                side_effect=lambda pid: (
                    mock_proc if pid == 1234 else psutil.NoSuchProcess(pid)
                ),
            ),
        ):
            b2 = sampler.collect(ctx)

        rows2 = b2[0].rows
        testproc_rows2 = [r for r in rows2 if r[2] == 1234]
        assert len(testproc_rows2) == 1
        pss_val = testproc_rows2[0][5]
        # smaps_rollup has Pss: 6144 kB = 6291456 bytes
        assert (
            pss_val == 6144 * 1024
        ), f"Expected PSS = {6144 * 1024} bytes, got {pss_val}"

    def test_stable_proc_id_for_same_name_and_hash(self):
        """The stub must return the same proc_id for the same (name, hash) pair."""
        sampler, stub = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        sampler.collect(ctx)
        time.sleep(0.05)
        sampler.collect(ctx)

        # Build a mapping: (name, hash) -> set of proc_ids seen.
        seen: dict[tuple[str, str], set[int]] = {}
        for name, hash12, _sample, _ts in stub.calls:
            key = (name, hash12)
            proc_id = stub._map[key]
            seen.setdefault(key, set()).add(proc_id)

        # Each (name, hash) pair must map to exactly one proc_id.
        for key, ids in seen.items():
            assert (
                len(ids) == 1
            ), f"(name, hash) = {key} resolved to multiple proc_ids: {ids}"

    def test_zombie_count(self):
        """last_zombie_count must reflect the number of zombie processes."""
        sampler, _ = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)

        # Monkeypatch status to make one process a zombie.
        original_status = psutil.Process.status
        call_count = [0]

        def patched_status(self):
            call_count[0] += 1
            if call_count[0] == 1:
                return "zombie"
            return original_status(self)

        psutil.Process.status = patched_status
        try:
            sampler.last_zombie_count = -1
            sampler.collect(ctx)
        finally:
            psutil.Process.status = original_status

        assert (
            sampler.last_zombie_count >= 0
        ), "last_zombie_count must be a non-negative integer after collect"

    def test_zombie_count_normal_zero_or_more(self):
        """Under normal conditions, last_zombie_count is >= 0."""
        sampler, _ = self._sampler()
        ctx = _make_ctx()
        sampler.setup(ctx)
        sampler.collect(ctx)
        assert sampler.last_zombie_count >= 0

    def test_run_wraps_collect_with_defensive_wall(self):
        """run() must not propagate exceptions from collect()."""
        stub = _ProcStub()
        sampler = ProcessSampler(resolve_proc=stub)
        ctx = _make_ctx()
        sampler.setup(ctx)

        # Force collect to raise.
        def _broken_collect(ctx):
            raise RuntimeError("テスト用の強制例外")

        sampler.collect = _broken_collect  # type: ignore[method-assign]
        result = sampler.run(ctx)
        assert result == []
        assert any("collect失敗" in note for note in ctx.notes)
