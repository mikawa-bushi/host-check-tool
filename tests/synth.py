"""Synthetic DB builder for procdiag tests.

Provides helpers that populate a :class:`~procdiag.storage.Storage` instance
with realistic-looking time-series data so that diagnosis rule tests can
exercise both the "detect" and "do not detect" paths without running a live
``record`` session.

Shared-asset contract for Phase 4 (T10–T12)
--------------------------------------------
This module is used by T06 (MEM-LEAK-01, DISK-FULL-01, CPU-PERIODIC-01,
CPU-SINGLECORE-01) and will be reused by T10 (MEM-PRESSURE-01,
SWAP-THRASH-01, CPU-SATURATION-01), T11 (IO-BOTTLENECK-01, DISK-GROWTH-01,
DISK-JOURNAL-01), and T12 (FD-LEAK-01, THREAD-LEAK-01, ZOMBIE-01).

**Contribution rules for Phase 4 agents:**

- **Append new helper functions** for the data patterns your rules need.
- **Do not modify** existing function signatures, docstrings, or default
  parameter values — T06 tests depend on the current behaviour.
- New functions must follow the same convention: accept ``store`` as the
  first positional argument, keyword-only options, and return nothing.
- Each new function should have a one-paragraph docstring explaining the
  data it generates and which rule(s) consume it.
"""

from __future__ import annotations

import math
import random
import time
from pathlib import Path
from typing import Optional

from procdiag.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MB = 1024 * 1024
_GB = 1024**3


def make_db(tmp_path: Path, *, hours: float = 24.0) -> Storage:
    """Create and return an open :class:`Storage` backed by a temp file.

    The schema is initialised (all tables created) and a ``meta`` row with
    ``start_time`` and ``hostname`` is inserted so ``Engine`` can detect a
    non-empty DB.  The caller is responsible for closing the storage.

    Parameters
    ----------
    tmp_path:
        Directory in which to create the SQLite file.
    hours:
        Nominal observation period stored in meta (does not insert any
        samples by itself).
    """
    db_path = tmp_path / "synth.db"
    store = Storage(db_path)
    now = time.time()
    store.set_meta("start_time", str(now - hours * 3600))
    store.set_meta("end_time", str(now))
    store.set_meta("hostname", "synth-host")
    store.conn.commit()
    return store


def add_sys_baseline(
    store: Storage,
    *,
    hours: float,
    cpu: float = 10.0,
    noise: float = 2.0,
    start_ts: Optional[float] = None,
) -> None:
    """Insert synthetic sys_samples and sys_rollup_1m rows.

    Generates one raw sample per minute (approximately) and one rollup row
    per minute over *hours* of simulated time.  CPU values are drawn from a
    Gaussian centred on *cpu* with standard deviation *noise*.

    Parameters
    ----------
    store:
        Target storage instance.
    hours:
        Number of hours to simulate.
    cpu:
        Mean CPU percentage for ``cpu_total``.
    noise:
        Standard deviation of the Gaussian noise added to *cpu*.
    start_ts:
        Start timestamp (POSIX seconds).  Defaults to
        ``time.time() - hours * 3600``.
    """
    if start_ts is None:
        start_ts = time.time() - hours * 3600.0

    rng = random.Random(42)
    n_minutes = int(hours * 60)

    raw_rows: list[tuple] = []
    rollup_rows: list[tuple] = []

    for i in range(n_minutes):
        ts = start_ts + i * 60.0
        cpu_val = max(0.0, min(100.0, cpu + rng.gauss(0, noise)))

        # Raw sample (one per minute for brevity).
        raw_rows.append(
            (
                ts,
                cpu_val,
                cpu_val + rng.uniform(0, 5),  # cpu_max_core slightly higher
                int(2 * _GB + rng.gauss(0, 10 * _MB)),  # mem_used ~2GB
                int(6 * _GB + rng.gauss(0, 10 * _MB)),  # mem_available ~6GB
                0,  # swap_used
                0,  # swap_in
                0,  # swap_out
                cpu_val / 100.0,  # load1
                0.0,  # psi_cpu_some10
                0.0,  # psi_mem_some10
                0.0,  # psi_mem_full10
                0.0,  # psi_io_some10
                0.0,  # psi_io_full10
                0,  # disk_read_bytes
                0,  # disk_write_bytes
                0.1,  # self_cpu
                int(50 * _MB),  # self_rss
            )
        )

        # 1-minute rollup.
        rollup_rows.append(
            (
                ts,  # ts_min
                cpu_val,  # cpu_avg
                cpu_val + rng.uniform(0, 5),  # cpu_max
                cpu_val + rng.uniform(3, 8),  # cpu_p95
                cpu_val + rng.uniform(0, 5),  # cpu_max_core_max
                int(2 * _GB),  # mem_used_avg
                int(2.1 * _GB),  # mem_used_max
                0.0,  # psi_cpu_max
                0.0,  # psi_mem_max
                0.0,  # psi_io_max
                0,  # swap_out_sum
            )
        )

    store.buffer("sys_samples", raw_rows)
    store.buffer("sys_rollup_1m", rollup_rows)
    store.flush(reason="synth")


