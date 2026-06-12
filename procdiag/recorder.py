"""Always-on record loop: the resident daemon that wires all samplers together.

Maps to netdiag's ``watch.py`` (Watcher) — "light periodic loop that
escalates to full diagnosis on events" — but generalised to three tiers plus
disk, a spike trigger, a self-throttle meter, and a 60-second flush cadence.

Key design decisions (mirroring netdiag/Watcher):
- All components are injected via ``__init__`` so the recorder is testable
  without importing the real sampler modules (T02–T04 run in parallel).
- Signal handlers (SIGTERM/SIGINT) are registered only when running in the
  main thread; calling ``run()`` from a worker thread is safe but no signal
  handling occurs.
- The scheduler uses ``time.monotonic`` as its clock so suspend/resume gaps
  are visible in ops_events without distorting wall-clock timestamps.
- A single ``threading.Event`` (``_stop``) coordinates graceful shutdown
  across the scheduler, the signal handler, and ``duration``-based expiry.
"""

from __future__ import annotations

import json
import logging
import platform
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .config import ProcdiagConfig
from .core.sampler import RunContext, Sampler
from .scheduler import TierScheduler
from .storage import Storage

try:
    from . import __version__ as _VERSION
except ImportError:
    _VERSION = "unknown"

logger = logging.getLogger(__name__)


