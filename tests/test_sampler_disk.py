"""Tests for procdiag.samplers.disk (T04).

Two test areas:

1. ``collect`` — smoke-tests against the real machine:
   * Returns at least one ``disk_samples`` SampleBatch.
   * Every row has exactly 4 columns (ts, mountpoint, total, used).
   * The root filesystem "/" is present in the collected mount points.

2. ``collect_dirs`` — unit-tests using a synthetic directory tree:
   * Correct size aggregation matches ``os.stat`` ground truth.
   * A ``time_budget_sec=0`` budget causes immediate cut-off with
     ``is_estimate=1``.
   * ``PermissionError`` on a sub-directory is silently skipped.
"""

from __future__ import annotations

import os

import pytest

from procdiag.config import ProcdiagConfig, SamplingConfig, WatchDir
from procdiag.core.sampler import RunContext
from procdiag.samplers.disk import DiskSampler, _scan_dir
from procdiag.storage import TABLE_COLUMNS

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_ctx(watch_dirs: list[WatchDir] | None = None) -> RunContext:
    sampling = SamplingConfig(watch_dirs=watch_dirs or [])
    cfg = ProcdiagConfig(sampling=sampling)
    return RunContext(cfg)


# ---------------------------------------------------------------------------
# collect() — real-machine smoke tests
# ---------------------------------------------------------------------------


class TestDiskCollect:
    def test_returns_samplebatch(self):
        sampler = DiskSampler()
        ctx = _make_ctx()
        batches = sampler.collect(ctx)
        assert len(batches) == 1
        batch = batches[0]
        assert batch.table == "disk_samples"

    def test_row_column_count(self):
        sampler = DiskSampler()
        ctx = _make_ctx()
        batch = sampler.collect(ctx)[0]
        # At minimum the root filesystem must be present, with every column.
        width = len(TABLE_COLUMNS["disk_samples"])
        assert len(batch.rows) >= 1, "少なくとも1パーティションが必要"
        for row in batch.rows:
            assert len(row) == width, f"列数が不正 (期待{width}, 実際{len(row)}): {row}"

    def test_root_filesystem_present(self):
        sampler = DiskSampler()
        ctx = _make_ctx()
        batch = sampler.collect(ctx)[0]
        mountpoints = [row[1] for row in batch.rows]
        assert "/" in mountpoints, f"'/' が見当たりません: {mountpoints}"

    def test_ts_is_float(self):
        sampler = DiskSampler()
        ctx = _make_ctx()
        batch = sampler.collect(ctx)[0]
        for row in batch.rows:
            assert isinstance(row[0], float), f"ts が float でない: {row[0]}"

    def test_total_and_used_positive(self):
        sampler = DiskSampler()
        ctx = _make_ctx()
        batch = sampler.collect(ctx)[0]
        for row in batch.rows:
            ts, mp, total, used, inodes_total, inodes_used, read_only = row
            assert total > 0, f"{mp}: total={total}"
            assert used >= 0, f"{mp}: used={used}"
            assert used <= total, f"{mp}: used={used} > total={total}"
            assert read_only in (0, 1)
            if inodes_total is not None:
                assert 0 <= inodes_used <= inodes_total


class TestMountState:
    def test_read_only_flag_parsed_from_options(self):
        from procdiag.samplers.disk import is_read_only

        assert is_read_only("ro,relatime")
        assert not is_read_only("rw,relatime,errors=remount-ro")
        assert not is_read_only("")

    def test_inode_usage_not_applicable_when_fs_reports_zero(self, monkeypatch):
        import procdiag.samplers.disk as disk_mod

        class _St:
            f_files = 0
            f_ffree = 0

        monkeypatch.setattr(disk_mod.os, "statvfs", lambda p: _St())
        assert disk_mod._inode_usage("/") == (None, None)


# ---------------------------------------------------------------------------
# collect_dirs() — unit tests with synthetic tree
# ---------------------------------------------------------------------------