def add_proc_series(
    store: Storage,
    name: str,
    *,
    rss_start: int,
    rss_slope_per_h: float,
    hours: float,
    cpu_pattern: Optional[float] = None,
    fds_slope: float = 0,
    threads_slope: float = 0,
    start_ts: Optional[float] = None,
) -> int:
    """Insert a synthetic process rollup series.

    Registers the process in ``proc_registry`` and inserts rows into
    ``proc_rollup_1m``.  Returns the assigned ``proc_id``.

    Parameters
    ----------
    store:
        Target storage instance.
    name:
        Process name (inserted into ``proc_registry``).
    rss_start:
        Initial RSS in bytes.
    rss_slope_per_h:
        Rate of RSS increase in bytes/hour.  Use 0 for a flat series.
    hours:
        Number of hours to simulate.
    cpu_pattern:
        Constant cpu_avg/cpu_max value (0–100).  ``None`` defaults to 10.0.
    fds_slope:
        Rate of FD count increase per hour.  0 means constant at 50 FDs.
    threads_slope:
        Rate of thread count increase per hour.  0 means constant at 8.
    start_ts:
        Start timestamp (POSIX seconds).  Defaults to
        ``time.time() - hours * 3600``.
    """
    if start_ts is None:
        start_ts = time.time() - hours * 3600.0

    # Register the process.
    proc_id = store.proc_id_for(
        name=name,
        cmdline_hash=f"hash_{name}",
        cmdline_sample=f"/usr/bin/{name}",
        ts=start_ts,
    )
    store.conn.commit()

    rng = random.Random(name.__hash__() & 0xFFFF)
    cpu_val = cpu_pattern if cpu_pattern is not None else 10.0
    n_minutes = int(hours * 60)

    rollup_rows: list[tuple] = []
    for i in range(n_minutes):
        ts = start_ts + i * 60.0
        elapsed_h = i / 60.0
        rss_avg = int(rss_start + rss_slope_per_h * elapsed_h)
        rss_max = int(rss_avg * 1.05)
        fds = int(50 + fds_slope * elapsed_h)
        threads = int(8 + threads_slope * elapsed_h)
        noise = rng.gauss(0, rss_avg * 0.01)  # 1% noise
        rollup_rows.append(
            (
                ts,
                proc_id,
                cpu_val,
                cpu_val * 1.1,
                max(0, int(rss_avg + noise)),
                max(0, int(rss_max + noise)),
                fds,
                threads,
            )
        )

    store.buffer("proc_rollup_1m", rollup_rows)
    store.flush(reason="synth")
    return proc_id


def add_disk_series(
    store: Storage,
    mountpoint: str,
    *,
    total: int,
    used_start: int,
    daily_growth: int,
    days: float,
    start_ts: Optional[float] = None,
) -> None:
    """Insert synthetic disk_samples for one mountpoint.

    Inserts one row per hour over *days* days so ``extrapolate_full`` has
    enough points to compute a reliable forecast.

    Parameters
    ----------
    store:
        Target storage instance.
    mountpoint:
        Mountpoint string, e.g. ``"/var"``.
    total:
        Partition capacity in bytes.
    used_start:
        Initial used space in bytes.
    daily_growth:
        Bytes added per day (linear growth).
    days:
        Number of days to simulate.
    start_ts:
        Start timestamp (POSIX seconds).  Defaults to
        ``time.time() - days * 86400``.
    """
    if start_ts is None:
        start_ts = time.time() - days * 86400.0

    hourly_growth = daily_growth / 24.0
    n_hours = int(days * 24)

    rows: list[tuple] = []
    for i in range(n_hours):
        ts = start_ts + i * 3600.0
        used = int(used_start + hourly_growth * i)
        used = min(used, total)
        rows.append((ts, mountpoint, total, used))

    store.buffer("disk_samples", rows)
    store.flush(reason="synth")


