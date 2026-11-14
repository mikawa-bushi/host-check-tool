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
import shutil
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from .core.util import MiB, parse_duration

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4

_Migration = Union[str, Callable[[sqlite3.Connection], None]]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of *table* (empty when the table does not exist)."""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(
    table: str, column: str, decl: str
) -> Callable[[sqlite3.Connection], None]:
    """Migration step adding *column* to *table* only when it is missing.

    ``ALTER TABLE … ADD COLUMN`` is not idempotent and SQLite runs DDL outside
    the migration's transaction, so a crash between the ALTER and the version
    bump would otherwise make the next open fail with "duplicate column".
    """

    def step(conn: sqlite3.Connection) -> None:
        if column not in _table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    return step


# Forward migrations keyed by the schema version they produce.  Each value is a
# list of idempotent steps (SQL text or a callable taking the connection) that
# upgrade a DB from (version - 1) to version, applied in order on the write
# path by ``_ensure_schema_version``.  ``_SCHEMA`` already contains the
# *current* shape (and is executed before migrating, so new tables already
# exist), so a brand-new DB never runs these — they exist only to bring an
# older on-disk DB forward.
_MIGRATIONS: Dict[int, List[_Migration]] = {
    2: [
        "CREATE INDEX IF NOT EXISTS idx_ops_events_kind_ts ON ops_events(kind, ts)",
    ],
    3: [
        "CREATE TABLE IF NOT EXISTS thermal_samples ("
        " ts REAL PRIMARY KEY, temp_max_c REAL,"
        " cpu_freq_cur_mhz REAL, cpu_freq_max_mhz REAL)",
    ],
    4: [
        _add_column("disk_samples", "inodes_total", "INTEGER"),
        _add_column("disk_samples", "inodes_used", "INTEGER"),
        _add_column("disk_samples", "read_only", "INTEGER"),
        _add_column("proc_registry", "fd_soft_limit", "INTEGER"),
        _add_column("proc_registry", "fd_hard_limit", "INTEGER"),
    ],
}


class StorageVersionError(RuntimeError):
    """Raised when a DB's schema version is newer than this procdiag supports."""


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
  fd_soft_limit INTEGER, fd_hard_limit INTEGER,
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
  inodes_total INTEGER, inodes_used INTEGER, read_only INTEGER,
  PRIMARY KEY (ts, mountpoint)
);
CREATE TABLE IF NOT EXISTS dir_samples (
  ts REAL, path TEXT, size_bytes INTEGER, is_estimate INTEGER,
  PRIMARY KEY (ts, path)
);
-- サーマル / CPU 周波数 (Tier-3, capability-gated; センサ無し環境では空)
CREATE TABLE IF NOT EXISTS thermal_samples (
  ts REAL PRIMARY KEY,
  temp_max_c REAL, cpu_freq_cur_mhz REAL, cpu_freq_max_mhz REAL
);

-- プロセス数・ゾンビ数 (Tier-2b の走査ごと)
CREATE TABLE IF NOT EXISTS proc_counts (
  ts REAL PRIMARY KEY, total INTEGER, zombies INTEGER
);
-- systemd サービスの自動再起動 / failed 遷移
CREATE TABLE IF NOT EXISTS service_events (
  ts REAL, unit TEXT, kind TEXT, n_restarts INTEGER, result TEXT
);
-- カーネルログの異常行 (I/O エラー・ハングタスク等)
CREATE TABLE IF NOT EXISTS kernel_events (
  ts REAL, kind TEXT, message TEXT
);
-- 記録セッション (開始・生存・正常停止; 予期しない再起動の検出に使う)
CREATE TABLE IF NOT EXISTS sessions (
  session_id INTEGER PRIMARY KEY,
  started_ts REAL, boot_time REAL, hostname TEXT, procdiag_version TEXT,
  last_alive_ts REAL, stopped_ts REAL
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
-- ops_events は kind での絞り込み(gap/throttle 集計)と ts での retention 削除が
-- 主アクセスだが PK が無く全表走査になる。索引を張って走査を避ける。
CREATE INDEX IF NOT EXISTS idx_ops_events_kind_ts ON ops_events(kind, ts);

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
        "inodes_total",
        "inodes_used",
        "read_only",
    ],
    "dir_samples": [
        "ts",
        "path",
        "size_bytes",
        "is_estimate",
    ],
    "thermal_samples": [
        "ts",
        "temp_max_c",
        "cpu_freq_cur_mhz",
        "cpu_freq_max_mhz",
    ],
    "proc_counts": ["ts", "total", "zombies"],
    "service_events": ["ts", "unit", "kind", "n_restarts", "result"],
    "kernel_events": ["ts", "kind", "message"],
    # A sampler's read position, committed in the same flush as its rows.
    "meta": ["key", "value"],
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

