"""Tier-2a cgroup sampler: per-service resource accounting via cgroup v2.

cgroup v2 (unified hierarchy) aggregates CPU time, memory, I/O bytes, and PID
counts at the systemd-unit boundary.  Because child processes are accounted to
their enclosing service cgroup, short-lived processes that escape the Tier-2b
process scan are still captured here.

Design choices:

* ``setup`` detects cgroup v2 by checking for ``cgroup.controllers`` under
  ``ctx.cgroup_root``.  If the file is absent (v1 or no cgroup), the sampler
  records a Japanese demotion note and returns an empty list from every
  ``collect`` call.
* Unit enumeration (``.service`` / ``.scope`` entries under ``system.slice/*/``
  and ``user.slice`` directly) is cached for five minutes to avoid redundant
  directory scans between ticks.
* CPU and I/O columns are recorded as *deltas* relative to the previous
  observation.  The first collect after start (or after a new unit appears)
  skips those columns (``None``) because there is no baseline to diff against.
* Any single file read failure (permission error, missing file) degrades only
  that column to ``None`` — the rest of the row is still emitted.  This is the
  "partial demotion" strategy described in design §2.1.

Registry IDs are resolved via ``resolve_cg(unit_path, ts)`` — a callable
injected at construction time.  In production, the recorder binds
``Storage.cg_id_for``; in tests a simple counter stub suffices.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from ..core.sampler import RunContext, SampleBatch, Sampler, register_sampler

# How long to cache the unit enumeration result (seconds).
_ENUM_CACHE_TTL = 300.0


def _read_int(path: Path) -> Optional[int]:
    """Return the integer content of *path*, or ``None`` on any error."""
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def _parse_cpu_stat(path: Path) -> Optional[int]:
    """Return ``usage_usec`` from a ``cpu.stat`` file, or ``None`` on failure."""
    try:
        text = path.read_text()
        for line in text.splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
        return None
    except Exception:
        return None


def _parse_io_stat(path: Path) -> tuple[Optional[int], Optional[int]]:
    """Return ``(rbytes_total, wbytes_total)`` aggregated across all devices.

    Each line of ``io.stat`` has the form::

        8:0 rbytes=N wbytes=N rios=N wios=N dbytes=N dios=N

    Returns ``(None, None)`` if the file is absent or unreadable.
    """
    try:
        text = path.read_text()
        rbytes = 0
        wbytes = 0
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            # parts[0] is the device major:minor; the rest are key=value pairs.
            for kv in parts[1:]:
                if kv.startswith("rbytes="):
                    rbytes += int(kv[7:])
                elif kv.startswith("wbytes="):
                    wbytes += int(kv[7:])
        return rbytes, wbytes
    except Exception:
        return None, None


@register_sampler
class CgroupSampler(Sampler):
    """Tier-2a: per-service resource accounting via the cgroup v2 unified tree.

    Reads ``cpu.stat``, ``memory.current``, ``memory.swap.current``,
    ``io.stat``, and ``pids.current`` for each unit under ``system.slice``
    and ``user.slice``.  Emits rows into ``cg_samples``.
    """

    name = "cgroup"
    title = "cgroup v2 サービス統計"
    tier = "T2a"
    interval_key = "cgroup_interval"
    what = "systemd ユニットごとの CPU/メモリ/I/O を cgroup v2 経由で記録する"
    why = (
        "短命な子プロセスの資源消費を cgroupに積算できるため、"
        "プロセス走査の隙間を埋める会計が可能になる"
    )
    manual_commands = [
        "systemd-cgtop",
        "cat /sys/fs/cgroup/system.slice/nginx.service/cpu.stat",
    ]

    def __init__(
        self,
        resolve_cg: Callable[[str, float], int],
    ) -> None:
        self._resolve_cg = resolve_cg
        # Previous cpu_usec and io cumulative values keyed by unit_path.
        self._prev_cpu: dict[str, int] = {}
        self._prev_io_r: dict[str, int] = {}
        self._prev_io_w: dict[str, int] = {}
        # Cached list of unit paths and the time it was last refreshed.
        self._units: list[str] = []
        self._units_refreshed: float = 0.0
        self._enabled: bool = False  # set by setup()

    def setup(self, ctx: RunContext) -> None:
        """Detect cgroup v2 and record a demotion note if absent."""
        controllers = ctx.cgroup_root / "cgroup.controllers"
        if controllers.exists():
            ctx.capabilities["cgroup_v2"] = True
            self._enabled = True
        else:
            ctx.capabilities["cgroup_v2"] = False
            self._enabled = False
            ctx.notes.append(
                "cgroup: cgroup v2 統一ツリーが見つかりません"
                " (cgroup.controllers 不在)。"
                "cgroup 層のサンプリングをスキップします。"
                " v1 環境ではプロセス視点のみで診断します。"
            )

    def _refresh_units(self, ctx: RunContext) -> None:
        """Enumerate .service/.scope units and cache the result for 5 minutes."""
        now = time.monotonic()
        if now - self._units_refreshed < _ENUM_CACHE_TTL:
            return

        units: list[str] = []
        root = ctx.cgroup_root

        # Enumerate system.slice/*.(service|scope)
        sys_slice = root / "system.slice"
        if sys_slice.is_dir():
            for entry in sorted(sys_slice.iterdir()):
                if entry.is_dir() and (
                    entry.name.endswith(".service") or entry.name.endswith(".scope")
                ):
                    units.append(f"system.slice/{entry.name}")

        # user.slice itself (not its sub-directories — follows spec §4.3)
        user_slice = root / "user.slice"
        if user_slice.is_dir():
            units.append("user.slice")

        self._units = units
        self._units_refreshed = now

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Read one tick of cgroup metrics and return ``cg_samples`` rows."""
        if not self._enabled:
            return []

        self._refresh_units(ctx)

        ts = time.time()
        rows: list[tuple] = []

        for unit_path in self._units:
            unit_dir = ctx.cgroup_root / unit_path

            # --- CPU ---
            cpu_usec_delta: Optional[int] = None
            cpu_raw = _parse_cpu_stat(unit_dir / "cpu.stat")
            if cpu_raw is not None:
                if unit_path in self._prev_cpu:
                    cpu_usec_delta = cpu_raw - self._prev_cpu[unit_path]
                self._prev_cpu[unit_path] = cpu_raw

            # --- Memory ---
            mem_current = _read_int(unit_dir / "memory.current")
            mem_swap = _read_int(unit_dir / "memory.swap.current")

            # --- I/O ---
            io_rbytes_delta: Optional[int] = None
            io_wbytes_delta: Optional[int] = None
            rb_raw, wb_raw = _parse_io_stat(unit_dir / "io.stat")
            if rb_raw is not None:
                if unit_path in self._prev_io_r:
                    io_rbytes_delta = rb_raw - self._prev_io_r[unit_path]
                self._prev_io_r[unit_path] = rb_raw
            if wb_raw is not None:
                if unit_path in self._prev_io_w:
                    io_wbytes_delta = wb_raw - self._prev_io_w[unit_path]
                self._prev_io_w[unit_path] = wb_raw

            # --- PIDs ---
            pids = _read_int(unit_dir / "pids.current")

            # Resolve registry ID via the injected callback.
            cg_id = self._resolve_cg(unit_path, ts)

            # Skip the row entirely on the very first collect (no deltas yet
            # *and* cpu_raw was None means the file is unreadable).
            # We always emit rows even if delta columns are None so that
            # the first tick after restart still captures memory/pids.
            rows.append(
                (
                    ts,
                    cg_id,
                    cpu_usec_delta,
                    mem_current,
                    mem_swap,
                    io_rbytes_delta,
                    io_wbytes_delta,
                    pids,
                )
            )

        return [SampleBatch(table="cg_samples", rows=rows)]