def add_spike_events(
    store: Storage,
    trigger_type: str,
    times: list[float],
) -> None:
    """Insert ``spike_events`` rows at the given timestamps.

    Parameters
    ----------
    store:
        Target storage instance.
    trigger_type:
        E.g. ``"cpu"`` or ``"storm"``.
    times:
        List of POSIX timestamps at which the spike events occurred.
    """
    rows: list[tuple] = []
    for ts in times:
        rows.append(
            (
                None,  # event_id AUTO
                ts,
                trigger_type,
                "{}",  # psi_snapshot
                "[]",  # top_processes
                "[]",  # top_cgroups
            )
        )

    # spike_events has an auto-increment primary key; insert directly.
    for row in rows:
        store.conn.execute(
            "INSERT INTO spike_events "
            "(event_id, ts, trigger_type, psi_snapshot, top_processes, top_cgroups) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            row,
        )
    store.conn.commit()


def add_dir_series(
    store: Storage,
    path: str,
    *,
    start: int,
    daily_growth: int,
    days: float,
    start_ts: Optional[float] = None,
) -> None:
    """Insert synthetic dir_samples for one watched directory.

    Parameters
    ----------
    store:
        Target storage instance.
    path:
        Directory path, e.g. ``"/var/log"``.
    start:
        Initial size in bytes.
    daily_growth:
        Bytes added per day.
    days:
        Number of days to simulate.
    start_ts:
        Start timestamp (POSIX seconds).  Defaults to
        ``time.time() - days * 86400``.
    """
    if start_ts is None:
        start_ts = time.time() - days * 86400.0

    hourly_growth = daily_growth / 24.0
    n_hours = int(days * 24)

    rows: list[tuple] = []
    for i in range(n_hours):
        ts = start_ts + i * 3600.0
        size = int(start + hourly_growth * i)
        rows.append((ts, path, size, 0))

    store.buffer("dir_samples", rows)
    store.flush(reason="synth")


# ---------------------------------------------------------------------------
# T10 helpers — MEM-PRESSURE-01 / SWAP-THRASH-01 / CPU-SATURATION-01
# ---------------------------------------------------------------------------


def add_mem_pressure_days(
    store: Storage,
    *,
    hours: float,
    psi_high: float = 8.0,
    psi_low: float = 1.0,
    pressure_fraction: float = 0.8,
    swap_out_per_row: int = 1024 * 1024,
    start_ts: Optional[float] = None,
) -> None:
    """Insert sys_rollup_1m rows simulating chronic memory pressure.

    Generates one rollup row per minute over *hours* of simulated time.  On
    days that fall within *pressure_fraction* of the total day count, the
    ``psi_mem_max`` value is set to *psi_high* (above threshold); on the
    remaining days it is set to *psi_low*.  ``swap_out_sum`` is set to
    *swap_out_per_row* on every row so that the total swap-out is non-zero,
    satisfying the MEM-PRESSURE-01 precondition.

    Consumed by: MEM-PRESSURE-01 (detect path).
    """
    if start_ts is None:
        start_ts = time.time() - hours * 3600.0

    from datetime import datetime, timezone

    rng = random.Random(77)
    n_minutes = int(hours * 60)

    # Pre-compute which calendar days should show high pressure.
    all_day_keys: list[str] = []
    for i in range(n_minutes):
        ts = start_ts + i * 60.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        dk = dt.strftime("%Y-%m-%d")
        if not all_day_keys or all_day_keys[-1] != dk:
            all_day_keys.append(dk)
    n_days = len(all_day_keys)
    n_pressure = max(1, round(n_days * pressure_fraction))
    pressure_day_keys: set[str] = set(all_day_keys[:n_pressure])

    rollup_rows: list[tuple] = []
    for i in range(n_minutes):
        ts = start_ts + i * 60.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        dk = dt.strftime("%Y-%m-%d")
        psi_mem = psi_high if dk in pressure_day_keys else psi_low
        rollup_rows.append(
            (
                ts,  # ts_min
                10.0 + rng.gauss(0, 1),  # cpu_avg
                15.0,  # cpu_max
                12.0,  # cpu_p95
                12.0,  # cpu_max_core_max
                int(3 * _GB),  # mem_used_avg
                int(3.1 * _GB),  # mem_used_max
                0.0,  # psi_cpu_max
                psi_mem,  # psi_mem_max
                0.0,  # psi_io_max
                swap_out_per_row,  # swap_out_sum
            )
        )

    store.buffer("sys_rollup_1m", rollup_rows)
    store.flush(reason="synth")


