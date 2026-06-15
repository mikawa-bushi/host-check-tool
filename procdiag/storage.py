"""SQLite time-series store with buffered writes, incremental rollup, and retention.

Follows the netdiag ``storage.py`` idiom (stdlib sqlite3, ``_SCHEMA`` constant,
context-manager protocol) while implementing the procdiag-specific DDL from
design doc §5.2 verbatim.

Key design choices that mirror netdiag:
- WAL mode + synchronous=NORMAL: safe for power loss, avoids per-sample fsyncs
  that would pollute the I/O metrics being measured.
- Buffered writes: ``buffer()`` accumulates rows in memory; ``flush()`` writes
  them in a single transaction (60-second cadence, or on SIGTERM/spike events).
- Incremental rollup: ``rollup_incremental()`` processes only the newly
  completed minutes since the last watermark, so the cost is O(new data) rather
  than O(all data).
- Internal dict cache for registry lookups: ``proc_id_for`` / ``cg_id_for`` are
  called up to once per process per 10-second tick; the cache prevents a
  round-trip SQL query on every call.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core.util import parse_duration

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Minimum seconds between ``last_seen`` rewrites for the same cgroup unit.
# ``last_seen`` is metadata that nothing reads for logic, so the per-tick
# UPDATE is throttled to bound the write rate during long recordings.
_CG_LAST_SEEN_THROTTLE_SEC = 60.0

# ---------------------------------------------------------------------------
# DDL — verbatim from design doc §5.2
# ---------------------------------------------------------------------------

_SCHEMA = """
-- メタ情報
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- プロセス台帳
CREATE TABLE IF NOT EXISTS proc_registry (
  proc_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  cmdline_hash TEXT NOT NULL,
  cmdline_sample TEXT,
  first_seen REAL, last_seen REAL,
  UNIQUE(name, cmdline_hash)
);

-- cgroupユニット台帳
CREATE TABLE IF NOT EXISTS cgroup_registry (
  cg_id INTEGER PRIMARY KEY,
  unit_path TEXT UNIQUE NOT NULL,
  first_seen REAL, last_seen REAL
);

-- Tier1: システムサンプル(1秒)
CREATE TABLE IF NOT EXISTS sys_samples (
  ts REAL PRIMARY KEY,
  cpu_total REAL, cpu_max_core REAL,
  mem_used INTEGER, mem_available INTEGER,
  swap_used INTEGER, swap_in INTEGER, swap_out INTEGER,
  load1 REAL,
  psi_cpu_some10 REAL, psi_mem_some10 REAL, psi_mem_full10 REAL,
  psi_io_some10 REAL, psi_io_full10 REAL,
  disk_read_bytes INTEGER, disk_write_bytes INTEGER,
  self_cpu REAL, self_rss INTEGER
);

-- Tier2a: cgroupサンプル(5秒)
CREATE TABLE IF NOT EXISTS cg_samples (
  ts REAL, cg_id INTEGER,
  cpu_usec_delta INTEGER,
  mem_current INTEGER, mem_swap INTEGER,
  io_rbytes_delta INTEGER, io_wbytes_delta INTEGER,
  pids INTEGER,
  PRIMARY KEY (ts, cg_id)
);

-- Tier2b: プロセスサンプル(10秒)
CREATE TABLE IF NOT EXISTS proc_samples (
  ts REAL, proc_id INTEGER, pid INTEGER,
  cpu_pct REAL, rss INTEGER, pss INTEGER,
  num_threads INTEGER, num_fds INTEGER,
  io_read_delta INTEGER, io_write_delta INTEGER,
  PRIMARY KEY (ts, proc_id, pid)
);

-- Tier3: ディスクサンプル
CREATE TABLE IF NOT EXISTS disk_samples (
  ts REAL, mountpoint TEXT, total INTEGER, used INTEGER,
  PRIMARY KEY (ts, mountpoint)
);
CREATE TABLE IF NOT EXISTS dir_samples (
  ts REAL, path TEXT, size_bytes INTEGER, is_estimate INTEGER,
  PRIMARY KEY (ts, path)
);