class Recorder:
    """Resident record loop for procdiag.

    All collaborators are injection points so unit tests can supply fakes
    without importing the real sampler implementations.

    Parameters
    ----------
    config:
        Validated ``ProcdiagConfig``; drives all intervals and thresholds.
    db_path:
        Override for ``config.db_path`` (convenient in tests).
    duration:
        If given, ``run()`` exits after this many seconds (monotonic).
        ``None`` means run forever (until a signal).
    ctx:
        Pre-built ``RunContext``; created from *config* if ``None``.
    samplers:
        Dict mapping keys ``"system"``, ``"cgroup"``, ``"process"``,
        ``"disk"`` to ``Sampler`` instances.  If ``None`` the real
        implementations are imported lazily (T09 wiring).
    trigger:
        Spike-trigger object; must implement
        ``observe(snap, mono_now) -> SpikeDecision | None``.
    selfmeter:
        Self-overhead meter; must implement
        ``sample() -> (float, int)``,
        ``update(mono_now) -> str | None``, and expose ``factor: float``.
    storage:
        Pre-built ``Storage`` instance (useful in tests).
    clock:
        Monotonic clock callable (injectable for tests).
    sleep:
        Sleep callable (injectable for tests).
    """

    def __init__(
        self,
        config: ProcdiagConfig,
        *,
        db_path: Optional[Path] = None,
        duration: Optional[float] = None,
        ctx: Optional[RunContext] = None,
        samplers: Optional[Dict[str, Sampler]] = None,
        trigger: Any = None,
        selfmeter: Any = None,
        storage: Optional[Storage] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._duration = duration
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()

        # Storage -----------------------------------------------------------
        if storage is not None:
            self._storage = storage
            self._owns_storage = False
        else:
            path = Path(db_path) if db_path is not None else Path(config.db_path)
            self._storage = Storage(path)
            self._owns_storage = True

        # RunContext --------------------------------------------------------
        self._ctx = ctx if ctx is not None else RunContext(config)

        # Samplers (lazy import if not injected) ----------------------------
        if samplers is not None:
            self._samplers = samplers
        else:
            # T09 wiring: build real sampler instances with storage callbacks.
            self._samplers = self._build_samplers()

        # Collaborators (build defaults when not injected) ------------------
        if trigger is not None:
            self._trigger = trigger
        else:
            self._trigger = self._build_trigger()

        if selfmeter is not None:
            self._selfmeter = selfmeter
        else:
            self._selfmeter = self._build_selfmeter()

        # Internal state ----------------------------------------------------
        self._start_mono: float = 0.0
        self._last_retention_mono: float = 0.0
        self._flush_interval = 60.0
        self._retention_interval = 3600.0

    # ------------------------------------------------------------------
    # Sampler / collaborator factory (T09 wiring)
    # ------------------------------------------------------------------

    def _build_samplers(self) -> Dict[str, Sampler]:
        """Import real sampler classes and wire storage callbacks.

        ``CgroupSampler`` and ``ProcessSampler`` require registry-id callbacks
        bound to the live Storage instance so they can resolve unit/process IDs
        without holding a reference to the storage object themselves.
        """
        from .samplers.system import SystemSampler  # type: ignore[import]
        from .samplers.cgroup import CgroupSampler  # type: ignore[import]
        from .samplers.process import ProcessSampler  # type: ignore[import]
        from .samplers.disk import DiskSampler  # type: ignore[import]

        return {
            "system": SystemSampler(),
            "cgroup": CgroupSampler(resolve_cg=self._storage.cg_id_for),
            "process": ProcessSampler(resolve_proc=self._storage.proc_id_for),
            "disk": DiskSampler(),
        }

    def _build_trigger(self) -> Any:
        """Build a SpikeTrigger with PSI availability from config context.

        PSI availability is detected during setup(); we default to the
        conservative assumption that PSI is available (it will be corrected
        by SystemSampler.setup once setup() runs).
        """
        try:
            from .samplers.trigger import SpikeTrigger  # type: ignore[import]

            # Use PSI if /proc/pressure/cpu exists on the real filesystem.
            psi_available = (self._ctx.proc_root / "pressure" / "cpu").exists()
            return SpikeTrigger(self._config.trigger, psi_available=psi_available)
        except Exception:
            logger.warning(
                "SpikeTrigger の初期化に失敗しました — トリガなしで継続します"
            )
            return None

    def _build_selfmeter(self) -> Any:
        """Build a SelfMeter from the budget config."""
        try:
            from .samplers.selfmeter import SelfMeter  # type: ignore[import]

            return SelfMeter(self._config.budget)
        except Exception:
            logger.warning(
                "SelfMeter の初期化に失敗しました — 自己計測なしで継続します"
            )
            return None

    # ------------------------------------------------------------------
    # Meta helpers
    # ------------------------------------------------------------------

    def _write_meta(self) -> None:
        """Record session-start metadata into the meta table."""
        import psutil

        try:
            boot_time = psutil.boot_time()
        except Exception:
            boot_time = 0.0

        entries = {
            "hostname": socket.gethostname(),
            "kernel": platform.release(),
            "boot_time": str(boot_time),
            "procdiag_version": _VERSION,
            "config": self._config.model_dump_json(),
            "started_ts": str(time.time()),
        }
        for k, v in entries.items():
            self._storage.set_meta(k, v)
        self._storage.conn.commit()

    # ------------------------------------------------------------------
    # Buffer helpers
    # ------------------------------------------------------------------

    def _buffer_batches(self, batches: list) -> None:
        for batch in batches:
            self._storage.buffer(batch.table, batch.rows)

    # ------------------------------------------------------------------
    # Spike handling
    # ------------------------------------------------------------------

    def _handle_spike(self, decision: Any, snap: Any) -> None:
        """Run T2a+T2b immediately and record a spike_events row."""
        cg_sampler = self._samplers.get("cgroup")
        proc_sampler = self._samplers.get("process")

        cg_batches = cg_sampler.run(self._ctx) if cg_sampler else []
        proc_batches = proc_sampler.run(self._ctx) if proc_sampler else []

        self._buffer_batches(cg_batches)
        self._buffer_batches(proc_batches)

        # Build top lists from the freshly buffered proc/cgroup rows.
        top_procs_json = self._top_processes_json(proc_batches)
        top_cgroups_json = self._top_cgroups_json(cg_batches)

        psi_snap = decision.psi_snapshot if hasattr(decision, "psi_snapshot") else None
        trigger_type = (
            decision.trigger_type if hasattr(decision, "trigger_type") else "unknown"
        )

        with self._storage.conn:
            self._storage.conn.execute(
                "INSERT INTO spike_events "
                "(ts, trigger_type, psi_snapshot, top_processes, top_cgroups) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    time.time(),
                    trigger_type,
                    json.dumps(psi_snap) if psi_snap is not None else "{}",
                    top_procs_json,
                    top_cgroups_json,
                ),
            )

        self._storage.flush(reason="spike")

    def _top_processes_json(self, proc_batches: list) -> str:
        """Extract top-10 processes by CPU and RSS from proc_samples batches."""
        rows = []
        for b in proc_batches:
            if b.table == "proc_samples":
                rows.extend(b.rows)

        # proc_samples columns: ts, proc_id, pid, cpu_pct, rss, pss,
        #                       num_threads, num_fds, io_read_delta, io_write_delta
        by_cpu = sorted(
            rows,
            key=lambda r: (r[3] if r[3] is not None else 0.0),
            reverse=True,
        )[:10]
        by_rss = sorted(
            rows,
            key=lambda r: (r[4] if r[4] is not None else 0),
            reverse=True,
        )[:10]

        def _row_to_dict(r: tuple) -> dict:
            return {
                "ts": r[0],
                "proc_id": r[1],
                "pid": r[2],
                "cpu_pct": r[3],
                "rss": r[4],
            }

        return json.dumps(
            {
                "by_cpu": [_row_to_dict(r) for r in by_cpu],
                "by_rss": [_row_to_dict(r) for r in by_rss],
            }
        )

    def _top_cgroups_json(self, cg_batches: list) -> str:
        """Extract top-5 cgroups by memory from cg_samples batches."""
        rows = []
        for b in cg_batches:
            if b.table == "cg_samples":
                rows.extend(b.rows)

        # cg_samples columns: ts, cg_id, cpu_usec_delta, mem_current, mem_swap,
        #                     io_rbytes_delta, io_wbytes_delta, pids
        by_mem = sorted(
            rows,
            key=lambda r: (r[3] if r[3] is not None else 0),
            reverse=True,
        )[:5]

        def _row_to_dict(r: tuple) -> dict:
            return {"ts": r[0], "cg_id": r[1], "mem_current": r[3]}

        return json.dumps({"by_mem": [_row_to_dict(r) for r in by_mem]})

    # ------------------------------------------------------------------
    # Scheduled job callables
    # ------------------------------------------------------------------

    def _job_system(self) -> None:
        """Tier-1 job: sample system, check trigger, update selfmeter."""
        sampler = self._samplers.get("system")
        if sampler is None:
            return

        batches = sampler.run(self._ctx)
        self._buffer_batches(batches)

        # Retrieve the snapshot that T1 deposits on the context.
        snap = getattr(sampler, "last_snapshot", None)

        # Spike-trigger check.
        if snap is not None and self._trigger is not None:
            decision = self._trigger.observe(snap, self._clock())
            if decision is not None:
                try:
                    self._handle_spike(decision, snap)
                except Exception:
                    logger.exception("スパイク処理例外")

        # Selfmeter update.
        if self._selfmeter is not None:
            throttle_result = self._selfmeter.update(self._clock())
            if throttle_result is not None:
                self._storage.record_event(throttle_result, "selfmeter自動減速")

    def _job_cgroup(self) -> None:
        """Tier-2a job: cgroup sampling."""
        sampler = self._samplers.get("cgroup")
        if sampler is None:
            return
        batches = sampler.run(self._ctx)
        self._buffer_batches(batches)

    def _job_process(self) -> None:
        """Tier-2b job: process sampling."""
        sampler = self._samplers.get("process")
        if sampler is None:
            return
        batches = sampler.run(self._ctx)
        self._buffer_batches(batches)

    def _job_disk(self) -> None:
        """Tier-3 job: disk usage sampling."""
        sampler = self._samplers.get("disk")
        if sampler is None:
            return
        batches = sampler.run(self._ctx)
        self._buffer_batches(batches)

    def _job_dir_scan(self) -> None:
        """Tier-3 dir job: watch-dir size scan."""
        sampler = self._samplers.get("disk")
        if sampler is None or not hasattr(sampler, "collect_dirs"):
            return
        try:
            batches = sampler.collect_dirs(self._ctx)
            self._buffer_batches(batches)
        except Exception:
            logger.exception("ディレクトリ走査例外")

    def _job_flush(self) -> None:
        """Flush buffer, run incremental rollup, and (hourly) retention."""
        self._storage.flush(reason="interval")
        self._storage.rollup_incremental()

        now_mono = self._clock()
        if now_mono - self._last_retention_mono >= self._retention_interval:
            self._last_retention_mono = now_mono
            try:
                self._storage.retention(
                    self._config.retention.raw,
                    self._config.retention.rollup,
                )
            except Exception:
                logger.exception("保持ポリシー実行例外")

    def _on_gap(self, delay: float, job_name: str) -> None:
        """Record a gap event when a job fires more than 3× its interval late."""
        detail = f"{delay:.1f}s ジョブ{job_name}"
        self._storage.record_event("gap", detail)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Run the record loop; return 0 on normal exit."""
        self._start_mono = self._clock()
        self._last_retention_mono = self._start_mono

        # Write session metadata.
        try:
            self._write_meta()
        except Exception:
            logger.exception("メタデータ書き込み例外")

        # Setup all samplers.
        for name, sampler in self._samplers.items():
            try:
                sampler.setup(self._ctx)
            except Exception:
                logger.exception("サンプラーsetup例外 (name=%s)", name)

        # Attach selfmeter to context.
        if self._selfmeter is not None:
            self._ctx.selfmeter = self._selfmeter

        # Build the scheduler.
        sched = TierScheduler(
            clock=self._clock,
            sleep=self._sleep,
            on_gap=self._on_gap,
        )

        sc = self._config.sampling
        sched.add("t1_system", sc.system_interval, self._job_system)
        sched.add("t2a_cgroup", sc.cgroup_interval, self._job_cgroup)

        # T2b interval is a callable so the selfmeter factor is evaluated
        # each scheduling cycle.  The widened interval is capped at the
        # design ceiling (§4.8: 上限60秒) regardless of the factor.
        if self._selfmeter is not None:
            max_interval = self._config.budget.throttle_max_interval
            sched.add(
                "t2b_process",
                lambda: min(sc.process_interval * self._selfmeter.factor, max_interval),
                self._job_process,
            )
        else:
            sched.add("t2b_process", sc.process_interval, self._job_process)

        sched.add("t3_disk", sc.disk_interval, self._job_disk)
        sched.add("t3_dir", sc.dir_scan_interval, self._job_dir_scan)
        sched.add("flush", self._flush_interval, self._job_flush)

        # Register signal handlers (main thread only).
        self._register_signals()

        # Duration-based stop: add a lightweight polling job to the scheduler
        # that checks the monotonic clock and sets _stop when the duration
        # has elapsed.  Using the scheduler avoids a second thread that would
        # consume the injected sleep in parallel with the main loop.
        if self._duration is not None:
            deadline = self._start_mono + self._duration

            def _check_duration() -> None:
                if self._clock() >= deadline:
                    self._stop.set()

            # Poll often enough to be responsive but not so often that it adds
            # overhead: every system_interval is fine.
            sched.add("_duration_check", sc.system_interval, _check_duration)

        # Run the scheduler loop (blocks until _stop is set).
        try:
            sched.run(self._stop)
        except Exception:
            logger.exception("スケジューラループ例外")

        # Graceful shutdown.
        try:
            self._storage.flush(reason="signal")
            self._storage.record_event("stopped", "正常終了")
        except Exception:
            logger.exception("シャットダウン例外")

        if self._owns_storage:
            try:
                self._storage.close()
            except Exception:
                logger.exception("Storage close 例外")

        return 0

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _register_signals(self) -> None:
        """Register SIGTERM/SIGINT handlers if we are in the main thread."""
        if threading.current_thread() is not threading.main_thread():
            return

        def _handler(signum: int, frame: Any) -> None:
            logger.info("シグナル %d を受信しました — 停止します", signum)
            self._stop.set()

        try:
            signal.signal(signal.SIGTERM, _handler)
            signal.signal(signal.SIGINT, _handler)
        except (OSError, ValueError):
            # Can happen in non-main threads or restricted environments.
            pass
