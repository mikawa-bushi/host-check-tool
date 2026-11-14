"""Tests for rollup_incremental and retention (expiry) in procdiag.storage."""

from __future__ import annotations

import time

import pytest

from procdiag.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_sys_samples(
    store: Storage, start_ts: float, count: int, interval: float = 1.0
):
    """Insert ``count`` sys_samples rows starting at ``start_ts``."""
    rows = []
    for i in range(count):
        ts = start_ts + i * interval
        rows.append(
            (
                ts,  # ts
                float(20 + i % 10),  # cpu_total (20..29 repeating)
                float(30 + i % 5),  # cpu_max_core
                4_000_000_000,  # mem_used
                2_000_000_000,  # mem_available
                0,  # swap_used
                0,  # swap_in
                i * 1024,  # swap_out — increments so sum is non-zero
                1.0,  # load1
                None,  # psi_cpu_some10
                None,  # psi_mem_some10
                None,  # psi_mem_full10
                None,  # psi_io_some10
                None,  # psi_io_full10
                0,  # disk_read_bytes
                0,  # disk_write_bytes
                0.1,  # self_cpu
                50_000_000,  # self_rss
            )
        )
    store.buffer("sys_samples", rows)
    store.flush()


def _insert_proc_samples(
    store: Storage, proc_id: int, start_ts: float, count: int, interval: float = 1.0
):
    rows = []
    for i in range(count):
        ts = start_ts + i * interval
        rows.append(
            (
                ts,  # ts
                proc_id,  # proc_id
                1000 + i,  # pid (varies but proc_id is constant → same process)
                float(5 + i % 5),  # cpu_pct
                200_000_000,  # rss
                None,  # pss
                4,  # num_threads
                50,  # num_fds
                None,  # io_read_delta
                None,  # io_write_delta
            )
        )
    store.buffer("proc_samples", rows)
    store.flush()


# ---------------------------------------------------------------------------
# rollup_incremental — basic correctness
# ---------------------------------------------------------------------------


