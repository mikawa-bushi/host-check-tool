"""Tier-1 system-wide sampler: CPU, memory, swap, disk I/O, load, and PSI.

This is the highest-frequency sampler (default 1 s).  It reads:

* ``psutil.cpu_percent(percpu=True)`` — overall average and per-core maximum
* ``psutil.virtual_memory()`` and ``psutil.swap_memory()`` — memory and swap;
  swap_in / swap_out are recorded as *deltas* (bytes since the last collect)
* ``psutil.disk_io_counters(perdisk=True)`` — read/write bytes as deltas, per
  device and then added up; partitions and LVM, dm-crypt, md and loop devices
  are not added to the disk they sit on
* ``/proc/loadavg`` — load average (direct read, not psutil)
* ``/proc/pressure/cpu|memory|io`` — PSI stall values (direct read)

All ``/proc`` reads go through ``ctx.proc_root`` so that tests can inject a
fixture directory without touching the real filesystem.

The PSI parser is exposed as a module-level pure function ``parse_psi`` so it
can be unit-tested independently, matching the netdiag idiom of keeping
testable logic at module scope.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Container, Dict, List, Optional, Tuple

import psutil

from ..core.result import Capability
from ..core.sampler import (
    RunContext,
    SampleBatch,
    Sampler,
    SystemSnapshot,
    register_sampler,
)
from ..core.util import CounterDelta
from ..storage import make_row
from .blockdev import is_partition, is_stacked

# ---------------------------------------------------------------------------
# PSI parser — pure function, tested directly
# ---------------------------------------------------------------------------

_PSI_KINDS = ("some", "full")
_PSI_AVG_KEYS = ("avg10", "avg60", "avg300")
# The sampler only consumes the 10-second averages (some_avg10 / full_avg10);
# restrict per-tick parsing to these to skip avg60/avg300/total conversions.
_PSI_USED_KEYS = frozenset({"avg10"})
# /proc/pressure/<file> -> (snapshot key prefix, PSI kinds recorded for it).
# The cpu file's "full" line is meaningless at the system level, so only
# "some" is kept for it.
_PSI_SOURCES = (
    ("cpu", "cpu", ("some",)),
    ("memory", "mem", ("some", "full")),
    ("io", "io", ("some", "full")),
)


def parse_psi(text: str, *, keys: "frozenset[str] | None" = None) -> Dict[str, float]:
    """Parse the content of a ``/proc/pressure/*`` file into a flat dict.

    The returned dict has keys of the form ``"<kind>_<metric>"`` where
    *kind* is ``some`` or ``full`` and *metric* is one of ``avg10``,
    ``avg60``, ``avg300``, or ``total``.  For example::

        {
            "some_avg10": 2.30,
            "some_avg60": 1.50,
            "some_avg300": 0.80,
            "some_total": 1234567.0,
            "full_avg10": 0.10,
            ...
        }

    Lines that cannot be parsed are silently skipped so that a partially
    readable file (e.g. a kernel that omits the ``full`` line for ``cpu``)
    still returns whatever data is present rather than raising.

    When *keys* is given (e.g. ``{"avg10"}``), only those metric keys are
    parsed; the per-tick sampler passes an allowlist so it does not float-
    convert ``avg60``/``avg300``/``total`` tokens it never uses.
    """
    result: Dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]  # "some" or "full"
        if kind not in _PSI_KINDS:
            continue
        for token in parts[1:]:
            if "=" not in token:
                continue
            key, _, raw_val = token.partition("=")
            key = key.strip()
            if keys is not None and key not in keys:
                continue
            raw_val = raw_val.strip()
            try:
                value = float(raw_val)
            except ValueError:
                continue
            result[f"{kind}_{key}"] = value
    return result


# ---------------------------------------------------------------------------
# SystemSampler
# ---------------------------------------------------------------------------


def _counts_disk_io(name: str, per_disk: Container[str], sys_root: Path) -> bool:
    """Whether the device *name* of psutil's per-disk listing adds to the host's I/O.

    A disk's counters include its partitions, and a logical volume, dm-crypt or
    md device, or a loop device, repeats the I/O of the device below it.  A
    device that /sys/block does not describe counts as not stacked: a disk is
    not dropped because its sysfs entry is missing or unreadable.
    """
    return not is_partition(name, per_disk) and not is_stacked(
        sys_root / "block" / name
    )


@register_sampler
class SystemSampler(Sampler):
    """Tier-1: record system-wide resource metrics every second.

    Attributes follow the procdiag Sampler convention (mirrors netdiag Probe):
    ``name``, ``tier``, ``interval_key`` for scheduling; ``title``/``what``/
    ``why``/``manual_commands`` for the ``explain`` command.
    """

    name = "system"
    tier = "T1"
    interval_key = "system_interval"
    title = "システム全体メトリクス"
    what = (
        "CPU使用率(コア別)、メモリ・スワップ、ディスクI/O、ロードアベレージ、PSIを"
        "1秒ごとに記録する"
    )
    why = (
        "PSI(Pressure Stall Information)は「誰かがCPU/メモリ/I/Oを待った時間」の割合であり、"
        "システム飽和の一次検出情報となる。スワップアウト増分と合わせてメモリ不足の早期検知に使う"
    )
    manual_commands = [
        "cat /proc/pressure/cpu",
        "cat /proc/pressure/memory",
        "cat /proc/pressure/io",
        "vmstat 1",
        "cat /proc/loadavg",
    ]

    # ------------------------------------------------------------------
    # Instance state (lives for the full record session)
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        # Cumulative swap / disk-I/O counters -> per-tick deltas.
        self._deltas = CounterDelta()
        # Most recent snapshot exposed to the trigger / recorder.
        self.last_snapshot: Optional[SystemSnapshot] = None

    # ------------------------------------------------------------------
    # Sampler protocol
    # ------------------------------------------------------------------

    def setup(self, ctx: RunContext) -> None:
        """Detect PSI support and record the capability flag.

        If ``/proc/pressure/cpu`` is absent or unreadable (PSI disabled reads
        fail with EOPNOTSUPP), the session lacks :attr:`Capability.PSI`.
        """
        cpu_pressure = ctx.proc_root / "pressure" / "cpu"
        try:
            cpu_pressure.read_text()
            ctx.capabilities[Capability.PSI.value] = True
        except OSError as exc:
            ctx.lacks(
                Capability.PSI,
                f"PSI (/proc/pressure) を読めないため、CPU・メモリ・I/O の圧迫を記録していません"
                f" ({exc.strerror or exc})。カーネル 4.20 より前か、PSI が無効です",
            )

    def collect(self, ctx: RunContext) -> List[SampleBatch]:
        """Collect one tick of system-wide metrics.

        Returns a single-element list: ``[SampleBatch("sys_samples", [row])]``.

        On the *first* call, swap and disk delta columns are 0 (the baseline is
        recorded for the next call).  Subsequent calls return true deltas.
        """
        ts = time.time()

        # --- CPU ---
        per_core: List[float] = psutil.cpu_percent(percpu=True)  # type: ignore[assignment]
        if per_core:
            cpu_total = sum(per_core) / len(per_core)
            cpu_max_core = max(per_core)
        else:
            cpu_total = psutil.cpu_percent()
            cpu_max_core = cpu_total

        # --- Memory / swap (swap_in / swap_out are cumulative bytes) ---
        vmem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        swap_in_delta = self._delta("swap_in", swap.sin)
        swap_out_delta = self._delta("swap_out", swap.sout)

        # --- Disk I/O ---
        disk_read_delta, disk_write_delta = self._disk_io_deltas(ctx.sys_root)

        # --- PSI (direct /proc read) ---
        psi: Optional[Dict[str, Optional[float]]] = None
        if ctx.capabilities.get("psi", False):
            psi = self._read_pressure(ctx.proc_root / "pressure")
        psi_cols = psi or {}

        # --- Self metrics ---
        self_cpu: Optional[float] = None
        self_rss: Optional[int] = None
        if ctx.selfmeter is not None:
            self_cpu, self_rss = ctx.selfmeter.sample()

        # --- Build the snapshot for trigger / recorder ---
        self.last_snapshot = SystemSnapshot(
            ts=ts,
            cpu_total=cpu_total,
            cpu_max_core=cpu_max_core,
            mem_total=vmem.total,
            mem_available=vmem.available,
            swap_out_delta=swap_out_delta,
            psi=psi,
        )

        row = make_row(
            "sys_samples",
            ts=ts,
            cpu_total=cpu_total,
            cpu_max_core=cpu_max_core,
            mem_used=vmem.used,
            mem_available=vmem.available,
            swap_used=swap.used,
            swap_in=swap_in_delta,
            swap_out=swap_out_delta,
            load1=self._read_load1(ctx.proc_root),
            psi_cpu_some10=psi_cols.get("cpu_some10"),
            psi_mem_some10=psi_cols.get("mem_some10"),
            psi_mem_full10=psi_cols.get("mem_full10"),
            psi_io_some10=psi_cols.get("io_some10"),
            psi_io_full10=psi_cols.get("io_full10"),
            disk_read_bytes=disk_read_delta,
            disk_write_bytes=disk_write_delta,
            self_cpu=self_cpu,
            self_rss=self_rss,
        )
        return [SampleBatch("sys_samples", [row])]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _delta(self, key: str, value: int) -> int:
        """Clamped per-tick delta of a cumulative counter (0 on the first tick)."""
        delta = self._deltas.update(key, value)
        return 0 if delta is None else delta

    def _disk_io_deltas(self, sys_root: Path) -> Tuple[int, int]:
        """Return ``(read_bytes_delta, write_bytes_delta)`` since the last call.

        Each device is differenced on its own and only then added up, so a
        device that starts or stops counting (a loop device attached or
        detached, a sysfs entry unreadable for a moment) is not I/O.  Returns
        ``(0, 0)`` on the first call or if counters are unavailable.  An empty
        listing is no counters: it leaves the baselines in place, so the disks
        that come back are not counted from 0.
        """
        try:
            per_disk = psutil.disk_io_counters(perdisk=True)
        except (AttributeError, OSError):
            return 0, 0
        if not per_disk:
            return 0, 0
        read = self._deltas.update_each(
            "disk_read", {n: c.read_bytes for n, c in per_disk.items()}
        )
        write = self._deltas.update_each(
            "disk_write", {n: c.write_bytes for n, c in per_disk.items()}
        )
        if read is None or write is None:
            return 0, 0
        # Only a device that did I/O is worth a look at sysfs.
        counted = [
            n
            for n in per_disk
            if (read[n] or write[n]) and _counts_disk_io(n, per_disk, sys_root)
        ]
        return sum(read[n] for n in counted), sum(write[n] for n in counted)

    @classmethod
    def _read_pressure(cls, pressure_dir: Path) -> Dict[str, Optional[float]]:
        """Read the 10-second PSI averages into snapshot keys (``cpu_some10`` …).

        A file that cannot be read contributes no keys, so the matching
        ``sys_samples`` columns are recorded as NULL for that tick.
        """
        psi: Dict[str, Optional[float]] = {}
        for filename, prefix, kinds in _PSI_SOURCES:
            vals = cls._read_psi(pressure_dir / filename)
            if vals is None:
                continue
            for kind in kinds:
                psi[f"{prefix}_{kind}10"] = vals.get(f"{kind}_avg10")
        return psi

    @staticmethod
    def _read_load1(proc_root: Path) -> Optional[float]:
        """Read the 1-minute load average from ``proc_root/loadavg``."""
        try:
            text = (proc_root / "loadavg").read_text()
            return float(text.split()[0])
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _read_psi(path: Path) -> Optional[Dict[str, float]]:
        """Read and parse a single PSI file.  Returns None on any I/O error."""
        try:
            text = path.read_text()
            return parse_psi(text, keys=_PSI_USED_KEYS)
        except OSError:
            return None
