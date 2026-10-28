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
* **num_fds throttling** — ``num_fds()`` scans ``/proc/<pid>/fd`` and is the
  most expensive per-process call; it is sampled ~once per minute since
  FD-LEAK-01 only reads fds at 1-minute rollup granularity.
* **process counts** — each scan also emits one ``proc_counts`` row (processes
  scanned, zombies).  The state comes from ``/proc/<pid>/stat``, which
  ``cpu_percent()`` already parses inside ``oneshot()``, so it is free; it lets
  ZOMBIE-01 count real zombies instead of approximating them.
* **FD limits** — when a PID is first seen its RLIMIT_NOFILE is recorded on the
  identity (``record_fd_limits``), so FD-LEAK-01 extrapolates to the process's
  real limit rather than a guessed 1024/4096.

Process identity uses ``normalize_cmdline`` + ``resolve_proc`` (injected
callable) following the proc_registry contract from design §5.2.
"""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Tuple

import psutil

from ..core.sampler import RunContext, SampleBatch, Sampler, register_sampler
from ..core.util import normalize_cmdline
from ..storage import make_row

# Per-attribute errors that degrade a single column to None instead of
# skipping the whole process.
_DENIED_OR_ZOMBIE = (psutil.AccessDenied, psutil.ZombieProcess)


class _Reading(NamedTuple):
    """Values read for one process inside a single ``oneshot()`` block."""

    name: Optional[str]  # only read for a PID without a cached identity
    cmdline: list[str]
    cpu: Optional[float]
    rss: Optional[int]
    num_threads: Optional[int]
    num_fds: Optional[int]
    io: Optional[Tuple[int, int]]  # cumulative (read_bytes, write_bytes)
    status: Optional[str]  # psutil.STATUS_* ("zombie", "running", ...)


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
        record_fd_limits: Optional[Callable[[int, int, int], None]] = None,
    ) -> None:
        # resolve_proc(name, hash12, cmdline_sample, ts) -> proc_id
        self._resolve_proc = resolve_proc
        # record_fd_limits(proc_id, soft, hard); None disables limit capture.
        self._record_fd_limits = record_fd_limits
        # proc_ids whose FD limits are already recorded.
        self._fd_limits_known: set[int] = set()
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
        # Tick counter used to throttle the expensive num_fds() /proc/<pid>/fd
        # scan to roughly once per minute instead of every tick.
        self._tick: int = 0

    def setup(self, ctx: RunContext) -> None:
        """No capability detection needed for process scanning."""

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Scan all processes; return ``proc_samples`` rows + a ``proc_counts`` row."""
        ts = time.time()
        rows: list[tuple] = []
        zombies = 0
        read_fds = self._should_read_fds(ctx)
        self._refresh_pid_cache()

        for pid, proc in list(self._pid_cache.items()):
            identity = self._identity.get(pid)
            try:
                reading = self._read_process(
                    pid, proc, read_fds=read_fds, need_identity=identity is None
                )
            except psutil.NoSuchProcess:
                # Process died between the pid listing and oneshot.
                self._forget(pid)
                continue
            except Exception:
                # AccessDenied (the process cannot be inspected at all) or any
                # other unexpected error — skip it for this tick.
                continue

            if identity is None:
                proc_id = self._register(pid, proc, reading, ts, ctx)
            else:
                _, proc_id = identity
            if reading.status == psutil.STATUS_ZOMBIE:
                zombies += 1

            # psutil's first cpu_percent() on a fresh Process object returns 0.0
            # without a baseline, so 0% and "no baseline" are indistinguishable.
            # Per spec (新規PIDの初回 cpu_pct=None で記録) the PID's first tick
            # records None; the absence of a _prev_io entry marks that tick.
            key = (proc_id, pid)
            is_first_tick = key not in self._prev_io
            io_read_delta, io_write_delta = self._io_deltas(
                key, reading.io, is_first_tick
            )

            # PSS scrutiny mode.
            pss: Optional[int] = None
            if proc_id in self.pss_targets:
                pss = _read_pss_bytes(ctx.proc_root / str(pid) / "smaps_rollup")

            rows.append(
                make_row(
                    "proc_samples",
                    ts=ts,
                    proc_id=proc_id,
                    pid=pid,
                    cpu_pct=None if is_first_tick else reading.cpu,
                    rss=reading.rss,
                    pss=pss,
                    num_threads=reading.num_threads,
                    num_fds=reading.num_fds,
                    io_read_delta=io_read_delta,
                    io_write_delta=io_write_delta,
                )
            )

        # Clean up stale _prev_io entries for pids that no longer exist
        # (membership-test the cache dict directly — no set rebuild needed).
        for key in list(self._prev_io):
            if key[1] not in self._pid_cache:
                del self._prev_io[key]

        counts = make_row("proc_counts", ts=ts, total=len(rows), zombies=zombies)
        return [
            SampleBatch(table="proc_samples", rows=rows),
            SampleBatch(table="proc_counts", rows=[counts]),
        ]

    # ------------------------------------------------------------------
    # collect() steps
    # ------------------------------------------------------------------

    def _should_read_fds(self, ctx: RunContext) -> bool:
        """Whether this tick reads ``num_fds()`` (roughly once per minute).

        ``num_fds()`` opens and scans /proc/<pid>/fd for every process — the
        most expensive per-process syscall here.  FD-LEAK-01 reads fds at
        1-minute rollup granularity, so sampling it about once per minute
        (rather than every tick) is lossless for the rule while cutting the
        scan cost.
        """
        interval = max(ctx.config.sampling.process_interval, 1.0)
        fd_every = max(1, round(60.0 / interval))
        read_fds = self._tick % fd_every == 0
        self._tick += 1
        return read_fds

    def _refresh_pid_cache(self) -> None:
        """Add newly started PIDs to the cache and forget vanished ones."""
        try:
            live_pids = set(psutil.pids())
        except Exception:
            live_pids = set()

        for pid in live_pids:
            if pid not in self._pid_cache:
                with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    self._pid_cache[pid] = psutil.Process(pid)

        for pid in list(self._pid_cache):
            if pid not in live_pids:
                self._forget(pid)

    def _forget(self, pid: int) -> None:
        """Drop every per-PID cache entry for a process that is gone."""
        self._pid_cache.pop(pid, None)
        self._identity.pop(pid, None)
        self._io_denied_pids.discard(pid)

    def _read_process(
        self,
        pid: int,
        proc: psutil.Process,
        *,
        read_fds: bool,
        need_identity: bool,
    ) -> _Reading:
        """Read one process's metrics inside a single ``oneshot()`` block.

        Name and cmdline are only read for a PID without a cached identity
        (they never change for a live process).  Per-attribute AccessDenied /
        zombie errors degrade just that column to None; NoSuchProcess and a
        process-wide AccessDenied propagate to the caller.
        """
        name: Optional[str] = None
        cmdline: list[str] = []
        with proc.oneshot():
            try:
                status: Optional[str] = proc.status()  # stat is cached in oneshot
            except _DENIED_OR_ZOMBIE:
                status = None
            if need_identity:
                name = proc.name()
                try:
                    cmdline = proc.cmdline()
                except _DENIED_OR_ZOMBIE:
                    cmdline = []
            cpu = proc.cpu_percent()
            try:
                rss: Optional[int] = proc.memory_info().rss
            except _DENIED_OR_ZOMBIE:
                rss = None
            try:
                num_threads: Optional[int] = proc.num_threads()
            except _DENIED_OR_ZOMBIE:
                num_threads = None
            num_fds: Optional[int] = None
            if read_fds:
                try:
                    num_fds = proc.num_fds()
                except (*_DENIED_OR_ZOMBIE, AttributeError):
                    num_fds = None
            io = self._read_io(pid, proc)
        return _Reading(name, cmdline, cpu, rss, num_threads, num_fds, io, status)

    def _read_io(self, pid: int, proc: psutil.Process) -> Optional[Tuple[int, int]]:
        """Return cumulative ``(read_bytes, write_bytes)``, or None if unreadable.

        AccessDenied is expected for processes owned by other users when
        running non-root.  A once-denied PID stays denied, so the read (and its
        exception cost — one per process per tick) is skipped from then on;
        the missing-rate counters still tick for the quality report.
        """
        self.io_total += 1
        if pid in self._io_denied_pids:
            self.io_denied += 1
            return None
        try:
            io = proc.io_counters()
            return io.read_bytes, io.write_bytes
        except (psutil.AccessDenied, AttributeError):
            self.io_denied += 1
            self._io_denied_pids.add(pid)
            return None
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None

    def _register(
        self,
        pid: int,
        proc: psutil.Process,
        reading: _Reading,
        ts: float,
        ctx: RunContext,
    ) -> int:
        """Resolve and cache the registry ``proc_id`` for a newly seen PID."""
        hash12, sample = normalize_cmdline(
            reading.cmdline,
            replace_digits=ctx.config.sampling.cmdline_normalize,
        )
        proc_id = self._resolve_proc(reading.name, hash12, sample, ts)
        self._identity[pid] = (reading.name, proc_id)
        self._capture_fd_limits(proc_id, proc)
        return proc_id

    def _capture_fd_limits(self, proc_id: int, proc: psutil.Process) -> None:
        """Record RLIMIT_NOFILE once per identity (best effort).

        Reading another user's limits needs root, so failures are expected when
        recording non-root; a later PID of the same identity may still succeed.
        """
        if self._record_fd_limits is None or proc_id in self._fd_limits_known:
            return
        try:
            soft, hard = proc.rlimit(psutil.RLIMIT_NOFILE)
        except Exception:  # AccessDenied / NoSuchProcess / platform without rlimit
            return
        self._record_fd_limits(proc_id, int(soft), int(hard))
        self._fd_limits_known.add(proc_id)

    def _io_deltas(
        self,
        key: Tuple[int, int],
        io: Optional[Tuple[int, int]],
        is_first_tick: bool,
    ) -> Tuple[Optional[int], Optional[int]]:
        """Return clamped I/O deltas for ``(proc_id, pid)`` and update the baseline.

        The first tick only records the baseline.  When I/O is unreadable on
        the first tick a ``(0, 0)`` sentinel is stored anyway, so the next tick
        is no longer treated as "first" (its delta stays None while unreadable).
        """
        if io is None:
            if is_first_tick:
                self._prev_io[key] = (0, 0)
            return None, None

        cur_r, cur_w = io
        deltas: Tuple[Optional[int], Optional[int]] = (None, None)
        if not is_first_tick:
            prev_r, prev_w = self._prev_io.get(key, io)
            deltas = (max(0, cur_r - prev_r), max(0, cur_w - prev_w))
        self._prev_io[key] = io
        return deltas
