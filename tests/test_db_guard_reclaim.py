"""DB size guard: stop at the first sufficient rung and really return the space.

Covers:
- the ladder stops as soon as the DB would fit, instead of always cutting to
  the bottom rung (a DELETE alone never shrinks the file or the -wal)
- the configured rollup retention is never cut below what the user asked for
- the -wal is truncated after the guard, so the guard does not re-fire
- a disk-low trigger is satisfied by the space the guard actually returns
- on a nearly full disk the guard skips (or survives a failing) VACUUM
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

from procdiag.config import ProcdiagConfig, RetentionConfig
from procdiag.recorder import Recorder
from procdiag.storage import Storage

_MiB = 1024 * 1024
_HOUR = 3600.0
_DAY = 86400.0


def _on_disk(db: Path) -> int:
    wal = Path(str(db) + "-wal")
    return db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)


def _build_db(
    path: Path,
    *,
    raw_hours: float,
    rollup_days: float,
    procs: int,
    rollup_procs: int = 1,
    newest_first: bool = False,
) -> None:
    """Write a recorder-shaped DB: 10 s raw proc samples, 1 min proc rollups.

    ``newest_first`` inserts in descending time order, which leaves the b-tree
    pages half full: after a DELETE the pages in use far overstate what a
    VACUUM leaves.
    """
    now = int(time.time() / 60) * 60.0
    with Storage(path) as store:
        raw = []
        t = now - raw_hours * _HOUR
        while t < now:
            for p in range(procs):
                raw.append((t, p, 1000 + p, 1.0, 50_000_000, None, 4, 20, None, None))
            t += 10.0
        raw.sort(reverse=newest_first)
        store.buffer("proc_samples", raw)
        rollup = []
        t = now - rollup_days * _DAY
        while t < now:
            for p in range(rollup_procs):
                rollup.append((t, p, 1.0, 2.0, 50_000_000, 60_000_000, 20, 4))
            t += 60.0
        rollup.sort(reverse=newest_first)
        store.buffer("proc_rollup_1m", rollup)
        store.flush(reason="synth")
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


@pytest.fixture(scope="module")
def mixed_template(tmp_path_factory) -> Path:
    """40 h of raw samples plus 20 days of rollups, built once per module."""
    path = tmp_path_factory.mktemp("guard-template") / "mixed.db"
    _build_db(path, raw_hours=40, rollup_days=20, procs=6, rollup_procs=2)
    return path


@pytest.fixture(scope="module")
def raw_template(tmp_path_factory) -> Path:
    """30 h of raw samples only, built once per module."""
    path = tmp_path_factory.mktemp("guard-template") / "raw.db"
    _build_db(path, raw_hours=30, rollup_days=0, procs=10)
    return path


def _copy(template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "guard.db"
    shutil.copy(template, db)
    return db


def _vacuumed_size(db: Path, tmp_path: Path, raw: Optional[str] = None) -> int:
    """File size after an optional cut of raw samples to *raw* and a VACUUM."""
    probe = tmp_path / "probe.db"
    shutil.copy(db, probe)
    with Storage(probe) as store:
        if raw is not None:
            store.retention(raw, "3650d")
        store.conn.execute("VACUUM")
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = probe.stat().st_size
    probe.unlink()
    return size


def _budget_needing_first_rung(db: Path, tmp_path: Path) -> int:
    """A budget the DB exceeds even vacuumed, that cutting raw to 24 h meets."""
    full = _vacuumed_size(db, tmp_path)
    cut = _vacuumed_size(db, tmp_path, "24h")
    return int(cut + 0.5 * (full - cut))


def _span(store: Storage, table: str, column: str) -> float:
    lo = store.conn.execute(f"SELECT MIN({column}) FROM {table}").fetchone()[0]
    return time.time() - lo


class _Usage(NamedTuple):
    total: int
    used: int
    free: int


class _FakeDisk:
    """``shutil.disk_usage`` for a disk whose only tenant is one DB."""

    def __init__(self, db: Path, capacity: int) -> None:
        self.db = db
        self.capacity = capacity

    def free(self) -> int:
        return self.capacity - _on_disk(self.db)

    def __call__(self, _path) -> _Usage:
        return _Usage(self.capacity, _on_disk(self.db), self.free())


class _VacuumFails:
    """A connection whose VACUUM fails the way a full disk makes it fail."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql, *args):
        if sql.strip().upper() == "VACUUM":
            raise sqlite3.OperationalError("database or disk is full")
        return self._conn.execute(sql, *args)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestSizeLimitLadder:
    def test_stops_at_first_rung_that_fits(self, mixed_template, tmp_path):
        db = _copy(mixed_template, tmp_path)
        budget = _budget_needing_first_rung(db, tmp_path)

        with Storage(db) as store:
            result = store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)

            assert result["steps"] == ["raw_24h"]
            assert _span(store, "proc_samples", "ts") / _HOUR == pytest.approx(
                24, abs=1
            )
            assert _span(store, "proc_rollup_1m", "ts_min") / _DAY > 19
        assert _on_disk(db) <= budget

    def test_half_empty_pages_do_not_cost_a_rung(self, tmp_path):
        db = tmp_path / "desc.db"
        _build_db(
            db,
            raw_hours=40,
            rollup_days=20,
            procs=6,
            rollup_procs=2,
            newest_first=True,
        )
        budget = _budget_needing_first_rung(db, tmp_path)

        with Storage(db) as store:
            try:
                store.conn.execute("SELECT 1 FROM dbstat LIMIT 1")
            except sqlite3.OperationalError:
                pytest.skip("this SQLite has no dbstat table")
            result = store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)

            assert result["steps"] == ["raw_24h"]
        assert _on_disk(db) <= budget

    def test_freed_pages_alone_are_returned_without_cutting_retention(
        self, mixed_template, tmp_path
    ):
        db = _copy(mixed_template, tmp_path)
        budget = _budget_needing_first_rung(db, tmp_path)

        with Storage(db) as store:
            store.retention("24h", "3650d")
            store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            assert db.stat().st_size > budget

            result = store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)

            assert result["steps"] == []
            assert result["vacuumed"] is True
            assert _span(store, "proc_samples", "ts") / _HOUR == pytest.approx(
                24, abs=1
            )
        assert _on_disk(db) <= budget

    def test_three_percent_over_budget_keeps_most_of_the_retention(
        self, mixed_template, tmp_path
    ):
        db = _copy(mixed_template, tmp_path)
        budget = int(_vacuumed_size(db, tmp_path) * 0.97)

        with Storage(db) as store:
            result = store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)

            assert result["steps"] == ["raw_24h"]
            assert _span(store, "proc_samples", "ts") / _HOUR > 20
        assert _on_disk(db) <= budget

    def test_configured_rollup_retention_is_not_cut_by_raw_rungs(self, tmp_path):
        db = tmp_path / "long.db"
        _build_db(db, raw_hours=40, rollup_days=120, procs=6, rollup_procs=1)
        config = ProcdiagConfig(
            retention=RetentionConfig(
                raw="48h",
                rollup="365d",
                db_max_size_mb=_budget_needing_first_rung(db, tmp_path) / _MiB,
                min_free_disk_mb=1.0,
            )
        )
        with Storage(db) as store:
            rec = Recorder(
                config, storage=store, samplers={}, trigger=object(), selfmeter=object()
            )
            rec._last_retention_mono = rec._clock() - 4000.0
            rec._job_flush()

            assert _span(store, "proc_rollup_1m", "ts_min") / _DAY > 119
            assert [
                r["detail"].split(":")[0]
                for r in store.rows("ops_events", where="kind = 'db_guard'")
            ] == ["raw_24h"]