def add_swap_thrash_samples(
    store: Storage,
    *,
    n_thrash: int = 5,
    psi_full: float = 15.0,
    swap_pages: int = 100,
    start_ts: Optional[float] = None,
    baseline_hours: float = 1.0,
) -> None:
    """Insert sys_samples rows simulating a swap-thrashing episode.

    Inserts *baseline_hours* worth of normal samples (swap_in=swap_out=0,
    psi_mem_full10=0) followed by *n_thrash* consecutive samples where
    swap_in > 0, swap_out > 0, and psi_mem_full10 = *psi_full*.

    Consumed by: SWAP-THRASH-01 (detect path).
    """
    if start_ts is None:
        start_ts = time.time() - (baseline_hours * 3600.0 + n_thrash * 60.0)

    rng = random.Random(99)
    rows: list[tuple] = []
    n_baseline = int(baseline_hours * 60)

    # Baseline samples: no swap activity, no PSI pressure.
    for i in range(n_baseline):
        ts = start_ts + i * 60.0
        cpu = 10.0 + rng.gauss(0, 1)
        rows.append(
            (
                ts,
                cpu,
                cpu + 2,
                int(2 * _GB),
                int(6 * _GB),
                int(512 * 1024 * 1024),  # swap_used
                0,  # swap_in
                0,  # swap_out
                cpu / 100.0,
                0.0,  # psi_cpu_some10
                0.0,  # psi_mem_some10
                0.0,  # psi_mem_full10  ← zero in baseline
                0.0,
                0.0,
                0,
                0,
                0.1,
                int(50 * _MB),
            )
        )

    # Thrashing samples: swap_in > 0, swap_out > 0, psi_mem_full10 > 0.
    thrash_base = start_ts + n_baseline * 60.0
    for j in range(n_thrash):
        ts = thrash_base + j * 60.0
        rows.append(
            (
                ts,
                50.0,
                55.0,
                int(7.5 * _GB),
                int(100 * _MB),
                int(512 * 1024 * 1024),  # swap_used
                swap_pages,  # swap_in
                swap_pages,  # swap_out
                0.5,
                5.0,  # psi_cpu_some10
                50.0,  # psi_mem_some10
                psi_full,  # psi_mem_full10 ← full stall
                10.0,
                5.0,
                0,
                0,
                0.1,
                int(50 * _MB),
            )
        )

    store.buffer("sys_samples", rows)
    store.flush(reason="synth")


def add_cpu_saturation_samples(
    store: Storage,
    *,
    psi_cpu_high: float = 35.0,
    psi_cpu_low: float = 2.0,
    duration_minutes: int = 20,
    start_ts: Optional[float] = None,
    baseline_minutes: int = 10,
) -> None:
    """Insert sys_samples rows simulating a CPU saturation episode.

    Inserts *baseline_minutes* of low-PSI samples followed by
    *duration_minutes* of high-PSI (psi_cpu_some10 = *psi_cpu_high*) samples,
    then another *baseline_minutes* of low-PSI samples.

    Consumed by: CPU-SATURATION-01 (detect path).
    """
    if start_ts is None:
        total_minutes = baseline_minutes + duration_minutes + baseline_minutes
        start_ts = time.time() - total_minutes * 60.0

    rng = random.Random(55)
    rows: list[tuple] = []

    def _row(ts: float, cpu: float, psi_cpu: float) -> tuple:
        return (
            ts,
            cpu,
            cpu + 2,
            int(2 * _GB),
            int(6 * _GB),
            0,  # swap_used
            0,
            0,
            cpu / 100.0,
            psi_cpu,  # psi_cpu_some10
            0.0,
            0.0,
            0.0,
            0.0,
            0,
            0,
            0.1,
            int(50 * _MB),
        )

    # Pre-baseline.
    for i in range(baseline_minutes):
        ts = start_ts + i * 60.0
        rows.append(_row(ts, 10.0 + rng.gauss(0, 1), psi_cpu_low))

    # Saturation episode.
    ep_start = start_ts + baseline_minutes * 60.0
    for j in range(duration_minutes):
        ts = ep_start + j * 60.0
        rows.append(_row(ts, 80.0 + rng.gauss(0, 2), psi_cpu_high))

    # Post-baseline.
    post_start = ep_start + duration_minutes * 60.0
    for k in range(baseline_minutes):
        ts = post_start + k * 60.0
        rows.append(_row(ts, 10.0 + rng.gauss(0, 1), psi_cpu_low))

    store.buffer("sys_samples", rows)
    store.flush(reason="synth")


