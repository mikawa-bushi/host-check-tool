"""Tier-2b process sampler: full-process-table scan with persistent PID cache.

This module implements the Tier-2b sampler described in design §4.4.  It
iterates over all running processes using psutil and emits one ``proc_samples``
row per process per tick.

Key design choices:

* **PID cache** — psutil Process objects are kept alive across ticks so that
  ``cpu_percent()`` can return a meaningful diff (the first call on a fresh
  object always returns 0.0 or None on some platforms).  New PIDs added to the
  cache in tick N emit ``cpu_pct=None``; tick N+1 gives the real value.
* **oneshot context** — all attributes are read inside ``Process.oneshot()``
  to minimise /proc round-trips.
* **AccessDenied handling** — if I/O counters are denied, the ``io_read_delta``
  and ``io_write_delta`` columns are set to ``None`` and the instance-level
  counters ``io_denied`` / ``io_total`` are incremented.  T06 uses the ratio
  to report observation quality.
* **PSS scrutiny mode** — if a process's ``proc_id`` is in ``pss_targets``
  (populated by T06 rules), the sampler additionally reads
  ``ctx.proc_root/<pid>/smaps_rollup`` and extracts the ``Pss:`` line.
* **Zombie counting** — processes with ``status == "zombie"`` are counted in
  ``last_zombie_count`` for the ZOMBIE-01 rule.

Process identity uses ``normalize_cmdline`` + ``resolve_proc`` (injected
callable) following the proc_registry contract from design §5.2.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import psutil

from ..core.sampler import RunContext, SampleBatch, Sampler, register_sampler
from ..core.util import normalize_cmdline


def _read_pss_bytes(smaps_rollup: Path) -> Optional[int]:
    """Extract the ``Pss:`` total from ``smaps_rollup`` in bytes.

    Returns ``None`` if the file is absent or the line is not found.
    The file reports values in kB; this function converts to bytes.
    """
    try:
        text = smaps_rollup.read_text()
        for line in text.splitlines():
            if line.startswith("Pss:"):
                parts = line.split()
                # format: "Pss:  N kB"
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
        return None
    except Exception:
        return None


@register_sampler
class ProcessSampler(Sampler):
    """Tier-2b: full-process-table scan with persistent PID-object cache.

    Emits rows into ``proc_samples``.  See module docstring for full design
    rationale.
    """

    name = "process"
    title = "プロセス走査 (Tier-2b)"
    tier = "T2b"
    interval_key = "process_interval"
    what = (
        "全プロセスを psutil で走査し、CPU・メモリ・FD数・I/O を記録する。"
        "PID キャッシュを保持して cpu_percent の差分を正確に計測する。"
    )
    why = (
        "cgroup 統計が『何が』資源を食っているかを示す際、"
        "プロセス粒度の犯人特定には個別プロセスの詳細計測が必要なため"
    )
    manual_commands = [
        "ps aux --sort=-%cpu | head -20",
        "top -b -n 1",
    ]

    def __init__(
        self,
        resolve_proc: Callable[[str, str, str, float], int],
    ) -> None:
        # resolve_proc(name, hash12, cmdline_sample, ts) -> proc_id
        self._resolve_proc = resolve_proc
        # PID -> psutil.Process object cache.
        self._pid_cache: dict[int, psutil.Process] = {}
        # PID -> (name, proc_id): name/cmdline are immutable per process, so
        # re-reading them every tick costs ~1/3 of the scan (M8 measurement).
        # PID reuse within one 10s tick window is accepted, same as the
        # cpu_percent diff baseline.
        self._identity: dict[int, tuple[str, int]] = {}
        # PIDs whose io_counters raised AccessDenied: privileges don't change
        # over a process lifetime, so skip the failing read (and its exception
        # cost — one per process per tick when running non-root) from then on.
        self._io_denied_pids: set[int] = set()
        # Previous I/O counter values for delta computation.
        # keyed by (proc_id, pid)
        self._prev_io: dict[tuple[int, int], tuple[int, int]] = {}
        # Observation quality counters.
        self.io_denied: int = 0
        self.io_total: int = 0
        # PSS scrutiny: set of proc_ids (max 5) for which smaps_rollup is read.
        self.pss_targets: set[int] = set()
        # Zombie count from the last collect tick.
        self.last_zombie_count: int = 0

    def setup(self, ctx: RunContext) -> None:
        """No capability detection needed for process scanning."""

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Scan all processes and return ``proc_samples`` rows."""
        ts = time.time()
        rows: list[tuple] = []
        zombie_count = 0

        # Refresh the PID cache: discover new PIDs, remove stale ones.
        try:
            live_pids = set(psutil.pids())
        except Exception:
            live_pids = set()

        # Add new PIDs.
        for pid in live_pids:
            if pid not in self._pid_cache:
                try:
                    self._pid_cache[pid] = psutil.Process(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        # Remove pids that vanished.
        for pid in list(self._pid_cache):
            if pid not in live_pids:
                del self._pid_cache[pid]
                self._identity.pop(pid, None)
                self._io_denied_pids.discard(pid)

        for pid, proc in list(self._pid_cache.items()):
            identity = self._identity.get(pid)
            try:
                with proc.oneshot():
                    if identity is None:
                        name: str = proc.name()
                        try:
                            cmdline: list[str] = proc.cmdline()
                        except (psutil.AccessDenied, psutil.ZombieProcess):
                            cmdline = []

                    # cpu_percent: returns None / 0.0 on first call for this
                    # Process object.  We store None on first tick.
                    raw_cpu = proc.cpu_percent()

                    try:
                        mem = proc.memory_info()
                        rss: Optional[int] = mem.rss
                    except (psutil.AccessDenied, psutil.ZombieProcess):
                        rss = None

                    try:
                        num_threads: Optional[int] = proc.num_threads()
                    except (psutil.AccessDenied, psutil.ZombieProcess):
                        num_threads = None

                    try:
                        num_fds: Optional[int] = proc.num_fds()
                    except (psutil.AccessDenied, psutil.ZombieProcess, AttributeError):
                        num_fds = None

                    # I/O counters — AccessDenied is expected for
                    # processes owned by other users when running non-root.
                    # A once-denied PID stays denied, so skip the read (the
                    # missing-rate counters still tick for the quality report).
                    self.io_total += 1
                    io_read_delta: Optional[int] = None
                    io_write_delta: Optional[int] = None
                    if pid in self._io_denied_pids:
                        self.io_denied += 1
                        cur_r = None
                        cur_w = None
                    else:
                        try:
                            io = proc.io_counters()
                            cur_r = io.read_bytes
                            cur_w = io.write_bytes
                        except (psutil.AccessDenied, AttributeError):
                            self.io_denied += 1
                            self._io_denied_pids.add(pid)
                            cur_r = None
                            cur_w = None
                        except (psutil.NoSuchProcess, psutil.ZombieProcess):
                            cur_r = None
                            cur_w = None

                    try:
                        status: str = proc.status()
                    except (psutil.AccessDenied, psutil.ZombieProcess):
                        status = "unknown"

            except psutil.NoSuchProcess:
                # Process died between the pid listing and oneshot.
                del self._pid_cache[pid]
                self._identity.pop(pid, None)
                self._io_denied_pids.discard(pid)
                continue
            except psutil.AccessDenied:
                # Can't inspect the process at all — skip it.
                continue
            except Exception:
                # Any other unexpected error — skip gracefully.
                continue

            # Zombie counting.
            if status == "zombie":
                zombie_count += 1

            # Normalise cmdline and resolve the registry ID once per PID;
            # later ticks reuse the cached identity.
            if identity is None:
                hash12, sample = normalize_cmdline(
                    cmdline,
                    replace_digits=ctx.config.sampling.cmdline_normalize,
                )
                proc_id = self._resolve_proc(name, hash12, sample, ts)
                self._identity[pid] = (name, proc_id)
            else:
                _, proc_id = identity

            # cpu_pct: mark as None when raw_cpu is 0.0 and the PID was
            # just added to the cache (first call always returns 0.0).
            # We track "first seen" by checking whether (proc_id, pid)
            # already has a prior I/O entry — but a simpler heuristic:
            # psutil documents that the first cpu_percent() call on a
            # Process object returns 0.0, so we cannot distinguish 0% from
            # "no baseline".  Per spec, "新規PIDの初回 cpu_pct=None で記録".
            # We use the absence of a prev_io key as a proxy for "first tick".
            key = (proc_id, pid)
            is_first_tick = key not in self._prev_io

            cpu_pct: Optional[float] = None if is_first_tick else raw_cpu

            # I/O deltas.
            if cur_r is not None and cur_w is not None:
                if not is_first_tick:
                    prev_r, prev_w = self._prev_io.get(key, (cur_r, cur_w))
                    io_read_delta = max(0, cur_r - prev_r)
                    io_write_delta = max(0, cur_w - prev_w)
                self._prev_io[key] = (cur_r, cur_w)
            else:
                # Even if io is denied, record the key so next tick
                # is not "first".  Use sentinel (0, 0) — delta stays None.
                if is_first_tick:
                    self._prev_io[key] = (0, 0)

            # PSS scrutiny mode.
            pss: Optional[int] = None
            if proc_id in self.pss_targets:
                smaps_path = ctx.proc_root / str(pid) / "smaps_rollup"
                pss = _read_pss_bytes(smaps_path)

            rows.append(
                (
                    ts,
                    proc_id,
                    pid,
                    cpu_pct,
                    rss,
                    pss,
                    num_threads,
                    num_fds,
                    io_read_delta,
                    io_write_delta,
                )
            )

        self.last_zombie_count = zombie_count

        # Clean up stale _prev_io entries for pids that no longer exist.
        live_pid_set = set(self._pid_cache.keys())
        for key in list(self._prev_io):
            if key[1] not in live_pid_set:
                del self._prev_io[key]

        return [SampleBatch(table="proc_samples", rows=rows)]