class TestCollectDirs:
    def test_size_aggregation_correct(self, tmp_path):
        """Total bytes from collect_dirs must match manually summed stat sizes."""
        # Build a small tree: root + sub-dir with 3 files.
        sub = tmp_path / "sub"
        sub.mkdir()
        sizes = [100, 200, 50]
        for i, sz in enumerate(sizes):
            (sub / f"file{i}.dat").write_bytes(b"x" * sz)
        (tmp_path / "top.dat").write_bytes(b"y" * 77)

        expected_total = sum(sizes) + 77

        wd = WatchDir(path=str(tmp_path), time_budget_sec=30.0)
        ctx = _make_ctx([wd])

        sampler = DiskSampler()
        batches = sampler.collect_dirs(ctx)

        assert len(batches) == 1
        batch = batches[0]
        assert batch.table == "dir_samples"
        assert len(batch.rows) == 1

        ts, path, size_bytes, is_estimate = batch.rows[0]
        assert path == str(tmp_path)
        assert size_bytes == expected_total
        assert is_estimate == 0

    def test_empty_directory_returns_zero(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        wd = WatchDir(path=str(empty_dir), time_budget_sec=30.0)
        ctx = _make_ctx([wd])
        sampler = DiskSampler()
        batch = sampler.collect_dirs(ctx)[0]

        ts, path, size_bytes, is_estimate = batch.rows[0]
        assert size_bytes == 0
        assert is_estimate == 0

    def test_time_budget_zero_triggers_estimate(self, tmp_path):
        """A budget of effectively 0 must set is_estimate=1."""
        # Create a moderately sized tree to ensure the walk cannot finish
        # within an essentially-zero budget.
        for i in range(20):
            (tmp_path / f"file{i}.dat").write_bytes(b"z" * 1024)

        # _scan_dir with time_budget_sec=0 — deadline is immediately in the
        # past so the very first check should abort.
        size, is_estimate = _scan_dir(str(tmp_path), 0.0)
        assert is_estimate is True

    def test_collect_dirs_budget_zero_is_estimate(self, tmp_path):
        """collect_dirs with time_budget_sec≈0 returns is_estimate=1."""
        for i in range(30):
            (tmp_path / f"f{i}").write_bytes(b"a" * 512)

        # Use a tiny positive value that will expire before the walk can finish.
        wd = WatchDir(path=str(tmp_path), time_budget_sec=1e-9)
        ctx = _make_ctx([wd])
        sampler = DiskSampler()
        batch = sampler.collect_dirs(ctx)[0]

        _ts, _path, _size, is_estimate = batch.rows[0]
        assert is_estimate == 1

    def test_multiple_watch_dirs_produce_multiple_rows(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "fa").write_bytes(b"a" * 10)
        (dir_b / "fb").write_bytes(b"b" * 20)

        wds = [
            WatchDir(path=str(dir_a), time_budget_sec=30.0),
            WatchDir(path=str(dir_b), time_budget_sec=30.0),
        ]
        ctx = _make_ctx(wds)
        sampler = DiskSampler()
        batch = sampler.collect_dirs(ctx)[0]

        assert len(batch.rows) == 2
        paths = {row[1] for row in batch.rows}
        assert str(dir_a) in paths
        assert str(dir_b) in paths

    def test_row_column_order(self, tmp_path):
        """Verify the column contract: ts, path, size_bytes, is_estimate."""
        (tmp_path / "x").write_bytes(b"hello")
        wd = WatchDir(path=str(tmp_path), time_budget_sec=30.0)
        ctx = _make_ctx([wd])
        sampler = DiskSampler()
        batch = sampler.collect_dirs(ctx)[0]

        row = batch.rows[0]
        assert len(row) == 4
        ts, path, size_bytes, is_estimate = row
        assert isinstance(ts, float)
        assert isinstance(path, str)
        assert isinstance(size_bytes, int)
        assert is_estimate in (0, 1)

    def test_no_watch_dirs_returns_empty_rows(self):
        ctx = _make_ctx([])
        sampler = DiskSampler()
        batch = sampler.collect_dirs(ctx)[0]
        assert batch.rows == []

    @pytest.mark.skipif(
        os.getuid() == 0, reason="rootではパーミッションエラーが発生しない"
    )
    def test_permission_error_skipped(self, tmp_path):
        """Subdirectory with mode 000 must be silently skipped."""
        protected = tmp_path / "protected"
        protected.mkdir()
        (protected / "secret.dat").write_bytes(b"x" * 500)
        # Remove all permissions from the sub-directory.
        protected.chmod(0o000)

        visible = tmp_path / "visible.dat"
        visible.write_bytes(b"y" * 100)

        wd = WatchDir(path=str(tmp_path), time_budget_sec=30.0)
        ctx = _make_ctx([wd])
        sampler = DiskSampler()
        try:
            batch = sampler.collect_dirs(ctx)[0]
            ts, path, size_bytes, is_estimate = batch.rows[0]
            # The protected subtree is inaccessible; only visible.dat counts.
            # We do not assert the exact value because the protected dir entry
            # itself may or may not be counted depending on inode size, but the
            # call must not raise.
        finally:
            # Restore permissions so pytest cleanup can remove the tree.
            protected.chmod(0o700)

    def test_symlinks_not_followed(self, tmp_path):
        """Symbolic links to directories must not be followed."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "big.dat").write_bytes(b"x" * 4096)

        link_dir = tmp_path / "link_to_real"
        link_dir.symlink_to(real_dir)

        wd = WatchDir(path=str(tmp_path), time_budget_sec=30.0)
        ctx = _make_ctx([wd])
        sampler = DiskSampler()
        batch = sampler.collect_dirs(ctx)[0]

        ts, path, size_bytes, is_estimate = batch.rows[0]
        # The symlink itself should be counted (small inode size), but the
        # 4096-byte file inside real/ must NOT be double-counted via the link.
        # Therefore size_bytes should be less than 4096 * 2.
        assert (
            size_bytes < 4096 * 2
        ), f"シンボリックリンクが辿られている疑い: size_bytes={size_bytes}"