_COLUMN_SETS: Dict[str, frozenset] = {t: frozenset(c) for t, c in TABLE_COLUMNS.items()}


def make_row(table: str, **values: Any) -> tuple:
    """Build a positional ``SampleBatch`` row for *table* from named columns.

    Samplers name every column instead of hand-ordering a tuple, so adding or
    reordering a column in ``TABLE_COLUMNS`` can never silently shift values
    into the wrong column: a missing or unknown name raises immediately.
    """
    cols = TABLE_COLUMNS[table]
    if values.keys() != _COLUMN_SETS[table]:
        missing = [c for c in cols if c not in values]
        unknown = [k for k in values if k not in _COLUMN_SETS[table]]
        raise ValueError(
            f"{table}: 列が一致しません (不足: {missing}, 不明: {unknown})"
        )
    return tuple(values[c] for c in cols)


# ---------------------------------------------------------------------------
# Table groups — one place that knows which tables share which policy
# ---------------------------------------------------------------------------

# Raw high-frequency samples: kept for ``retention.raw`` (e.g. 48h).
RAW_TABLES = ("sys_samples", "cg_samples", "proc_samples", "proc_counts")
# 1-minute rollups: keyed by ``ts_min`` and kept for ``retention.rollup``.
_ROLLUP_TABLES = ("sys_rollup_1m", "proc_rollup_1m", "cg_rollup_1m")
# Low-frequency raw samples that are cheap enough to keep as long as rollups.
_LONG_LIVED_TABLES = (
    "disk_samples",
    "dir_samples",
    "thermal_samples",
    "service_events",
    "kernel_events",
)
# Samplers that legitimately never write on some hosts (no thermal sensor, no
# watch_dirs configured).
OPTIONAL_SAMPLER_TABLES = frozenset({"thermal_samples", "dir_samples"})
# Tables without a ``ts`` column (lookups, and the tiny session history that is
# kept for good so reboots can be compared across the whole recording).
_NO_TS_TABLES = ("meta", "proc_registry", "cgroup_registry", "sessions")
# Every table, in the order ``counts()`` reports them.
_ALL_TABLES = (
    *_NO_TS_TABLES,
    *RAW_TABLES,
    *_LONG_LIVED_TABLES,
    "spike_events",
    "ops_events",
    *_ROLLUP_TABLES,
)


