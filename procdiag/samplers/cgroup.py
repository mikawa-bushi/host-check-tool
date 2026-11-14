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
  observation (clamped at 0 across unit restarts, see ``CounterDelta``).  The
  first collect after start (or after a new unit appears) skips those columns
  (``None``) because there is no baseline to diff against.
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

from ..core.result import Capability
from ..core.sampler import RunContext, SampleBatch, Sampler, register_sampler
from ..core.util import CounterDelta
from ..storage import make_row

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
        # Cumulative cpu_usec / io bytes per (unit_path, counter) -> deltas.
        self._deltas = CounterDelta()
        # Cached list of unit paths and the time it was last refreshed.
        # -inf, not 0.0: time.monotonic() counts from boot on Linux, so a
        # recorder started at boot would otherwise skip enumeration (and record
        # no cgroups) until the host had been up for _ENUM_CACHE_TTL.
        self._units: list[str] = []
        self._units_refreshed: float = float("-inf")
        self._enabled: bool = False  # set by setup()

    def setup(self, ctx: RunContext) -> None:
        """Detect cgroup v2 and record a demotion note if absent."""
        controllers = ctx.cgroup_root / "cgroup.controllers"
        if controllers.exists():
            ctx.capabilities[Capability.CGROUP_V2.value] = True
            self._enabled = True
        else:
            self._enabled = False
            ctx.lacks(
                Capability.CGROUP_V2,
                "cgroup: cgroup v2 統一ツリーが見つかりません"
                " (cgroup.controllers 不在)。"
                "cgroup 層のサンプリングをスキップします。"
                " v1 環境ではプロセス視点のみで診断します。",
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
            rb_raw, wb_raw = _parse_io_stat(unit_dir / "io.stat")

            # Rows are emitted even when the delta columns are still None (first
            # tick for a unit) so that memory/pids are captured from the start.
            rows.append(
                make_row(
                    "cg_samples",
                    ts=ts,
                    cg_id=self._resolve_cg(unit_path, ts),
                    cpu_usec_delta=self._delta(
                        unit_path, "cpu", _parse_cpu_stat(unit_dir / "cpu.stat")
                    ),
                    mem_current=_read_int(unit_dir / "memory.current"),
                    mem_swap=_read_int(unit_dir / "memory.swap.current"),
                    io_rbytes_delta=self._delta(unit_path, "io_r", rb_raw),
                    io_wbytes_delta=self._delta(unit_path, "io_w", wb_raw),
                    pids=_read_int(unit_dir / "pids.current"),
                )
            )

        return [SampleBatch(table="cg_samples", rows=rows)]

    def _delta(self, unit_path: str, counter: str, raw: Optional[int]) -> Optional[int]:
        """Per-tick delta of one cumulative counter; None when unreadable/first.

        An unreadable tick leaves the previous baseline in place, so the next
        readable tick still diffs against the last good value.
        """
        if raw is None:
            return None
        return self._deltas.update((unit_path, counter), raw)
