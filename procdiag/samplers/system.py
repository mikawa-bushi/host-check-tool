"""Tier-1 system-wide sampler: CPU, memory, swap, disk I/O, load, and PSI.

This is the highest-frequency sampler (default 1 s).  It reads:

* ``psutil.cpu_percent(percpu=True)`` — overall average and per-core maximum
* ``psutil.virtual_memory()`` and ``psutil.swap_memory()`` — memory and swap;
  swap_in / swap_out are recorded as *deltas* (bytes since the last collect)
* ``psutil.disk_io_counters()`` — aggregate read/write bytes as deltas
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
from typing import Any, Dict, List, Optional, Tuple

import psutil

from ..core.sampler import (
    RunContext,
    SampleBatch,
    Sampler,
    SystemSnapshot,
    register_sampler,
)
from ..storage import TABLE_COLUMNS

# ---------------------------------------------------------------------------
# PSI parser — pure function, tested directly
# ---------------------------------------------------------------------------

_PSI_KINDS = ("some", "full")
_PSI_AVG_KEYS = ("avg10", "avg60", "avg300")
# The sampler only consumes the 10-second averages (some_avg10 / full_avg10);
# restrict per-tick parsing to these to skip avg60/avg300/total conversions.
_PSI_USED_KEYS = frozenset({"avg10"})


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
        # Previous swap counters for delta computation.
        self._prev_swap_in: Optional[int] = None
        self._prev_swap_out: Optional[int] = None
        # Previous disk I/O counters for delta computation.
        self._prev_disk_read: Optional[int] = None
        self._prev_disk_write: Optional[int] = None
        # Most recent snapshot exposed to the trigger / recorder.
        self.last_snapshot: Optional[SystemSnapshot] = None

    # ------------------------------------------------------------------
    # Sampler protocol
    # ------------------------------------------------------------------

    def setup(self, ctx: RunContext) -> None:
        """Detect PSI support and record the capability flag.

        If ``/proc/pressure/cpu`` is absent or unreadable, ``ctx.capabilities["psi"]``
        is set to ``False`` and a Japanese demotion note is appended to
        ``ctx.notes``.
        """
        cpu_pressure = ctx.proc_root / "pressure" / "cpu"
        try:
            cpu_pressure.read_text()
            ctx.capabilities["psi"] = True
        except OSError:
            ctx.capabilities["psi"] = False
            ctx.notes.append(
                "PSI(/proc/pressure)が読み取れないため、PSI列はNULLで記録されます。"
                "カーネル4.20未満またはPSI無効でビルドされた可能性があります。"
            )

    def collect(self, ctx: RunContext) -> List[SampleBatch]:
        """Collect one tick of system-wide metrics.

        Returns a single-element list: ``[SampleBatch("sys_samples", [row])]``.
        The row tuple follows the column order defined in
        ``storage.TABLE_COLUMNS["sys_samples"]``.

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

        # --- Memory ---
        vmem = psutil.virtual_memory()
        mem_used: int = vmem.used
        mem_available: int = vmem.available

        # --- Swap ---
        swap = psutil.swap_memory()
        swap_used: int = swap.used
        raw_swap_in: int = swap.sin  # cumulative bytes
        raw_swap_out: int = swap.sout

        if self._prev_swap_in is None:
            swap_in_delta = 0
            swap_out_delta = 0
        else:
            swap_in_delta = max(0, raw_swap_in - self._prev_swap_in)
            swap_out_delta = max(0, raw_swap_out - self._prev_swap_out)  # type: ignore[operator]
        self._prev_swap_in = raw_swap_in
        self._prev_swap_out = raw_swap_out

        # --- Disk I/O ---
        disk_read_delta, disk_write_delta = self._disk_io_deltas()

        # --- Load average (direct /proc read) ---
        load1 = self._read_load1(ctx.proc_root)

        # --- PSI (direct /proc read) ---
        psi_cpu_some10: Optional[float] = None
        psi_mem_some10: Optional[float] = None
        psi_mem_full10: Optional[float] = None
        psi_io_some10: Optional[float] = None
        psi_io_full10: Optional[float] = None
        psi_dict: Optional[Dict[str, float]] = None

        if ctx.capabilities.get("psi", False):
            psi_dict = {}
            pressure_dir = ctx.proc_root / "pressure"

            cpu_vals = self._read_psi(pressure_dir / "cpu")
            if cpu_vals is not None:
                psi_cpu_some10 = cpu_vals.get("some_avg10")
                psi_dict["cpu_some10"] = psi_cpu_some10  # type: ignore[assignment]

            mem_vals = self._read_psi(pressure_dir / "memory")
            if mem_vals is not None:
                psi_mem_some10 = mem_vals.get("some_avg10")
                psi_mem_full10 = mem_vals.get("full_avg10")
                psi_dict["mem_some10"] = psi_mem_some10  # type: ignore[assignment]
                psi_dict["mem_full10"] = psi_mem_full10  # type: ignore[assignment]

            io_vals = self._read_psi(pressure_dir / "io")
            if io_vals is not None:
                psi_io_some10 = io_vals.get("some_avg10")
                psi_io_full10 = io_vals.get("full_avg10")
                psi_dict["io_some10"] = psi_io_some10  # type: ignore[assignment]
                psi_dict["io_full10"] = psi_io_full10  # type: ignore[assignment]

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
            mem_available=mem_available,
            swap_out_delta=swap_out_delta,
            psi=psi_dict,
        )

        # --- Assemble row in TABLE_COLUMNS order ---
        # ts, cpu_total, cpu_max_core, mem_used, mem_available,
        # swap_used, swap_in, swap_out,
        # load1,
        # psi_cpu_some10, psi_mem_some10, psi_mem_full10, psi_io_some10, psi_io_full10,
        # disk_read_bytes, disk_write_bytes,
        # self_cpu, self_rss
        row: tuple = (
            ts,
            cpu_total,
            cpu_max_core,
            mem_used,
            mem_available,
            swap_used,
            swap_in_delta,
            swap_out_delta,
            load1,
            psi_cpu_some10,
            psi_mem_some10,
            psi_mem_full10,
            psi_io_some10,
            psi_io_full10,
            disk_read_delta,
            disk_write_delta,
            self_cpu,
            self_rss,
        )

        return [SampleBatch("sys_samples", [row])]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _disk_io_deltas(self) -> Tuple[int, int]:
        """Return ``(read_bytes_delta, write_bytes_delta)`` since the last call.

        Returns ``(0, 0)`` on the first call or if counters are unavailable.
        """
        try:
            counters = psutil.disk_io_counters()
            if counters is None:
                return 0, 0
            raw_read: int = counters.read_bytes
            raw_write: int = counters.write_bytes
        except (AttributeError, OSError):
            return 0, 0

        if self._prev_disk_read is None:
            self._prev_disk_read = raw_read
            self._prev_disk_write = raw_write
            return 0, 0

        read_delta = max(0, raw_read - self._prev_disk_read)
        write_delta = max(0, raw_write - self._prev_disk_write)  # type: ignore[operator]
        self._prev_disk_read = raw_read
        self._prev_disk_write = raw_write
        return read_delta, write_delta

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