def _ts_column(table: str) -> Optional[str]:
    """Return the timestamp column of *table* (``None`` for lookup tables)."""
    if table in _NO_TS_TABLES:
        return None
    return "ts_min" if table in _ROLLUP_TABLES else "ts"


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
        try:
            self._prepare(read_only)
        except BaseException:
            # e.g. StorageVersionError: do not leak the connection to the caller.
            self.conn.close()
            raise

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
    # Schema versioning / migrations
    # ------------------------------------------------------------------

    def _prepare(self, read_only: bool) -> None:
        """Set pragmas and bring the schema up to date (write) or check it (read)."""
        if read_only:
            # A read-only connection cannot switch the journal mode (it raises
            # on a DB that is not already WAL, e.g. a copy made by another
            # tool); a WAL DB is read in WAL mode regardless.
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self._check_read_only_version()
            return

        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._ensure_schema_version()

    def _ensure_schema_version(self) -> None:
        """Read the stored schema version and migrate forward if needed.

        Write path only.  A brand-new DB (no stored version) is stamped with the
        current version.  An older DB has each pending migration in
        ``_MIGRATIONS`` applied in order.  A DB written by a *newer* procdiag is
        refused outright so a future schema is never silently mis-read.
        """
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()
            return

        stored = int(row["value"])
        if stored == SCHEMA_VERSION:
            return
        if stored > SCHEMA_VERSION:
            raise StorageVersionError(
                f"DB のスキーマ版 {stored} はこの procdiag (版 {SCHEMA_VERSION}) "
                "より新しいため開けません。procdiag を更新してください。"
            )

        for target in range(stored + 1, SCHEMA_VERSION + 1):
            for step in _MIGRATIONS.get(target, []):
                if callable(step):
                    step(self.conn)
                else:
                    self.conn.execute(step)
            self.conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(target),),
            )
        self.conn.commit()
        logger.info("スキーマを版 %d から %d へ移行しました", stored, SCHEMA_VERSION)

    def _check_read_only_version(self) -> None:
        """Refuse a read-only DB written by a *newer* procdiag.

        Older DBs are tolerated silently: a read-only connection cannot migrate,
        and past migrations only add indexes/columns that reads survive without.
        """
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.Error:
            return  # not a procdiag DB / no meta table — let reads fail naturally
        if row is not None and int(row["value"]) > SCHEMA_VERSION:
            raise StorageVersionError(
                f"DB のスキーマ版 {int(row['value'])} はこの procdiag "
                f"(版 {SCHEMA_VERSION}) より新しいため読めません。"
            )

    # ------------------------------------------------------------------
    # Write path (recorder-only)
    # ------------------------------------------------------------------

    def buffer(self, table: str, rows: List[tuple]) -> None:
        """Accumulate rows in the in-memory buffer for a later flush.

        Each row is INSERTed positionally in ``TABLE_COLUMNS[table]`` order, so a
        row whose width drifted from that map would silently shift columns or
        raise a cryptic SQLite error at flush.  Reject the mismatch here with a
        clear, table-named message instead (issue v0.3 §2-6); combined with
        ``test_table_columns_match_schema`` (§2-2) this fences the write path
        against schema/sampler drift.
        """
        cols = TABLE_COLUMNS.get(table)
        if cols is not None:
            width = len(cols)
            for row in rows:
                if len(row) != width:
                    raise ValueError(
                        f"{table}: 行の列数 {len(row)} が TABLE_COLUMNS の "
                        f"{width} 列と一致しません"
                    )
        self._buffer[table].extend(rows)

    def flush(self, *, reason: str = "interval") -> int:
        """Write all buffered rows to SQLite in a single transaction.

        Returns the total number of rows written.  When ``reason`` is
        ``"spike"`` or ``"signal"`` an ops_event is also recorded so
        operators can see why a flush happened mid-interval.
        """
        if not self._buffer:
            if reason in ("spike", "signal"):
                self.record_event(reason, "バッファ空でフラッシュ")
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
                self._insert_event("flush", f"reason={reason}")
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

        if watermark > current_minute_floor:
            # Wall clock moved backward (NTP step / suspend / manual set) since
            # the watermark was written: it now sits in the "future", so the
            # normal `>= floor` guard below would skip every rolled-back minute
            # forever.  Reset the watermark to now and surface the event so the
            # observer can flag the affected window.
            self.record_event(
                "clock_skew",
                f"rollup watermark {watermark:.0f} > 現在分 {current_minute_floor}"
                " — 時計巻き戻しを検出し watermark を補正",
            )
            self.set_meta("rollup_watermark", str(current_minute_floor))
            return

        if watermark >= current_minute_floor:
            return  # nothing to roll up yet (only an in-progress minute)

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

            # sys_rollup_1m needs a p95, which SQLite cannot aggregate, so this
            # one table is rolled up in Python (it is 1 row/s — small).
            for bucket, rows in minute_buckets.items():
                cpu_vals = _non_null(rows, "cpu_total")
                mem_vals = _non_null(rows, "mem_used")
                swap_out = _non_null(rows, "swap_out")
                mem_avg = _avg(mem_vals)
                self.conn.execute(
                    "INSERT OR REPLACE INTO sys_rollup_1m "
                    "(ts_min, cpu_avg, cpu_max, cpu_p95, cpu_max_core_max, "
                    " mem_used_avg, mem_used_max, psi_cpu_max, psi_mem_max, "
                    " psi_io_max, swap_out_sum) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bucket,
                        _avg(cpu_vals),
                        _max(cpu_vals),
                        _p95(cpu_vals),
                        _max(_non_null(rows, "cpu_max_core")),
                        int(mem_avg) if mem_avg is not None else None,
                        _max(mem_vals),
                        _max(_non_null(rows, "psi_cpu_some10")),
                        _max(_non_null(rows, "psi_mem_some10")),
                        _max(_non_null(rows, "psi_io_some10")),
                        sum(swap_out) if swap_out else None,
                    ),
                )

        # --- proc_rollup_1m (SQL aggregation) ---
        # proc_samples is the dominant volume (processes × ticks); aggregate it
        # in SQLite (AVG/MAX) rather than pulling every row into Python.  No
        # percentile column here, so a single INSERT…SELECT…GROUP BY suffices.
        # AVG/MAX return NULL for an all-NULL group, matching the previous
        # Python ``_avg([]) -> None`` / ``max(...) if vals else None`` semantics.
        self.conn.execute(
            "INSERT OR REPLACE INTO proc_rollup_1m "
            "(ts_min, proc_id, cpu_avg, cpu_max, rss_avg, rss_max, "
            " fds_max, threads_max) "
            "SELECT CAST(ts / 60 AS INTEGER) * 60.0 AS ts_min, proc_id, "
            "       AVG(cpu_pct), MAX(cpu_pct), "
            "       CAST(AVG(rss) AS INTEGER), MAX(rss), "
            "       MAX(num_fds), MAX(num_threads) "
            "FROM proc_samples "
            "WHERE ts >= ? AND ts < ? "
            "GROUP BY ts_min, proc_id",
            (watermark, current_minute_floor),
        )

        # --- cg_rollup_1m (SQL aggregation) ---
        # COALESCE(SUM(...), 0) reproduces the previous Python ``sum(empty) == 0``
        # for the delta columns, while mem AVG/MAX stay NULL on an all-NULL group.
        self.conn.execute(
            "INSERT OR REPLACE INTO cg_rollup_1m "
            "(ts_min, cg_id, cpu_usec_sum, mem_avg, mem_max, "
            " io_rbytes_sum, io_wbytes_sum) "
            "SELECT CAST(ts / 60 AS INTEGER) * 60.0 AS ts_min, cg_id, "
            "       COALESCE(SUM(cpu_usec_delta), 0), "
            "       CAST(AVG(mem_current) AS INTEGER), MAX(mem_current), "
            "       COALESCE(SUM(io_rbytes_delta), 0), "
            "       COALESCE(SUM(io_wbytes_delta), 0) "
            "FROM cg_samples "
            "WHERE ts >= ? AND ts < ? "
            "GROUP BY ts_min, cg_id",
            (watermark, current_minute_floor),
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

        with self.conn:
            raw_deleted = self._delete_before(RAW_TABLES, raw_cutoff)
        with self.conn:
            rollup_deleted = self._delete_before(
                _ROLLUP_TABLES + _LONG_LIVED_TABLES, rollup_cutoff
            )

        return {"raw_deleted": raw_deleted, "rollup_deleted": rollup_deleted}

    def _delete_before(self, tables: Iterable[str], cutoff: float) -> int:
        """Delete rows older than *cutoff* from *tables*; return the row count."""
        deleted = 0
        for tbl in tables:
            cur = self.conn.execute(
                f"DELETE FROM {tbl} WHERE {_ts_column(tbl)} < ?", (cutoff,)
            )
            deleted += cur.rowcount
        return deleted

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
            detail = f"{step_name}: DB={after // MiB}MB"
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
            self._insert_event(kind, detail)

    def record_spike(
        self,
        *,
        trigger_type: str,
        psi_snapshot: str,
        top_processes: str,
        top_cgroups: str,
    ) -> None:
        """Commit one ``spike_events`` row immediately.

        The JSON payload columns are encoded by :mod:`procdiag.spikes`; the row
        is self-contained (it carries the culprit snapshot inline), so a single
        atomic insert is all the durability a spike needs.
        """
        with self.conn:
            self.conn.execute(
                "INSERT INTO spike_events "
                "(ts, trigger_type, psi_snapshot, top_processes, top_cgroups) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), trigger_type, psi_snapshot, top_processes, top_cgroups),
            )

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a key-value pair in the ``meta`` table (caller commits)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )

    def write_meta(self, entries: Dict[str, str]) -> None:
        """Upsert several ``meta`` entries and commit them together."""
        with self.conn:
            for key, value in entries.items():
                self.set_meta(key, value)

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
        ts_col = _ts_column(table)
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

        # Registry/meta tables have no timestamp column, so time filtering and
        # ordering do not apply to them.
        ts_col = _ts_column(table)
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
        for tbl in _ALL_TABLES:
            row = self.conn.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()
            result[tbl] = row["c"]
        try:
            result["db_bytes"] = self.path.stat().st_size
        except OSError:
            result["db_bytes"] = 0
        return result

    def time_bounds(
        self,
        table: str,
        since: Optional[float] = None,
        until: Optional[float] = None,
        *,
        before: Optional[float] = None,
    ) -> Optional[Tuple[float, float]]:
        """Return ``(min_ts, max_ts)`` of *table*, or ``None`` when it is empty.

        *since* / *until* restrict the rows like :meth:`rows` (inclusive);
        *before* is an exclusive upper bound.
        Aggregated in SQL so callers that only need the recorded span (status,
        window detection) never materialise a long recording into Python.
        """
        ts_col = _ts_column(table)
        q = f"SELECT MIN({ts_col}) AS lo, MAX({ts_col}) AS hi FROM {table} WHERE 1=1"
        params: List[Any] = []
        if since is not None:
            q += f" AND {ts_col} >= ?"
            params.append(since)
        if until is not None:
            q += f" AND {ts_col} <= ?"
            params.append(until)
        if before is not None:
            q += f" AND {ts_col} < ?"
            params.append(before)
        row = self.conn.execute(q, params).fetchone()
        if row is None or row["lo"] is None:
            return None
        return float(row["lo"]), float(row["hi"])

    def self_cpu_stats(
        self, since: Optional[float] = None, until: Optional[float] = None
    ) -> Tuple[Optional[float], Optional[float]]:
        """Return ``(avg, max)`` of procdiag's own CPU% in ``sys_samples``.

        Both are ``None`` when no sample carries a ``self_cpu`` value.
        """
        q = (
            "SELECT AVG(self_cpu) AS avg, MAX(self_cpu) AS max FROM sys_samples "
            "WHERE self_cpu IS NOT NULL"
        )
        params: List[Any] = []
        if since is not None:
            q += " AND ts >= ?"
            params.append(since)
        if until is not None:
            q += " AND ts <= ?"
            params.append(until)
        row = self.conn.execute(q, params).fetchone()
        return row["avg"], row["max"]

    def missing_io_ratio(self, since: float, until: float) -> float:
        """Fraction of ``proc_samples`` rows whose I/O counters were unreadable.

        Non-root recording cannot read other users' ``io_counters``; the ratio
        tells the report how much the I/O rules can be trusted.  ``0.0`` when
        there are no process samples in the range.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN io_read_delta IS NULL THEN 1 ELSE 0 END) AS missing "
            "FROM proc_samples WHERE ts >= ? AND ts <= ?",
            (since, until),
        ).fetchone()
        if not row or not row["total"]:
            return 0.0
        return (row["missing"] or 0) / row["total"]

    def recent_events(self, limit: int) -> List[sqlite3.Row]:
        """Return the newest *limit* ``ops_events`` rows, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM ops_events ORDER BY ts DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return rows[::-1]

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

    def proc_identities(self) -> Dict[int, Tuple[str, str]]:
        """``{proc_id: (name, cmdline_hash)}`` for every registered process.

        The pair is ``proc_registry``'s UNIQUE key and is stable across DBs.
        """
        return {
            r["proc_id"]: (r["name"], r["cmdline_hash"])
            for r in self.rows("proc_registry")
        }

    def proc_first_seen(self, proc_id: int) -> Optional[float]:
        """Return the ``first_seen`` timestamp for *proc_id*, or None.

        Used by MEM-LEAK-01 to tell whether a leaking process identity predates
        the current boot (i.e. the leak recurred across a reboot).
        """
        row = self.conn.execute(
            "SELECT first_seen FROM proc_registry WHERE proc_id = ?", (proc_id,)
        ).fetchone()
        if row is None or row["first_seen"] is None:
            return None
        return float(row["first_seen"])

    def window_bounds(self) -> Optional[tuple[float, float, bool]]:
        """Return ``(min_ts, max_ts, used_rollup_fallback)`` or None if empty.

        Prefers raw ``sys_samples``; falls back to ``sys_rollup_1m`` (with the
        flag set) so diagnosis can derive its observation window without reaching
        into ``Storage.conn`` directly.
        """
        bounds = self.time_bounds("sys_samples")
        if bounds is not None:
            return bounds[0], bounds[1], False
        bounds = self.time_bounds("sys_rollup_1m")
        if bounds is not None:
            return bounds[0], bounds[1], True
        return None

    def system_bounds(
        self,
        since: Optional[float] = None,
        until: Optional[float] = None,
        *,
        before: Optional[float] = None,
    ) -> Optional[Tuple[float, float]]:
        """Return ``(min_ts, max_ts)`` over ``sys_samples`` and ``sys_rollup_1m``
        combined, or ``None`` when neither has a row in range.

        Bounds as in :meth:`time_bounds`.
        """
        found = [
            bounds
            for table in ("sys_samples", "sys_rollup_1m")
            if (bounds := self.time_bounds(table, since, until, before=before))
            is not None
        ]
        if not found:
            return None
        return min(lo for lo, _ in found), max(hi for _, hi in found)

    def proc_fd_limits(self, proc_id: int) -> Optional[Tuple[int, int]]:
        """Return the recorded ``(soft, hard)`` RLIMIT_NOFILE for *proc_id*.

        ``None`` when it was never readable (other user's process while
        recording non-root) or the DB predates schema v4.
        """
        if not self.has_column("proc_registry", "fd_soft_limit"):
            return None
        row = self.conn.execute(
            "SELECT fd_soft_limit AS soft, fd_hard_limit AS hard "
            "FROM proc_registry WHERE proc_id = ?",
            (proc_id,),
        ).fetchone()
        if row is None or row["soft"] is None:
            return None
        return int(row["soft"]), int(row["hard"])

    def cg_unit_path(self, cg_id: int) -> str:
        """Return the registered cgroup unit path for *cg_id*.

        Falls back to ``str(cg_id)`` when the id is not in ``cgroup_registry``.
        """
        row = self.conn.execute(
            "SELECT unit_path FROM cgroup_registry WHERE cg_id = ?", (cg_id,)
        ).fetchone()
        return row["unit_path"] if row else str(cg_id)

    def set_proc_fd_limits(self, proc_id: int, soft: int, hard: int) -> None:
        """Record a process identity's FD limits (caller's next flush commits)."""
        self.conn.execute(
            "UPDATE proc_registry SET fd_soft_limit=?, fd_hard_limit=? "
            "WHERE proc_id=?",
            (soft, hard, proc_id),
        )

    # ------------------------------------------------------------------
    # Recording sessions (unclean-shutdown detection)
    # ------------------------------------------------------------------

    def start_session(
        self, *, boot_time: float, hostname: str, procdiag_version: str
    ) -> int:
        """Open a ``sessions`` row for this record run and return its id."""
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO sessions (started_ts, boot_time, hostname, "
                "procdiag_version, last_alive_ts) VALUES (?, ?, ?, ?, ?)",
                (now, boot_time, hostname, procdiag_version, now),
            )
        return int(cur.lastrowid)

    def touch_session(self, session_id: int) -> None:
        """Advance the session's ``last_alive_ts`` (a cheap heartbeat).

        After a power cut this is the last moment the recorder was known to be
        running — the best estimate of when the host went down.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET last_alive_ts=? WHERE session_id=?",
                (time.time(), session_id),
            )

    def end_session(self, session_id: int) -> None:
        """Mark the session as stopped cleanly (SIGTERM/SIGINT/duration)."""
        now = time.time()
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET last_alive_ts=?, stopped_ts=? WHERE session_id=?",
                (now, now, session_id),
            )

    def sessions(self) -> List[sqlite3.Row]:
        """All recording sessions, oldest first ([] for a pre-v4 DB)."""
        if not self.has_table("sessions"):
            return []
        return self.conn.execute(
            "SELECT * FROM sessions ORDER BY started_ts"
        ).fetchall()

    # ------------------------------------------------------------------
    # Schema introspection (old DBs opened read-only lack newer objects)
    # ------------------------------------------------------------------

    def has_table(self, table: str) -> bool:
        """Whether *table* exists — False for objects added after this DB's version."""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    def has_column(self, table: str, column: str) -> bool:
        """Whether *table* has *column* (see :meth:`has_table`)."""
        return column in _table_columns(self.conn, table)

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

    def _insert_event(self, kind: str, detail: str) -> None:
        """Insert an ops_event inside the caller's transaction (no commit)."""
        self.conn.execute(
            "INSERT INTO ops_events (ts, kind, detail) VALUES (?, ?, ?)",
            (time.time(), kind, detail),
        )


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _non_null(rows: List[sqlite3.Row], col: str) -> List[Any]:
    """Return the non-NULL values of *col* across *rows*."""
    return [r[col] for r in rows if r[col] is not None]


def _max(vals: List[Any]) -> Optional[Any]:
    """Return ``max(vals)``, or None for an empty list."""
    return max(vals) if vals else None


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
