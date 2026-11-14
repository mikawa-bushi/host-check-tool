"""Tests for procdiag.storage — schema, buffer/flush, series/rows/counts/meta,
and registry upsert helpers."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from procdiag.storage import (
    SCHEMA_VERSION,
    TABLE_COLUMNS,
    Storage,
    StorageVersionError,
)

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
# Schema migrations + ops_events index (issue v0.3 §2-1 / §4-8)
# ---------------------------------------------------------------------------


def _has_ops_index(store) -> bool:
    row = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_ops_events_kind_ts'"
    ).fetchone()
    return row is not None


def test_ops_events_index_created_on_fresh_db(tmp_db):
    assert _has_ops_index(tmp_db)


def test_open_migrates_old_db_to_current(tmp_path):
    """Opening a DB stamped with an older version migrates it forward."""
    db = tmp_path / "mig.db"
    with Storage(db) as store:
        store.set_meta("schema_version", "1")
        store.conn.commit()
    with Storage(db) as store:
        assert store.meta()["schema_version"] == str(SCHEMA_VERSION)
        assert _has_ops_index(store)


def test_newer_schema_version_refused_on_write(tmp_path):
    db = tmp_path / "future.db"
    with Storage(db) as store:
        store.set_meta("schema_version", "999")
        store.conn.commit()
    with pytest.raises(StorageVersionError):
        Storage(db)


def test_newer_schema_version_refused_read_only(tmp_path):
    db = tmp_path / "future_ro.db"
    with Storage(db) as store:
        store.set_meta("schema_version", "999")
        store.conn.commit()
    with pytest.raises(StorageVersionError):
        Storage(db, read_only=True)


def test_version_matches_pyproject():
    """__version__ must equal pyproject's version (issue §2-4: catch drift).

    Metadata-derivation was rejected (stale editable installs report the wrong
    version), so the two declared values are kept in sync by this guard instead.
    """
    tomllib = pytest.importorskip("tomllib")  # stdlib on py3.11+
    import procdiag

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert procdiag.__version__ == data["tool"]["poetry"]["version"]


def test_buffer_rejects_wrong_arity(tmp_db):
    """A row whose width drifts from TABLE_COLUMNS is rejected (issue §2-6)."""
    with pytest.raises(ValueError):
        tmp_db.buffer("sys_samples", [(1.0, 2.0)])  # far too few columns


def test_proc_first_seen(tmp_db):
    """proc_first_seen returns the registry first_seen, or None (issue §3-7)."""
    ts = 1_700_000_000.0
    pid = tmp_db.proc_id_for("p", "h00000000001", "p --x", ts)
    tmp_db.conn.commit()
    assert tmp_db.proc_first_seen(pid) == pytest.approx(ts)
    assert tmp_db.proc_first_seen(999_999) is None


def test_table_columns_match_schema(tmp_db):
    """TABLE_COLUMNS must match the real column order for every listed table.

    The buffer/flush write path INSERTs positionally in TABLE_COLUMNS order, and
    synthetic test fixtures build tuples by hand, so a silent drift between this
    map and the actual CREATE TABLE would corrupt writes (issue v0.3 §2-2).
    """
    for table, cols in TABLE_COLUMNS.items():
        actual = [
            r[1] for r in tmp_db.conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        assert actual == list(cols), f"{table}: TABLE_COLUMNS drift vs schema"


def test_clock_skew_resets_watermark(tmp_path):
    """A backward clock jump (watermark in the future) is detected (issue §3-3)."""
    db = tmp_path / "skew.db"
    with Storage(db) as store:
        # Simulate a watermark written when the clock was far ahead.
        future = (int(time.time() / 60) * 60) + 100_000 * 60
        store.set_meta("rollup_watermark", str(future))
        store.conn.commit()

        store.rollup_incremental()

        wm = float(store.meta()["rollup_watermark"])
        assert wm < future, "watermark must be pulled back from the future"
        skew = store.conn.execute(
            "SELECT COUNT(*) AS c FROM ops_events WHERE kind='clock_skew'"
        ).fetchone()
        assert skew["c"] >= 1, "a clock_skew event must be recorded"


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
    return (ts, "/", 100_000_000_000, 40_000_000_000, 1_000_000, 250_000, 0)


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
            (1.0, "/", 100_000_000_000, 10_000_000_000, None, None, 0),
            (2.0, "/var", 50_000_000_000, 5_000_000_000, None, None, 0),
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
    tmp_db.proc_id_for("proc", "hash000000", "proc", ts2)
    tmp_db.flush()

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


# ---------------------------------------------------------------------------
# Registry name-lookup helpers (issue #4 §3-3)
# ---------------------------------------------------------------------------


def test_proc_name_returns_registered_name(tmp_db):
    proc_id = tmp_db.proc_id_for("nginx", "abc123hash", "nginx -g daemon", 1000.0)
    assert tmp_db.proc_name(proc_id) == "nginx"


def test_proc_name_falls_back_for_unknown_id(tmp_db):
    assert tmp_db.proc_name(99999) == "proc_99999"


def test_cg_unit_path_returns_registered_path(tmp_db):
    cg_id = tmp_db.cg_id_for("system.slice/nginx.service", 1000.0)
    assert tmp_db.cg_unit_path(cg_id) == "system.slice/nginx.service"


def test_cg_unit_path_falls_back_for_unknown_id(tmp_db):
    assert tmp_db.cg_unit_path(99999) == "99999"


# ---------------------------------------------------------------------------
# read-only rows() query cache + cgroup last_seen (issue #3 §2)
# ---------------------------------------------------------------------------


def test_readonly_rows_are_cached(tmp_path):
    db = tmp_path / "cache.db"
    with Storage(db) as store:
        store.buffer("sys_samples", [_sys_row(1.0), _sys_row(2.0)])
        store.flush()
    with Storage(db, read_only=True) as ro:
        first = ro.rows("sys_samples")
        second = ro.rows("sys_samples")
        # A read-only Storage caches identical queries → same object returned,
        # so multiple rules sharing a window do not re-scan the table.
        assert first is second
        assert len(first) == 2
        # A different query is a separate cache entry.
        assert ro.rows("sys_samples", since=1.5) is not first


def test_readwrite_rows_are_not_cached(tmp_db):
    tmp_db.buffer("sys_samples", [_sys_row(1.0)])
    tmp_db.flush()
    # A writable Storage must not cache: its data changes over the session.
    assert tmp_db.rows("sys_samples") is not tmp_db.rows("sys_samples")


def test_cg_last_seen_is_the_latest_sighting_after_a_flush(tmp_db):
    unit = "system.slice/throttle.service"
    for ts in (1000.0, 1010.0, 1100.0):
        tmp_db.cg_id_for(unit, ts)
    tmp_db.flush()

    row = tmp_db.conn.execute(
        "SELECT first_seen, last_seen FROM cgroup_registry WHERE unit_path = ?",
        (unit,),
    ).fetchone()
    assert (row["first_seen"], row["last_seen"]) == (1000.0, 1100.0)


# ---------------------------------------------------------------------------
# make_row — name-keyed row construction for samplers
# ---------------------------------------------------------------------------


def test_make_row_orders_columns_by_table_columns():
    from procdiag.storage import make_row

    row = make_row("dir_samples", size_bytes=40, path="/var/log", ts=1.0, is_estimate=0)
    assert row == (1.0, "/var/log", 40, 0)


def test_make_row_rejects_missing_and_unknown_columns():
    from procdiag.storage import make_row

    with pytest.raises(ValueError, match="不足"):
        make_row("dir_samples", ts=1.0, path="/", size_bytes=100)
    with pytest.raises(ValueError, match="不明"):
        make_row("dir_samples", ts=1.0, path="/", size_bytes=1, is_estimate=0, bogus=2)


# ---------------------------------------------------------------------------
# Aggregate read helpers (status / engine quality)
# ---------------------------------------------------------------------------


def test_time_bounds_and_self_cpu_stats(tmp_db):
    assert tmp_db.time_bounds("sys_samples") is None
    assert tmp_db.self_cpu_stats() == (None, None)

    rows = [_sys_row(10.0), _sys_row(20.0), _sys_row(30.0)]
    rows[2] = rows[2][:-2] + (2.0, rows[2][-1])  # self_cpu 0.5, 0.5, 2.0
    tmp_db.buffer("sys_samples", rows)
    tmp_db.flush()

    assert tmp_db.time_bounds("sys_samples") == (10.0, 30.0)
    assert tmp_db.self_cpu_stats() == (pytest.approx(1.0), 2.0)
    assert tmp_db.self_cpu_stats(since=15.0, until=25.0) == (0.5, 0.5)


def test_missing_io_ratio(tmp_db):
    assert tmp_db.missing_io_ratio(0.0, 100.0) == 0.0
    readable = _proc_row(1.0, pid=1)[:-2] + (10, 20)
    tmp_db.buffer("proc_samples", [readable, _proc_row(1.0, pid=2)])
    tmp_db.flush()
    assert tmp_db.missing_io_ratio(0.0, 100.0) == pytest.approx(0.5)


def test_recent_events_returns_newest_oldest_first(tmp_db):
    for i in range(7):
        tmp_db.conn.execute(
            "INSERT INTO ops_events (ts, kind, detail) VALUES (?, ?, '')",
            (float(i), f"k{i}"),
        )
    tmp_db.conn.commit()
    assert [r["kind"] for r in tmp_db.recent_events(3)] == ["k4", "k5", "k6"]


def test_record_spike_and_write_meta(tmp_db):
    tmp_db.write_meta({"hostname": "edge-01", "kernel": "6.8"})
    tmp_db.record_spike(
        trigger_type="cpu",
        psi_snapshot="{}",
        top_processes='{"by_cpu": [], "by_rss": []}',
        top_cgroups='{"by_mem": []}',
    )
    assert tmp_db.meta()["hostname"] == "edge-01"
    (spike,) = tmp_db.rows("spike_events")
    assert spike["trigger_type"] == "cpu"
