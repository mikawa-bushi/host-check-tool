"""DB size guard: limits the ladder cannot reach, a reader's hold, the estimate.

A free-space floor the DB cannot return does not pin retention at the bottom
rung; the hourly check does not stall the sampling for a reader; the size the
guard expects after a VACUUM is not below what the VACUUM leaves; a failed
WAL truncation is not blamed on a reader.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from test_db_guard_reclaim import (
    _DAY,
    _HOUR,
    _budget_needing_first_rung,
    _build_db,
    _copy,
    _FakeDisk,
    _on_disk,
    _span,
)

from procdiag.storage import Storage


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("guard-followups") / "mixed.db"
    _build_db(path, raw_hours=40, rollup_days=20, procs=6, rollup_procs=2)
    return path


class _ConnFailing:
    """A connection whose statements containing *needle* fail with *error*."""

    def __init__(self, conn: sqlite3.Connection, needle: str, error: Exception) -> None:
        self._conn = conn
        self._needle = needle
        self._error = error

    def execute(self, sql, *args):
        if self._needle in sql:
            raise self._error
        return self._conn.execute(sql, *args)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _failing(store: Storage, needle: str, message: str):
    real = store.conn
    store.conn = _ConnFailing(real, needle, sqlite3.OperationalError(message))  # type: ignore[assignment]
    return real


class TestAFloorTheDbCannotReturn:
    def test_retention_is_not_pinned_at_the_bottom_rung_for_it(
        self, template, tmp_path, monkeypatch
    ):
        db = _copy(template, tmp_path)
        on_disk = _on_disk(db)
        disk = _FakeDisk(db, capacity=3 * on_disk)
        monkeypatch.setattr("procdiag.storage.shutil.disk_usage", disk)

        with Storage(db) as store:
            result = store.enforce_size_budget(
                max_bytes=None, min_free_bytes=int(3.5 * on_disk)
            )

            assert result["steps"] == []
            assert _span(store, "proc_samples", "ts") / _HOUR > 39
            assert _span(store, "proc_rollup_1m", "ts_min") / _DAY > 19
            errors = store.rows("ops_events", where="kind = 'db_guard_error'")
            assert any("満たせない" in e["detail"] for e in errors)

    def test_the_size_cap_is_still_kept_while_the_floor_is_out_of_reach(
        self, template, tmp_path, monkeypatch
    ):
        db = _copy(template, tmp_path)
        on_disk = _on_disk(db)
        budget = _budget_needing_first_rung(db, tmp_path)
        disk = _FakeDisk(db, capacity=3 * on_disk)
        monkeypatch.setattr("procdiag.storage.shutil.disk_usage", disk)

        with Storage(db) as store:
            result = store.enforce_size_budget(
                max_bytes=budget, min_free_bytes=int(3.5 * on_disk)
            )

            assert result["steps"] == ["raw_24h"]
        assert _on_disk(db) <= budget


def _bottom_rung_size(db: Path, tmp_path: Path) -> int:
    """File size once the last rung of the ladder has cut and a VACUUM has run."""
    probe = tmp_path / "bottom.db"
    shutil.copy(db, probe)
    with Storage(probe) as store:
        store.retention("6h", "14d")
        store.conn.execute("VACUUM")
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = probe.stat().st_size
    probe.unlink()
    return size


class TestAFloorTheBottomRungMeets:
    @pytest.mark.parametrize("spare", [0.0, 0.02])
    def test_the_guard_cuts_until_it_is_met_when_the_estimate_says_it_is_out_of_reach(
        self, template, tmp_path, monkeypatch, spare
    ):
        db = _copy(template, tmp_path)
        capacity = 3 * _on_disk(db)
        bottom = _bottom_rung_size(db, tmp_path)
        floor = int(capacity - (1 + spare) * bottom)
        disk = _FakeDisk(db, capacity=capacity)
        monkeypatch.setattr("procdiag.storage.shutil.disk_usage", disk)

        with Storage(db) as store:
            result = store.enforce_size_budget(max_bytes=None, min_free_bytes=floor)

        assert result["steps"] != []
        assert disk.free() >= floor

    def test_the_guard_cuts_for_it_without_dbstat_too(
        self, template, tmp_path, monkeypatch
    ):
        db = _copy(template, tmp_path)
        capacity = 3 * _on_disk(db)
        bottom = _bottom_rung_size(db, tmp_path)
        floor = int(capacity - 1.02 * bottom)
        disk = _FakeDisk(db, capacity=capacity)
        monkeypatch.setattr("procdiag.storage.shutil.disk_usage", disk)

        with Storage(db) as store:
            real = _failing(store, "dbstat", "no such table: dbstat")
            try:
                result = store.enforce_size_budget(max_bytes=None, min_free_bytes=floor)
            finally:
                store.conn = real

        assert result["steps"] != []
        assert disk.free() >= floor

    def test_a_floor_even_the_estimates_margin_cannot_reach_is_still_given_up(
        self, template, tmp_path, monkeypatch
    ):
        db = _copy(template, tmp_path)
        capacity = 3 * _on_disk(db)
        bottom = _bottom_rung_size(db, tmp_path)
        disk = _FakeDisk(db, capacity=capacity)
        monkeypatch.setattr("procdiag.storage.shutil.disk_usage", disk)

        with Storage(db) as store:
            result = store.enforce_size_budget(
                max_bytes=None, min_free_bytes=int(capacity - 0.85 * bottom)
            )

            assert result["steps"] == []
            assert _span(store, "proc_samples", "ts") / _HOUR > 39


class TestReaderHoldingTheWal:
    def test_the_quiet_hourly_check_does_not_wait_for_a_reader(
        self, template, tmp_path
    ):
        db = _copy(template, tmp_path)
        with Storage(db) as store:
            reader = sqlite3.connect(db)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM proc_samples").fetchone()
            now = time.time()
            store.buffer(
                "proc_samples",
                [
                    (now + i, 1, 1000, 1.0, 5e7, None, 4, 20, None, None)
                    for i in range(3000)
                ],
            )
            store.flush()

            started = time.perf_counter()
            result = store.enforce_size_budget(max_bytes=10**12, min_free_bytes=1)
            took = time.perf_counter() - started
            reader.close()

        assert result == {"triggered": False}
        assert took < 1.5


class TestVacuumEstimate:
    @pytest.mark.parametrize(
        ("build", "keep"),
        [
            (dict(raw_hours=30, rollup_days=0, procs=10), "6h"),
            (dict(raw_hours=12, rollup_days=5, procs=3, rollup_procs=1), "6h"),
            (dict(raw_hours=40, rollup_days=20, procs=6, rollup_procs=2), "24h"),
        ],
    )
    def test_it_is_not_below_what_a_vacuum_leaves(self, tmp_path, build, keep):
        db = tmp_path / "estimate.db"
        _build_db(db, **build)
        with Storage(db) as store:
            store.retention(keep, "3650d")
            store.conn.commit()
            estimate = store._size_after_vacuum()
            store.conn.execute("VACUUM")
            store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert estimate >= db.stat().st_size

    def test_without_dbstat_it_falls_back_to_the_pages_in_use(self, template, tmp_path):
        db = _copy(template, tmp_path)
        with Storage(db) as store:
            store.retention("24h", "3650d")
            store.conn.commit()
            pages = store.conn.execute("PRAGMA page_count").fetchone()[0]
            free = store.conn.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = store.conn.execute("PRAGMA page_size").fetchone()[0]
            real = _failing(store, "dbstat", "no such table: dbstat")
            try:
                estimate = store._size_after_vacuum()
            finally:
                store.conn = real

        assert estimate == (pages - free) * page_size


class TestGuardFailures:
    def test_a_failure_between_rungs_takes_every_cut_back(self, template, tmp_path):
        db = _copy(template, tmp_path)
        budget = int(_budget_needing_first_rung(db, tmp_path) * 0.6)
        with Storage(db) as store:
            real = _failing(store, "SAVEPOINT guard_1", "disk I/O error")
            try:
                store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)
            finally:
                store.conn = real

            assert _span(store, "proc_samples", "ts") / _HOUR > 39
            errors = store.rows("ops_events", where="kind = 'db_guard_error'")
            assert any("retention 失敗" in e["detail"] for e in errors)
            assert not store.rows("ops_events", where="kind = 'db_guard'")

    def test_a_wal_that_cannot_be_truncated_is_not_blamed_on_a_reader(
        self, template, tmp_path
    ):
        db = _copy(template, tmp_path)
        budget = _budget_needing_first_rung(db, tmp_path)
        with Storage(db) as store:
            real = _failing(store, "wal_checkpoint(TRUNCATE)", "disk I/O error")
            try:
                store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)
            finally:
                store.conn = real

            (event,) = store.rows("ops_events", where="kind = 'db_guard_reclaim'")
        assert "読み取り中" not in event["detail"]
        assert "disk I/O error" in event["detail"]


class TestHoldSlack:
    def test_a_ladder_that_cannot_vacuum_keeps_a_share_of_the_file_reusable(
        self, template, tmp_path, monkeypatch
    ):
        db = _copy(template, tmp_path)
        with Storage(db) as store:
            store.conn.execute("VACUUM")
            store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        on_disk = _on_disk(db)
        disk = _FakeDisk(db, capacity=int(1.5 * on_disk))
        monkeypatch.setattr("procdiag.storage.shutil.disk_usage", disk)

        with Storage(db) as store:
            result = store.enforce_size_budget(
                max_bytes=None, min_free_bytes=int(disk.free() + 0.5 * on_disk)
            )
            free_pages = store.conn.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = store.conn.execute("PRAGMA page_size").fetchone()[0]

        assert result["vacuumed"] is False
        share = free_pages * page_size / db.stat().st_size
        assert 0.05 <= share <= 0.40