def add_zombie_series(
    store: Storage,
    *,
    hours: float,
    zombie_slope_per_h: float = 0.0,
    base_zombie_count: int = 0,
    start_ts: Optional[float] = None,
) -> None:
    """Insert synthetic proc_samples rows that approximate zombie processes.

    Generates one batch of proc_samples rows per tick (every 10 seconds) over
    *hours* of simulated time.  At each tick, ``zombie_approx_count`` rows
    are inserted with cpu_pct=0, rss=0, num_threads=0 to simulate zombies.
    The count increases linearly at *zombie_slope_per_h* zombies/hour starting
    from *base_zombie_count*.

    This data is consumed by ZOMBIE-01, which aggregates per-timestamp counts
    of (cpu_pct=0, rss=0, num_threads=0) rows.

    Parameters
    ----------
    store:
        Target storage instance.
    hours:
        Number of hours to simulate.
    zombie_slope_per_h:
        Rate of zombie count increase per hour.  0 means constant zombie count.
    base_zombie_count:
        Initial number of zombie-like processes at the start.
    start_ts:
        Start timestamp (POSIX seconds).  Defaults to
        ``time.time() - hours * 3600``.
    """
    if start_ts is None:
        start_ts = time.time() - hours * 3600.0

    # Register a placeholder proc_id for zombie rows.
    proc_id = store.proc_id_for(
        name="<defunct>",
        cmdline_hash="hash_zombie",
        cmdline_sample="",
        ts=start_ts,
    )
    store.conn.commit()

    # Insert one row every 10 seconds (tick interval matching ProcessSampler).
    tick_interval = 10.0
    n_ticks = int(hours * 3600 / tick_interval)

    rows: list[tuple] = []
    for i in range(n_ticks):
        ts = start_ts + i * tick_interval
        elapsed_h = i * tick_interval / 3600.0
        count = int(base_zombie_count + zombie_slope_per_h * elapsed_h)
        # Insert 'count' zombie-approximate rows at this timestamp.
        for z in range(count):
            rows.append(
                (
                    ts,
                    proc_id,
                    90000 + z,  # synthetic pid
                    0.0,  # cpu_pct = 0
                    0,  # rss = 0
                    None,  # pss
                    0,  # num_threads = 0
                    None,  # num_fds
                    None,  # io_read_delta
                    None,  # io_write_delta
                )
            )

    if rows:
        store.buffer("proc_samples", rows)
        store.flush(reason="synth")


def add_psi_io_series(
    store: Storage,
    *,
    hours: float,
    psi_io_some10: float,
    start_ts: Optional[float] = None,
) -> None:
    """Insert sys_samples rows with a constant psi_io_some10 value.

    Used by IO-BOTTLENECK-01 tests to simulate sustained I/O pressure.  One
    sample per minute is inserted over *hours* hours.  All other columns are
    set to typical idle values.

    Parameters
    ----------
    store:
        Target storage instance.
    hours:
        Number of hours to simulate.
    psi_io_some10:
        The PSI I/O some-10 value to set for every sample.
    start_ts:
        Start timestamp (POSIX seconds).  Defaults to
        ``time.time() - hours * 3600``.
    """
    if start_ts is None:
        start_ts = time.time() - hours * 3600.0

    n_minutes = int(hours * 60)
    rng = random.Random(99)
    rows: list[tuple] = []
    for i in range(n_minutes):
        ts = start_ts + i * 60.0
        rows.append(
            (
                ts,
                10.0,  # cpu_total
                12.0,  # cpu_max_core
                int(2 * _GB),  # mem_used
                int(6 * _GB),  # mem_available
                0,  # swap_used
                0,  # swap_in
                0,  # swap_out
                0.1,  # load1
                0.0,  # psi_cpu_some10
                0.0,  # psi_mem_some10
                0.0,  # psi_mem_full10
                psi_io_some10,  # psi_io_some10
                0.0,  # psi_io_full10
                0,  # disk_read_bytes
                0,  # disk_write_bytes
                0.1,  # self_cpu
                int(50 * _MB),  # self_rss
            )
        )
    store.buffer("sys_samples", rows)
    store.flush(reason="synth")