-- スパイクイベント
CREATE TABLE IF NOT EXISTS spike_events (
  event_id INTEGER PRIMARY KEY,
  ts REAL, trigger_type TEXT,
  psi_snapshot TEXT,
  top_processes TEXT,
  top_cgroups TEXT
);

-- 欠測・運用イベント
CREATE TABLE IF NOT EXISTS ops_events (
  ts REAL, kind TEXT, detail TEXT
);

-- ロールアップ(1分)
CREATE TABLE IF NOT EXISTS sys_rollup_1m (
  ts_min REAL PRIMARY KEY,
  cpu_avg REAL, cpu_max REAL, cpu_p95 REAL,
  cpu_max_core_max REAL,
  mem_used_avg INTEGER, mem_used_max INTEGER,
  psi_cpu_max REAL, psi_mem_max REAL, psi_io_max REAL,
  swap_out_sum INTEGER
);
CREATE TABLE IF NOT EXISTS proc_rollup_1m (
  ts_min REAL, proc_id INTEGER,
  cpu_avg REAL, cpu_max REAL,
  rss_avg INTEGER, rss_max INTEGER,
  fds_max INTEGER, threads_max INTEGER,
  PRIMARY KEY (ts_min, proc_id)
);
CREATE TABLE IF NOT EXISTS cg_rollup_1m (
  ts_min REAL, cg_id INTEGER,
  cpu_usec_sum INTEGER,
  mem_avg INTEGER, mem_max INTEGER,
  io_rbytes_sum INTEGER, io_wbytes_sum INTEGER,
  PRIMARY KEY (ts_min, cg_id)
);
"""

# ---------------------------------------------------------------------------
# TABLE_COLUMNS — column-name lists that define SampleBatch.rows tuple order
# ---------------------------------------------------------------------------

TABLE_COLUMNS: Dict[str, List[str]] = {
    "sys_samples": [
        "ts",
        "cpu_total",
        "cpu_max_core",
        "mem_used",
        "mem_available",
        "swap_used",
        "swap_in",
        "swap_out",
        "load1",
        "psi_cpu_some10",
        "psi_mem_some10",
        "psi_mem_full10",
        "psi_io_some10",
        "psi_io_full10",
        "disk_read_bytes",
        "disk_write_bytes",
        "self_cpu",
        "self_rss",
    ],
    "cg_samples": [
        "ts",
        "cg_id",
        "cpu_usec_delta",
        "mem_current",
        "mem_swap",
        "io_rbytes_delta",
        "io_wbytes_delta",
        "pids",
    ],
    "proc_samples": [
        "ts",
        "proc_id",
        "pid",
        "cpu_pct",
        "rss",
        "pss",
        "num_threads",
        "num_fds",
        "io_read_delta",
        "io_write_delta",
    ],
    "disk_samples": [
        "ts",
        "mountpoint",
        "total",
        "used",
    ],
    "dir_samples": [
        "ts",
        "path",
        "size_bytes",
        "is_estimate",
    ],
    "sys_rollup_1m": [
        "ts_min",
        "cpu_avg",
        "cpu_max",
        "cpu_p95",
        "cpu_max_core_max",
        "mem_used_avg",
        "mem_used_max",
        "psi_cpu_max",
        "psi_mem_max",
        "psi_io_max",
        "swap_out_sum",
    ],
    "proc_rollup_1m": [
        "ts_min",
        "proc_id",
        "cpu_avg",
        "cpu_max",
        "rss_avg",
        "rss_max",
        "fds_max",
        "threads_max",
    ],
    "cg_rollup_1m": [
        "ts_min",
        "cg_id",
        "cpu_usec_sum",
        "mem_avg",
        "mem_max",
        "io_rbytes_sum",
        "io_wbytes_sum",
    ],
}


# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------


class Storage:
    """SQLite backend for procdiag.

    ``recorder.py`` (T05) owns the write path; ``diagnosis/engine.py`` (T06)
    and ``report.py`` (T07) use the read path.  Opening with
    ``read_only=True`` enforces that separation at the OS level.
    """

    def __init__(self, path: Any, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self._read_only = read_only

        if read_only:
            # URI mode: file must exist. as_uri() requires an absolute path,
            # so resolve first (the CLI passes user-supplied relative paths).
            uri = self.path.resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path))

        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

        if not read_only:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
            # Record schema version if not already present.
            existing = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self.conn.commit()

        # In-memory write buffer: table_name -> list[tuple]
        self._buffer: Dict[str, List[tuple]] = defaultdict(list)

        # Registry caches to avoid per-tick SQL round-trips.
        # key -> (id, last_seen_ts)
        self._proc_cache: Dict[Tuple[str, str], int] = {}
        self._cg_cache: Dict[str, int] = {}
        # Last ts at which each cgroup unit's last_seen was actually written
        # (write path only) — used to throttle the per-tick UPDATE.
        self._cg_last_seen_written: Dict[str, float] = {}

        # Query result cache, enabled only for read-only sessions so multiple
        # rules over the same window share a single scan.  ``None`` disables it
        # on the writable recorder connection (its data changes per tick).
        self._rows_cache: Optional[Dict[tuple, List[sqlite3.Row]]] = (
            {} if read_only else None
        )

    # ------------------------------------------------------------------
    # Write path (recorder-only)
    # ------------------------------------------------------------------

    def buffer(self, table: str, rows: List[tuple]) -> None:
        """Accumulate rows in the in-memory buffer for a later flush."""
        self._buffer[table].extend(rows)

    def flush(self, *, reason: str = "interval") -> int:
        """Write all buffered rows to SQLite in a single transaction.

        Returns the total number of rows written.  When ``reason`` is
        ``"spike"`` or ``"signal"`` an ops_event is also recorded so
        operators can see why a flush happened mid-interval.
        """
        if not self._buffer:
            if reason in ("spike", "signal"):
                self._record_ops_event_direct(reason, "バッファ空でフラッシュ")
            return 0

        total = 0
        with self.conn:
            for table, rows in self._buffer.items():
                if not rows:
                    continue
                cols = TABLE_COLUMNS.get(table)
                if cols is None:
                    continue  # unknown table — silently skip
                placeholders = ", ".join("?" * len(cols))
                col_list = ", ".join(cols)
                self.conn.executemany(
                    f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})",
                    rows,
                )
                total += len(rows)
            if reason in ("spike", "signal"):
                self.conn.execute(
                    "INSERT INTO ops_events (ts, kind, detail) VALUES (?, ?, ?)",
                    (time.time(), "flush", f"reason={reason}"),
                )
        self._buffer.clear()
        return total

    def rollup_incremental(self) -> None:
        """Aggregate completed minutes into rollup tables since last watermark.

        Only minutes whose end timestamp is strictly in the past are processed
        so we never aggregate an in-progress minute.  The watermark stored in
        ``meta`` advances after each successful batch, making this idempotent
        and safe to call frequently.
        """
        now = time.time()
        current_minute_floor = int(now / 60) * 60  # start of the current minute

        wm_row = self.conn.execute(
            "SELECT value FROM meta WHERE key='rollup_watermark'"
        ).fetchone()
        watermark = float(wm_row["value"]) if wm_row else 0.0

        if watermark >= current_minute_floor:
            return  # nothing to roll up yet

        # --- sys_rollup_1m ---
        sys_rows = self.conn.execute(
            "SELECT ts, cpu_total, cpu_max_core, mem_used, "
            "       psi_cpu_some10, psi_mem_some10, psi_io_some10, swap_out "
            "FROM sys_samples "
            "WHERE ts >= ? AND ts < ? "
            "ORDER BY ts",
            (watermark, current_minute_floor),
        ).fetchall()

        if sys_rows:
            # Group by minute bucket
            minute_buckets: Dict[float, List[sqlite3.Row]] = defaultdict(list)
            for row in sys_rows:
                bucket = int(row["ts"] / 60) * 60.0
                minute_buckets[bucket].append(row)

            for bucket, rows in minute_buckets.items():
                cpu_vals = [r["cpu_total"] for r in rows if r["cpu_total"] is not None]
                core_vals = [
                    r["cpu_max_core"] for r in rows if r["cpu_max_core"] is not None
                ]
                mem_vals = [r["mem_used"] for r in rows if r["mem_used"] is not None]
                psi_cpu = [
                    r["psi_cpu_some10"] for r in rows if r["psi_cpu_some10"] is not None
                ]
                psi_mem = [
                    r["psi_mem_some10"] for r in rows if r["psi_mem_some10"] is not None
                ]
                psi_io = [
                    r["psi_io_some10"] for r in rows if r["psi_io_some10"] is not None
                ]
                swap_out = [r["swap_out"] for r in rows if r["swap_out"] is not None]

                cpu_avg = _avg(cpu_vals)
                cpu_max = max(cpu_vals) if cpu_vals else None
                cpu_p95 = _p95(cpu_vals)
                core_max = max(core_vals) if core_vals else None
                mem_avg = int(_avg(mem_vals)) if mem_vals else None
                mem_max = max(mem_vals) if mem_vals else None
                psi_cpu_max = max(psi_cpu) if psi_cpu else None
                psi_mem_max = max(psi_mem) if psi_mem else None
                psi_io_max = max(psi_io) if psi_io else None
                swap_sum = sum(swap_out) if swap_out else None

                self.conn.execute(
                    "INSERT OR REPLACE INTO sys_rollup_1m "
                    "(ts_min, cpu_avg, cpu_max, cpu_p95, cpu_max_core_max, "
                    " mem_used_avg, mem_used_max, psi_cpu_max, psi_mem_max, "
                    " psi_io_max, swap_out_sum) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bucket,
                        cpu_avg,
                        cpu_max,
                        cpu_p95,
                        core_max,
                        mem_avg,
                        mem_max,
                        psi_cpu_max,
                        psi_mem_max,
                        psi_io_max,
                        swap_sum,
                    ),
                )

        # --- proc_rollup_1m ---
        proc_rows = self.conn.execute(
            "SELECT ts, proc_id, cpu_pct, rss, num_fds, num_threads "
            "FROM proc_samples "
            "WHERE ts >= ? AND ts < ? "
            "ORDER BY ts",
            (watermark, current_minute_floor),
        ).fetchall()

        if proc_rows:
            # Group by (minute_bucket, proc_id)
            proc_buckets: Dict[Tuple[float, int], List[sqlite3.Row]] = defaultdict(list)
            for row in proc_rows:
                key = (int(row["ts"] / 60) * 60.0, row["proc_id"])
                proc_buckets[key].append(row)

            for (bucket, proc_id), rows in proc_buckets.items():
                cpu_vals = [r["cpu_pct"] for r in rows if r["cpu_pct"] is not None]
                rss_vals = [r["rss"] for r in rows if r["rss"] is not None]
                fds_vals = [r["num_fds"] for r in rows if r["num_fds"] is not None]
                thr_vals = [
                    r["num_threads"] for r in rows if r["num_threads"] is not None
                ]

                self.conn.execute(
                    "INSERT OR REPLACE INTO proc_rollup_1m "
                    "(ts_min, proc_id, cpu_avg, cpu_max, rss_avg, rss_max, "
                    " fds_max, threads_max) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bucket,
                        proc_id,
                        _avg(cpu_vals),
                        max(cpu_vals) if cpu_vals else None,
                        int(_avg(rss_vals)) if rss_vals else None,
                        max(rss_vals) if rss_vals else None,
                        max(fds_vals) if fds_vals else None,
                        max(thr_vals) if thr_vals else None,
                    ),
                )

        # --- cg_rollup_1m ---
        cg_rows = self.conn.execute(
            "SELECT ts, cg_id, cpu_usec_delta, mem_current, "
            "       io_rbytes_delta, io_wbytes_delta "
            "FROM cg_samples "
            "WHERE ts >= ? AND ts < ? "
            "ORDER BY ts",
            (watermark, current_minute_floor),
        ).fetchall()

        if cg_rows:
            cg_buckets: Dict[Tuple[float, int], List[sqlite3.Row]] = defaultdict(list)
            for row in cg_rows:
                key = (int(row["ts"] / 60) * 60.0, row["cg_id"])
                cg_buckets[key].append(row)

            for (bucket, cg_id), rows in cg_buckets.items():
                cpu_sum = sum(
                    r["cpu_usec_delta"] for r in rows if r["cpu_usec_delta"] is not None
                )
                mem_vals = [
                    r["mem_current"] for r in rows if r["mem_current"] is not None
                ]
                io_r = sum(
                    r["io_rbytes_delta"]
                    for r in rows
                    if r["io_rbytes_delta"] is not None
                )
                io_w = sum(
                    r["io_wbytes_delta"]
                    for r in rows
                    if r["io_wbytes_delta"] is not None
                )

                self.conn.execute(
                    "INSERT OR REPLACE INTO cg_rollup_1m "
                    "(ts_min, cg_id, cpu_usec_sum, mem_avg, mem_max, "
                    " io_rbytes_sum, io_wbytes_sum) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        bucket,
                        cg_id,
                        cpu_sum,
                        int(_avg(mem_vals)) if mem_vals else None,
                        max(mem_vals) if mem_vals else None,
                        io_r,
                        io_w,
                    ),
                )

        # Advance watermark
        self.set_meta("rollup_watermark", str(current_minute_floor))
        self.conn.commit()

    def retention(self, raw: str = "48h", rollup: str = "90d") -> Dict[str, int]:
        """Delete expired rows from raw-sample and rollup tables.

        ``raw`` and ``rollup`` are duration strings accepted by
        :func:`~procdiag.core.util.parse_duration` (e.g. ``"48h"``, ``"90d"``).
        Returns ``{"raw_deleted": n, "rollup_deleted": m}``.
        """
        now = time.time()
        raw_cutoff = now - parse_duration(raw)
        rollup_cutoff = now - parse_duration(rollup)

        raw_deleted = 0
        with self.conn:
            for tbl in ("sys_samples", "cg_samples", "proc_samples"):
                cur = self.conn.execute(
                    f"DELETE FROM {tbl} WHERE ts < ?", (raw_cutoff,)
                )
                raw_deleted += cur.rowcount

        rollup_deleted = 0
        with self.conn:
            for tbl in ("sys_rollup_1m", "proc_rollup_1m", "cg_rollup_1m"):
                cur = self.conn.execute(
                    f"DELETE FROM {tbl} WHERE ts_min < ?", (rollup_cutoff,)
                )
                rollup_deleted += cur.rowcount
            for tbl in ("disk_samples", "dir_samples"):
                cur = self.conn.execute(
                    f"DELETE FROM {tbl} WHERE ts < ?", (rollup_cutoff,)
                )
                rollup_deleted += cur.rowcount

        return {"raw_deleted": raw_deleted, "rollup_deleted": rollup_deleted}

    def enforce_size_budget(
        self,
        *,
        max_bytes: Optional[int],
        min_free_bytes: Optional[int],
    ) -> Dict[str, Any]:
        """Enforce DB size and free-disk constraints with a deterministic ladder.

        Checks the current DB size (main file + WAL) and available disk space.
        If either limit is exceeded the method applies a sequence of retention
        tightenings — each step is evaluated before the next is attempted.
        A single VACUUM is run at the end if any steps were taken.

        Parameters
        ----------
        max_bytes:
            Maximum allowed DB size in bytes.  ``None`` skips the DB-size
            check (min_free_bytes alone governs).
        min_free_bytes:
            Minimum required free disk space in bytes.  ``None`` skips the
            free-space check.

        Returns
        -------
        dict with keys:
            ``triggered`` (bool), ``steps`` (list[str]),
            ``size_before`` (int), ``size_after`` (int).
        """

        # Compute current DB size (main file + WAL).
        def _db_size() -> int:
            total = 0
            try:
                total += self.path.stat().st_size
            except OSError:
                pass
            wal = Path(str(self.path) + "-wal")
            try:
                total += wal.stat().st_size
            except OSError:
                pass
            return total

        # Check if we are currently over budget.
        def _over_budget() -> bool:
            if max_bytes is not None and _db_size() > max_bytes:
                return True
            if min_free_bytes is not None:
                try:
                    free = shutil.disk_usage(self.path.parent).free
                except OSError:
                    free = 0
                if free < min_free_bytes:
                    return True
            return False

        size_before = _db_size()
        if not _over_budget():
            return {"triggered": False}

        steps: List[str] = []

        # Deterministic ladder — raw first (3 steps), then rollup (2 steps).
        # Each step applies retention() and re-evaluates the budget; we stop
        # as soon as we are back within limits.
        # rollup retention stays at the existing "90d" default while we
        # tighten raw; raw stays at "6h" (lower bound) while we tighten rollup.
        ladder: List[Tuple[str, str, str]] = [
            ("raw_24h", "24h", "90d"),
            ("raw_12h", "12h", "90d"),
            ("raw_6h", "6h", "90d"),
            ("rollup_30d", "6h", "30d"),
            ("rollup_14d", "6h", "14d"),
        ]

        for step_name, raw_arg, rollup_arg in ladder:
            if not _over_budget():
                break
            try:
                self.retention(raw_arg, rollup_arg)
            except (sqlite3.Error, OSError) as exc:
                # Do not silently swallow: a recurring retention failure would
                # otherwise let the DB grow unbounded with no trace.
                logger.warning(
                    "DBサイズガード: retention 失敗 (%s): %s", step_name, exc
                )
                self.record_event(
                    "db_guard_error", f"{step_name}: retention 失敗: {exc}"
                )
            after = _db_size()
            detail = f"{step_name}: DB={after // (1024 * 1024)}MB"
            self.record_event("db_guard", detail)
            steps.append(step_name)

        # Single VACUUM to reclaim freed pages (rare — budget exceeded only
        # when the automated retention has not yet run for a long time).
        try:
            self.conn.execute("VACUUM")
            self.conn.commit()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("DBサイズガード: VACUUM 失敗: %s", exc)

        size_after = _db_size()
        return {
            "triggered": True,
            "steps": steps,
            "size_before": size_before,
            "size_after": size_after,
        }

    def record_event(self, kind: str, detail: str = "") -> None:
        """Insert one row into ``ops_events`` immediately (no buffering)."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO ops_events (ts, kind, detail) VALUES (?, ?, ?)",
                (time.time(), kind, detail),
            )

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a key-value pair in the ``meta`` table."""
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def meta(self) -> Dict[str, str]:
        """Return all rows from the ``meta`` table as a plain dict."""
        rows = self.conn.execute("SELECT key, value FROM meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def series(
        self,
        table: str,
        column: str,
        *,
        subject_col: Optional[str] = None,
        subject: Any = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> List[Tuple[float, float]]:
        """Return ``(ts, value)`` pairs from *table* for *column*.

        ``sys_rollup_1m`` uses ``ts_min`` as its timestamp column; all others
        use ``ts``.  The caller does not need to know which.

        When *subject_col* and *subject* are both given, the query is filtered
        to rows where ``subject_col = subject``.
        """
        _rollup_tables = {"sys_rollup_1m", "proc_rollup_1m", "cg_rollup_1m"}
        ts_col = "ts_min" if table in _rollup_tables else "ts"
        q = f"SELECT {ts_col}, {column} FROM {table} WHERE {column} IS NOT NULL"
        params: List[Any] = []

        if subject_col is not None and subject is not None:
            q += f" AND {subject_col} = ?"
            params.append(subject)
        if since is not None:
            q += f" AND {ts_col} >= ?"
            params.append(since)
        if until is not None:
            q += f" AND {ts_col} <= ?"
            params.append(until)
        q += f" ORDER BY {ts_col}"

        return [(r[0], r[1]) for r in self.conn.execute(q, params)]

    def rows(
        self,
        table: str,
        *,
        since: Optional[float] = None,
        until: Optional[float] = None,
        where: Optional[str] = None,
        params: tuple = (),
    ) -> List[sqlite3.Row]:
        """Return raw rows from *table* with optional time and custom filters.

        On a read-only Storage the result is cached by query arguments so that
        several rules sharing the same window do not each re-scan the table.
        The returned list is a shared read-only view — callers must not mutate
        it (``sqlite3.Row`` itself is immutable).
        """
        cache_key = (table, since, until, where, params)
        if self._rows_cache is not None:
            cached = self._rows_cache.get(cache_key)
            if cached is not None:
                return cached

        # Rollup tables use ts_min; registry/meta tables have no timestamp column,
        # so time filtering and ordering do not apply to them.
        _rollup_tables = {"sys_rollup_1m", "proc_rollup_1m", "cg_rollup_1m"}
        _no_ts_tables = {"proc_registry", "cgroup_registry", "meta"}
        if table in _no_ts_tables:
            ts_col = None
        else:
            ts_col = "ts_min" if table in _rollup_tables else "ts"
        if ts_col is None and (since is not None or until is not None):
            raise ValueError(
                f"テーブル {table} には時刻列がないため since/until は使えません"
            )
        q = f"SELECT * FROM {table} WHERE 1=1"
        p: List[Any] = []

        if since is not None:
            q += f" AND {ts_col} >= ?"
            p.append(since)
        if until is not None:
            q += f" AND {ts_col} <= ?"
            p.append(until)
        if where:
            q += f" AND ({where})"
            p.extend(params)
        if ts_col is not None:
            q += f" ORDER BY {ts_col}"

        result = self.conn.execute(q, p).fetchall()
        if self._rows_cache is not None:
            self._rows_cache[cache_key] = result
        return result

    def counts(self) -> Dict[str, Any]:
        """Return per-table row counts and the DB file size in bytes."""
        result: Dict[str, Any] = {}
        tables = [
            "meta",
            "proc_registry",
            "cgroup_registry",
            "sys_samples",
            "cg_samples",
            "proc_samples",
            "disk_samples",
            "dir_samples",
            "spike_events",
            "ops_events",
            "sys_rollup_1m",
            "proc_rollup_1m",
            "cg_rollup_1m",
        ]
        for tbl in tables:
            row = self.conn.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()
            result[tbl] = row["c"]
        try:
            result["db_bytes"] = self.path.stat().st_size
        except OSError:
            result["db_bytes"] = 0
        return result

    # ------------------------------------------------------------------
    # Registry upsert helpers (used by T03 samplers)
    # ------------------------------------------------------------------

    def proc_id_for(
        self,
        name: str,
        cmdline_hash: str,
        cmdline_sample: str,
        ts: float,
    ) -> int:
        """Upsert a process into ``proc_registry`` and return its ``proc_id``.

        Uses an in-memory cache so repeated calls for the same
        ``(name, cmdline_hash)`` within a session cost no SQL round-trips.
        ``last_seen`` is updated in the DB every call (but the cache avoids
        re-reading the ID each time).
        """
        key = (name, cmdline_hash)
        if key in self._proc_cache:
            # Update last_seen in DB (infrequent; acceptable cost)
            self.conn.execute(
                "UPDATE proc_registry SET last_seen=? WHERE name=? AND cmdline_hash=?",
                (ts, name, cmdline_hash),
            )
            return self._proc_cache[key]

        # Try to insert; if the unique key already exists, update instead.
        self.conn.execute(
            "INSERT INTO proc_registry (name, cmdline_hash, cmdline_sample, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name, cmdline_hash) DO UPDATE SET last_seen=excluded.last_seen",
            (name, cmdline_hash, cmdline_sample, ts, ts),
        )
        row = self.conn.execute(
            "SELECT proc_id FROM proc_registry WHERE name=? AND cmdline_hash=?",
            (name, cmdline_hash),
        ).fetchone()
        proc_id: int = row["proc_id"]
        self._proc_cache[key] = proc_id
        return proc_id

    def cg_id_for(self, unit_path: str, ts: float) -> int:
        """Upsert a cgroup unit into ``cgroup_registry`` and return ``cg_id``.

        ``last_seen`` is refreshed at most once per
        ``_CG_LAST_SEEN_THROTTLE_SEC`` seconds per unit to bound the write rate
        on long recordings (the column is metadata that nothing reads for logic).
        """
        if unit_path in self._cg_cache:
            # Throttle the last_seen rewrite: it is metadata nothing reads for
            # logic, so there is no need to UPDATE on every tick for every unit.
            last_written = self._cg_last_seen_written.get(unit_path, 0.0)
            if ts - last_written >= _CG_LAST_SEEN_THROTTLE_SEC:
                self.conn.execute(
                    "UPDATE cgroup_registry SET last_seen=? WHERE unit_path=?",
                    (ts, unit_path),
                )
                self._cg_last_seen_written[unit_path] = ts
            return self._cg_cache[unit_path]

        self.conn.execute(
            "INSERT INTO cgroup_registry (unit_path, first_seen, last_seen) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(unit_path) DO UPDATE SET last_seen=excluded.last_seen",
            (unit_path, ts, ts),
        )
        row = self.conn.execute(
            "SELECT cg_id FROM cgroup_registry WHERE unit_path=?", (unit_path,)
        ).fetchone()
        cg_id: int = row["cg_id"]
        self._cg_cache[unit_path] = cg_id
        self._cg_last_seen_written[unit_path] = ts
        return cg_id

    # ------------------------------------------------------------------
    # Registry reverse lookups (id -> human label)
    # ------------------------------------------------------------------

    def proc_name(self, proc_id: int) -> str:
        """Return the registered process name for *proc_id*.

        Falls back to ``"proc_{proc_id}"`` when the id is not in
        ``proc_registry``.  Centralises the name lookup that the diagnosis
        rules previously inlined as raw SQL.
        """
        row = self.conn.execute(
            "SELECT name FROM proc_registry WHERE proc_id = ?", (proc_id,)
        ).fetchone()
        return row["name"] if row else f"proc_{proc_id}"

    def cg_unit_path(self, cg_id: int) -> str:
        """Return the registered cgroup unit path for *cg_id*.

        Falls back to ``str(cg_id)`` when the id is not in ``cgroup_registry``.
        """
        row = self.conn.execute(
            "SELECT unit_path FROM cgroup_registry WHERE cg_id = ?", (cg_id,)
        ).fetchone()
        return row["unit_path"] if row else str(cg_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Flush remaining buffer and close the connection."""
        if not self._read_only:
            self.flush(reason="close")
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_ops_event_direct(self, kind: str, detail: str) -> None:
        """Insert an ops_event without going through the public buffer."""
        self.conn.execute(
            "INSERT INTO ops_events (ts, kind, detail) VALUES (?, ?, ?)",
            (time.time(), kind, detail),
        )
        self.conn.commit()


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _avg(vals: List[float]) -> Optional[float]:
    """Return arithmetic mean, or None for an empty list."""
    if not vals:
        return None
    return sum(vals) / len(vals)


def _p95(vals: List[float]) -> Optional[float]:
    """Return the 95th-percentile value, or None for an empty list."""
    if not vals:
        return None
    sorted_vals = sorted(vals)
    idx = int(len(sorted_vals) * 0.95)
    idx = min(idx, len(sorted_vals) - 1)
    return sorted_vals[idx]