class TestWalAfterGuard:
    def test_wal_is_truncated_and_guard_does_not_refire(self, raw_template, tmp_path):
        db = _copy(raw_template, tmp_path)
        budget = int(db.stat().st_size * 0.6)

        with Storage(db) as store:
            first = store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)
            wal = Path(str(db) + "-wal")
            wal_bytes = wal.stat().st_size if wal.exists() else 0

            assert first["triggered"] is True
            assert first["size_after"] <= budget
            assert wal_bytes < 128 * 1024
            assert len(first["steps"]) < 5
            second = store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)
            assert second["triggered"] is False

    def test_wal_held_by_a_reader_is_returned_on_the_next_run(
        self, raw_template, tmp_path
    ):
        db = _copy(raw_template, tmp_path)
        budget = int(db.stat().st_size * 0.6)

        with Storage(db) as store:
            store.conn.execute("PRAGMA busy_timeout=0")
            reader = sqlite3.connect(db)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM proc_samples").fetchone()
            first = store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)
            reader.close()

            assert first["triggered"] is True
            second = store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)
            assert second["triggered"] is False
        wal = Path(str(db) + "-wal")
        assert not wal.exists() or wal.stat().st_size < 128 * 1024


class TestDiskLow:
    def test_stops_at_the_rung_that_returns_enough_space(
        self, mixed_template, tmp_path, monkeypatch
    ):
        db = _copy(mixed_template, tmp_path)
        on_disk = _on_disk(db)
        full = _vacuumed_size(db, tmp_path)
        cut = _vacuumed_size(db, tmp_path, "24h")
        disk = _FakeDisk(db, capacity=6 * on_disk)
        min_free = int(disk.free() + (on_disk - full) + 0.5 * (full - cut))
        monkeypatch.setattr("procdiag.storage.shutil.disk_usage", disk)

        with Storage(db) as store:
            result = store.enforce_size_budget(max_bytes=None, min_free_bytes=min_free)

            assert result["steps"] == ["raw_24h"]
            assert _span(store, "proc_rollup_1m", "ts_min") / _DAY > 19
        assert disk.free() >= min_free


class TestNearlyFullDisk:
    def test_vacuum_is_skipped_when_the_disk_cannot_hold_its_copy(
        self, mixed_template, tmp_path, monkeypatch
    ):
        db = _copy(mixed_template, tmp_path)
        before = db.stat().st_size
        budget = _budget_needing_first_rung(db, tmp_path)
        monkeypatch.setattr(
            "procdiag.storage.shutil.disk_usage",
            lambda _p: _Usage(10 * _MiB, 10 * _MiB - 1, 1),
        )

        with Storage(db) as store:
            result = store.enforce_size_budget(max_bytes=budget, min_free_bytes=None)

            assert result["triggered"] is True
            assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            errors = store.rows("ops_events", where="kind = 'db_guard_error'")
            assert any("VACUUM" in e["detail"] for e in errors)
            assert result["vacuumed"] is False
        assert db.stat().st_size >= before

    def test_failing_vacuum_is_recorded_and_the_wal_is_returned(
        self, mixed_template, tmp_path
    ):
        db = _copy(mixed_template, tmp_path)
        budget = _budget_needing_first_rung(db, tmp_path)

        with Storage(db) as store:
            real = store.conn
            store.conn = _VacuumFails(real)  # type: ignore[assignment]
            try:
                result = store.enforce_size_budget(
                    max_bytes=budget, min_free_bytes=None
                )
            finally:
                store.conn = real

            errors = store.rows("ops_events", where="kind = 'db_guard_error'")
            assert any("VACUUM" in e["detail"] for e in errors)
            assert result["vacuumed"] is False
        wal = Path(str(db) + "-wal")
        assert not wal.exists() or wal.stat().st_size < 128 * 1024