def add_proc_io_series(
    store: Storage,
    name: str,
    *,
    hours: float,
    io_write_per_min: int,
    io_read_per_min: int = 0,
    start_ts: Optional[float] = None,
) -> int:
    """Insert proc_samples rows for a process with constant I/O deltas.

    Used by IO-BOTTLENECK-01 tests.  The process is registered in
    ``proc_registry`` and one sample per minute is inserted into
    ``proc_samples`` with the given I/O delta values.  Returns the
    assigned ``proc_id``.

    Parameters
    ----------
    store:
        Target storage instance.
    name:
        Process name.
    hours:
        Number of hours to simulate.
    io_write_per_min:
        Bytes written per sample (io_write_delta).
    io_read_per_min:
        Bytes read per sample (io_read_delta).
    start_ts:
        Start timestamp (POSIX seconds).  Defaults to
        ``time.time() - hours * 3600``.
    """
    if start_ts is None:
        start_ts = time.time() - hours * 3600.0

    proc_id = store.proc_id_for(
        name=name,
        cmdline_hash=f"iohash_{name}",
        cmdline_sample=f"/usr/bin/{name}",
        ts=start_ts,
    )
    store.conn.commit()

    n_minutes = int(hours * 60)
    rows: list[tuple] = []
    for i in range(n_minutes):
        ts = start_ts + i * 60.0
        rows.append(
            (
                ts,
                proc_id,
                1000 + proc_id,  # pid
                5.0,  # cpu_pct
                int(200 * _MB),  # rss
                int(180 * _MB),  # pss
                4,  # num_threads
                20,  # num_fds
                io_read_per_min,  # io_read_delta
                io_write_per_min,  # io_write_delta
            )
        )
    store.buffer("proc_samples", rows)
    store.flush(reason="synth")
    return proc_id


def add_cg_io_series(
    store: Storage,
    unit_path: str,
    *,
    hours: float,
    io_wbytes_per_min: int,
    io_rbytes_per_min: int = 0,
    start_ts: Optional[float] = None,
) -> int:
    """Insert cg_samples rows for a cgroup with constant I/O byte deltas.

    Used by IO-BOTTLENECK-01 tests as the cgroup fallback path when
    proc_samples I/O counters are all NULL.  Returns the assigned ``cg_id``.

    Parameters
    ----------
    store:
        Target storage instance.
    unit_path:
        Cgroup unit path, e.g. ``"system.slice/mysqld.service"``.
    hours:
        Number of hours to simulate.
    io_wbytes_per_min:
        Bytes written per sample (io_wbytes_delta).
    io_rbytes_per_min:
        Bytes read per sample (io_rbytes_delta).
    start_ts:
        Start timestamp (POSIX seconds).  Defaults to
        ``time.time() - hours * 3600``.
    """
    if start_ts is None:
        start_ts = time.time() - hours * 3600.0

    cg_id = store.cg_id_for(unit_path, start_ts)
    store.conn.commit()

    n_minutes = int(hours * 60)
    rows: list[tuple] = []
    for i in range(n_minutes):
        ts = start_ts + i * 60.0
        rows.append(
            (
                ts,
                cg_id,
                0,  # cpu_usec_delta
                int(500 * _MB),  # mem_current
                0,  # mem_swap
                io_rbytes_per_min,  # io_rbytes_delta
                io_wbytes_per_min,  # io_wbytes_delta
                5,  # pids
            )
        )
    store.buffer("cg_samples", rows)
    store.flush(reason="synth")
    return cg_id