def test_rollup_incremental_creates_sys_rollup(tmp_path):
    """One minute of sys_samples should produce one sys_rollup_1m row."""
    db_path = tmp_path / "roll.db"
    # Choose a minute that is definitely in the past
    minute_floor = (int(time.time()) // 60 - 5) * 60.0
    with Storage(db_path) as store:
        _insert_sys_samples(store, start_ts=minute_floor, count=60, interval=1.0)
        store.rollup_incremental()

        rows = store.rows("sys_rollup_1m")
        assert len(rows) >= 1, "Expected at least one rollup row"

        row = rows[0]
        # cpu_total ranges 20..29 → avg ≈ 24.5, max = 29
        assert row["cpu_max"] == pytest.approx(29.0)
        assert row["cpu_avg"] == pytest.approx(24.5, abs=1.0)
        # p95 of [20,21,...,29] repeated 6 times: 95th percentile is 28 or 29
        assert row["cpu_p95"] >= 28.0
        # swap_out_sum = sum(0,1,...,59) * 1024
        expected_swap_sum = sum(range(60)) * 1024
        assert row["swap_out_sum"] == expected_swap_sum


def test_rollup_incremental_proc(tmp_path):
    """proc_samples for one process should produce a proc_rollup_1m row."""
    db_path = tmp_path / "roll_proc.db"
    minute_floor = (int(time.time()) // 60 - 5) * 60.0
    with Storage(db_path) as store:
        # Ensure proc_registry has an entry
        store.proc_id_for("testproc", "abc000000001", "testproc --opt", minute_floor)
        store.conn.commit()

        proc_id = store.proc_id_for(
            "testproc", "abc000000001", "testproc --opt", minute_floor
        )
        _insert_proc_samples(
            store, proc_id=proc_id, start_ts=minute_floor, count=6, interval=10.0
        )
        store.rollup_incremental()

        rows = store.rows("proc_rollup_1m")
        assert len(rows) >= 1
        # cpu_pct cycles 5,6,7,8,9 → max is 9, min is 5
        assert rows[0]["cpu_max"] == pytest.approx(9.0)
        assert rows[0]["rss_avg"] == 200_000_000


def test_rollup_incremental_is_idempotent(tmp_path):
    """Calling rollup_incremental twice should not create duplicate rows."""
    db_path = tmp_path / "roll_idem.db"
    minute_floor = (int(time.time()) // 60 - 5) * 60.0
    with Storage(db_path) as store:
        _insert_sys_samples(store, start_ts=minute_floor, count=60)
        store.rollup_incremental()
        count_after_1 = store.conn.execute(
            "SELECT COUNT(*) FROM sys_rollup_1m"
        ).fetchone()[0]
        store.rollup_incremental()
        count_after_2 = store.conn.execute(
            "SELECT COUNT(*) FROM sys_rollup_1m"
        ).fetchone()[0]
        assert count_after_1 == count_after_2


def test_rollup_does_not_process_current_minute(tmp_path):
    """Samples in the current (incomplete) minute must not be rolled up."""
    db_path = tmp_path / "roll_cur.db"
    now = time.time()
    current_minute_floor = int(now / 60) * 60.0
    with Storage(db_path) as store:
        # Insert samples in the current minute
        _insert_sys_samples(store, start_ts=current_minute_floor, count=5, interval=1.0)
        store.rollup_incremental()

        count = store.conn.execute("SELECT COUNT(*) FROM sys_rollup_1m").fetchone()[0]
        # No rollup rows should be created for the current (incomplete) minute
        assert count == 0


# ---------------------------------------------------------------------------
# retention — expiry deletes correct rows
# ---------------------------------------------------------------------------


def test_retention_deletes_old_raw_samples(tmp_path):
    """Samples older than ``raw`` cutoff should be deleted."""
    db_path = tmp_path / "ret.db"
    with Storage(db_path) as store:
        now = time.time()
        # Insert two batches: one 3 hours ago (old), one 30 minutes ago (fresh)
        _insert_sys_samples(store, start_ts=now - 3 * 3600, count=10)
        _insert_sys_samples(store, start_ts=now - 30 * 60, count=10)

        result = store.retention(raw="1h", rollup="2h")

        remaining = store.conn.execute("SELECT COUNT(*) FROM sys_samples").fetchone()[0]
        assert remaining == 10, "Only fresh samples should survive"
        assert result["raw_deleted"] == 10


def test_retention_deletes_old_proc_samples(tmp_path):
    db_path = tmp_path / "ret_proc.db"
    with Storage(db_path) as store:
        now = time.time()
        ts_old = now - 3 * 3600
        ts_new = now - 30 * 60

        proc_id = store.proc_id_for("p", "hash000000a1", "p", ts_old)
        store.conn.commit()
        _insert_proc_samples(store, proc_id=proc_id, start_ts=ts_old, count=5)
        _insert_proc_samples(store, proc_id=proc_id, start_ts=ts_new, count=5)

        store.retention(raw="1h", rollup="2h")

        remaining = store.conn.execute("SELECT COUNT(*) FROM proc_samples").fetchone()[
            0
        ]
        assert remaining == 5


def test_retention_deletes_old_rollup_rows(tmp_path):
    """Rollup rows older than ``rollup`` cutoff should be deleted."""
    db_path = tmp_path / "ret_roll.db"
    with Storage(db_path) as store:
        now = time.time()
        # Insert rollup rows directly — one old, one recent
        old_ts_min = now - 3 * 3600
        new_ts_min = now - 30 * 60

        store.conn.execute(
            "INSERT OR REPLACE INTO sys_rollup_1m "
            "(ts_min, cpu_avg, cpu_max, cpu_p95, cpu_max_core_max, "
            " mem_used_avg, mem_used_max, psi_cpu_max, psi_mem_max, psi_io_max, swap_out_sum) "
            "VALUES (?, 20.0, 30.0, 28.0, 35.0, 4000000000, 4100000000, 5.0, 2.0, 10.0, 0)",
            (old_ts_min,),
        )
        store.conn.execute(
            "INSERT OR REPLACE INTO sys_rollup_1m "
            "(ts_min, cpu_avg, cpu_max, cpu_p95, cpu_max_core_max, "
            " mem_used_avg, mem_used_max, psi_cpu_max, psi_mem_max, psi_io_max, swap_out_sum) "
            "VALUES (?, 22.0, 32.0, 30.0, 38.0, 4000000000, 4100000000, 5.0, 2.0, 10.0, 0)",
            (new_ts_min,),
        )
        store.conn.commit()

        result = store.retention(raw="10d", rollup="2h")

        remaining = store.conn.execute("SELECT COUNT(*) FROM sys_rollup_1m").fetchone()[
            0
        ]
        assert remaining == 1
        assert result["rollup_deleted"] >= 1


def test_retention_leaves_spike_events_alone(tmp_path):
    """spike_events and ops_events are never deleted by retention()."""
    db_path = tmp_path / "ret_evt.db"
    with Storage(db_path) as store:
        now = time.time()
        # Insert an old ops_event
        store.conn.execute(
            "INSERT INTO ops_events (ts, kind, detail) VALUES (?, 'gap', 'old')",
            (now - 7 * 86400,),
        )
        store.conn.commit()

        store.retention(raw="1h", rollup="1h")

        count = store.conn.execute("SELECT COUNT(*) FROM ops_events").fetchone()[0]
        # Should still be there (retention does not touch ops_events)
        assert count == 1


def test_enforce_size_budget_records_retention_failure(tmp_path, monkeypatch):
    """A failing retention during the size guard is recorded, not swallowed."""
    import sqlite3

    with Storage(tmp_path / "guard.db") as store:

        def _boom(*_a, **_k):
            raise sqlite3.OperationalError("retention boom")

        monkeypatch.setattr(store, "_delete_before", _boom)
        # max_bytes=1 forces over-budget so the retention ladder runs.
        store.enforce_size_budget(max_bytes=1, min_free_bytes=None)

        errors = store.rows("ops_events", where="kind = 'db_guard_error'")
        assert len(errors) >= 1
