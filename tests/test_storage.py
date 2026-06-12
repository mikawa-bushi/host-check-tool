"""Tests for procdiag.storage — schema, buffer/flush, series/rows/counts/meta,
and registry upsert helpers."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from procdiag.storage import SCHEMA_VERSION, TABLE_COLUMNS, Storage

# ---------------------------------------------------------------------------
# Schema: TABLE_COLUMNS matches PRAGMA table_info for every table
# ---------------------------------------------------------------------------


def test_table_columns_match_pragma(tmp_db):
    """Every entry in TABLE_COLUMNS must exactly match the DDL column order."""
    for tbl, expected_cols in TABLE_COLUMNS.items():
        pragma_rows = tmp_db.conn.execute(f"PRAGMA table_info({tbl})").fetchall()
        actual_cols = [r["name"] for r in pragma_rows]
        assert actual_cols == expected_cols, (
            f"TABLE_COLUMNS['{tbl}'] does not match DDL column order\n"
            f"  expected: {expected_cols}\n"
            f"  actual:   {actual_cols}"
        )


# ---------------------------------------------------------------------------
# Schema version is recorded in meta
# ---------------------------------------------------------------------------


def test_schema_version_in_meta(tmp_db):
    m = tmp_db.meta()
    assert "schema_version" in m
    assert m["schema_version"] == str(SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# buffer → flush inserts rows into every raw-sample table
# ---------------------------------------------------------------------------


def _sys_row(ts: float = 1.0):
    """Return a full sys_samples row tuple in TABLE_COLUMNS order."""
    return (
        ts,  # ts
        25.0,  # cpu_total
        40.0,  # cpu_max_core
        4_000_000_000,  # mem_used
        2_000_000_000,  # mem_available
        500_000_000,  # swap_used
        0,  # swap_in
        0,  # swap_out
        1.5,  # load1
        5.0,  # psi_cpu_some10
        2.0,  # psi_mem_some10
        0.5,  # psi_mem_full10
        10.0,  # psi_io_some10
        0.0,  # psi_io_full10
        1024,  # disk_read_bytes
        2048,  # disk_write_bytes
        0.5,  # self_cpu
        50_000_000,  # self_rss
    )


def _cg_row(ts: float = 1.0, cg_id: int = 1):
    return (ts, cg_id, 1000, 512_000_000, 0, 4096, 8192, 5)


def _proc_row(ts: float = 1.0, proc_id: int = 1, pid: int = 100):
    return (ts, proc_id, pid, 10.0, 200_000_000, None, 4, 50, None, None)


def _disk_row(ts: float = 1.0):
    return (ts, "/", 100_000_000_000, 40_000_000_000)


def _dir_row(ts: float = 1.0):
    return (ts, "/var/log", 5_000_000_000, 0)


def test_buffer_flush_sys_samples(tmp_db):
    tmp_db.buffer("sys_samples", [_sys_row(1.0)])
    written = tmp_db.flush()
    assert written == 1

    rows = tmp_db.rows("sys_samples")
    assert len(rows) == 1
    assert rows[0]["cpu_total"] == 25.0


def test_buffer_flush_cg_samples(tmp_db):
    tmp_db.buffer("cg_samples", [_cg_row(2.0)])
    written = tmp_db.flush()
    assert written == 1

    rows = tmp_db.rows("cg_samples")
    assert len(rows) == 1
    assert rows[0]["cpu_usec_delta"] == 1000


def test_buffer_flush_proc_samples(tmp_db):
    tmp_db.buffer("proc_samples", [_proc_row(3.0)])
    written = tmp_db.flush()
    assert written == 1


def test_buffer_flush_disk_samples(tmp_db):
    tmp_db.buffer("disk_samples", [_disk_row(4.0)])
    written = tmp_db.flush()
    assert written == 1

    rows = tmp_db.rows("disk_samples")
    assert len(rows) == 1
    assert rows[0]["mountpoint"] == "/"


def test_buffer_flush_dir_samples(tmp_db):
    tmp_db.buffer("dir_samples", [_dir_row(5.0)])
    written = tmp_db.flush()
    assert written == 1


def test_buffer_accumulates_before_flush(tmp_db):
    tmp_db.buffer("sys_samples", [_sys_row(1.0)])
    tmp_db.buffer("sys_samples", [_sys_row(2.0)])
    # Not flushed yet — rows not in DB
    rows_before = tmp_db.rows("sys_samples")
    assert len(rows_before) == 0

    tmp_db.flush()
    rows_after = tmp_db.rows("sys_samples")
    assert len(rows_after) == 2


def test_flush_empty_buffer_returns_zero(tmp_db):
    assert tmp_db.flush() == 0


# ---------------------------------------------------------------------------
# series / rows / counts / meta
# ---------------------------------------------------------------------------


def test_series_returns_ts_value_pairs(tmp_db):
    tmp_db.buffer("sys_samples", [_sys_row(10.0), _sys_row(20.0)])
    tmp_db.flush()

    pairs = tmp_db.series("sys_samples", "cpu_total")
    assert len(pairs) == 2
    assert pairs[0] == (10.0, 25.0)
    assert pairs[1] == (20.0, 25.0)


def test_series_with_since_filter(tmp_db):
    tmp_db.buffer(
        "sys_samples",
        [_sys_row(100.0), _sys_row(200.0), _sys_row(300.0)],
    )
    tmp_db.flush()

    pairs = tmp_db.series("sys_samples", "cpu_total", since=150.0)
    assert len(pairs) == 2
    assert pairs[0][0] == 200.0


def test_series_with_subject_col(tmp_db):
    tmp_db.buffer(
        "proc_samples",
        [_proc_row(1.0, proc_id=1), _proc_row(2.0, proc_id=2)],
    )
    tmp_db.flush()

    pairs = tmp_db.series("proc_samples", "cpu_pct", subject_col="proc_id", subject=1)
    assert len(pairs) == 1
    assert pairs[0][1] == 10.0


def test_rows_with_where(tmp_db):
    tmp_db.buffer(
        "disk_samples",
        [
            (1.0, "/", 100_000_000_000, 10_000_000_000),
            (2.0, "/var", 50_000_000_000, 5_000_000_000),
        ],
    )
    tmp_db.flush()

    rows = tmp_db.rows("disk_samples", where="mountpoint = ?", params=("/",))
    assert len(rows) == 1
    assert rows[0]["mountpoint"] == "/"


def test_counts_returns_table_counts(tmp_db):
    tmp_db.buffer("sys_samples", [_sys_row(1.0)])
    tmp_db.flush()

    c = tmp_db.counts()
    assert c["sys_samples"] == 1
    assert "db_bytes" in c
    assert c["db_bytes"] > 0


def test_set_meta_and_meta(tmp_db):
    tmp_db.set_meta("hostname", "testbox")
    tmp_db.conn.commit()
    m = tmp_db.meta()
    assert m["hostname"] == "testbox"


# ---------------------------------------------------------------------------
# proc_id_for upsert behaviour
# ---------------------------------------------------------------------------


def test_proc_id_for_returns_same_id_for_same_key(tmp_db):
    ts = time.time()
    id1 = tmp_db.proc_id_for("mysqld", "abc123456789", "mysqld --port 3306", ts)
    id2 = tmp_db.proc_id_for("mysqld", "abc123456789", "mysqld --port 3306", ts + 10)
    assert id1 == id2


def test_proc_id_for_different_key_different_id(tmp_db):
    ts = time.time()
    id1 = tmp_db.proc_id_for("mysqld", "abc123456789", "mysqld", ts)
    id2 = tmp_db.proc_id_for("nginx", "def123456789", "nginx -g daemon off", ts)
    assert id1 != id2


def test_proc_id_for_updates_last_seen(tmp_db):
    ts1 = 1000.0
    ts2 = 2000.0
    tmp_db.proc_id_for("proc", "hash000000", "proc", ts1)
    tmp_db.conn.commit()
    tmp_db.proc_id_for("proc", "hash000000", "proc", ts2)
    tmp_db.conn.commit()

    row = tmp_db.conn.execute(
        "SELECT last_seen FROM proc_registry WHERE name='proc' AND cmdline_hash='hash000000'"
    ).fetchone()
    assert row["last_seen"] == ts2


def test_cg_id_for_returns_same_id_for_same_path(tmp_db):
    ts = time.time()
    id1 = tmp_db.cg_id_for("system.slice/nginx.service", ts)
    id2 = tmp_db.cg_id_for("system.slice/nginx.service", ts + 5)
    assert id1 == id2


def test_cg_id_for_different_path_different_id(tmp_db):
    ts = time.time()
    id1 = tmp_db.cg_id_for("system.slice/nginx.service", ts)
    id2 = tmp_db.cg_id_for("system.slice/mysql.service", ts)
    assert id1 != id2


# ---------------------------------------------------------------------------
# record_event
# ---------------------------------------------------------------------------


def test_record_event_inserts_to_ops_events(tmp_db):
    tmp_db.record_event("gap", "マシンが3分間応答不能でした")
    rows = tmp_db.rows("ops_events")
    assert len(rows) == 1
    assert rows[0]["kind"] == "gap"
    assert "応答不能" in rows[0]["detail"]


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


def test_read_only_opens_existing_db(tmp_path):
    db_path = tmp_path / "ro.db"
    # Create and populate with a writable store
    with Storage(db_path) as store:
        store.buffer("sys_samples", [_sys_row(1.0)])
        store.flush()

    # Now open read-only and verify we can read
    with Storage(db_path, read_only=True) as ro:
        rows = ro.rows("sys_samples")
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Tables without a timestamp column (registry/meta)
# ---------------------------------------------------------------------------


def test_rows_works_on_tables_without_ts_column(tmp_db):
    pid = tmp_db.proc_id_for("nginx", "abcdef123456", "nginx -g daemon", 1.0)
    rows = tmp_db.rows("proc_registry")
    assert [r["proc_id"] for r in rows] == [pid]
    tmp_db.cg_id_for("system.slice/nginx.service", 1.0)
    assert len(tmp_db.rows("cgroup_registry")) == 1


def test_rows_rejects_time_filter_on_no_ts_table(tmp_db):
    import pytest

    with pytest.raises(ValueError):
        tmp_db.rows("proc_registry", since=0.0)


def test_read_only_accepts_relative_path(tmp_path, monkeypatch):
    # The CLI passes user-supplied paths like "smoke.db"; as_uri() rejects
    # relative paths unless resolved first.
    monkeypatch.chdir(tmp_path)
    with Storage(Path("rel.db")) as store:
        store.buffer("sys_samples", [_sys_row(1.0)])
        store.flush()
    with Storage(Path("rel.db"), read_only=True) as ro:
        assert len(ro.rows("sys_samples")) == 1
