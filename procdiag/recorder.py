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

import logging
import platform
import signal
import socket
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import spikes
from .config import ProcdiagConfig
from .core.sampler import RunContext, SampleBatch, Sampler
from .core.util import MiB
from .scheduler import TierScheduler
from .storage import TABLE_COLUMNS, Storage

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
        self._last_gap_mono: float = float("-inf")
        self._boot_time: float = 0.0
        # sessions row of this run (None when it could not be opened).
        self._session_id: Optional[int] = None
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
        from .samplers.cgroup import CgroupSampler  # type: ignore[import]
        from .samplers.disk import DiskSampler  # type: ignore[import]
        from .samplers.kernel_log import KernelLogSampler  # type: ignore[import]
        from .samplers.process import ProcessSampler  # type: ignore[import]
        from .samplers.service import ServiceSampler  # type: ignore[import]
        from .samplers.system import SystemSampler  # type: ignore[import]
        from .samplers.thermal import ThermalSampler  # type: ignore[import]

        return {
            "system": SystemSampler(),
            "cgroup": CgroupSampler(resolve_cg=self._storage.cg_id_for),
            "process": ProcessSampler(
                resolve_proc=self._storage.proc_id_for,
                record_fd_limits=self._storage.set_proc_fd_limits,
            ),
            "disk": DiskSampler(),
            "thermal": ThermalSampler(),
            "service": ServiceSampler(),
            "kernel_log": KernelLogSampler(),
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
        self._boot_time = boot_time

        self._storage.write_meta(
            {
                "hostname": socket.gethostname(),
                "kernel": platform.release(),
                "boot_time": str(boot_time),
                "procdiag_version": _VERSION,
                "config": self._config.model_dump_json(),
                "started_ts": str(time.time()),
            }
        )

    # ------------------------------------------------------------------
    # Sampling helpers
    # ------------------------------------------------------------------

    def _run_sampler(self, name: str) -> list[SampleBatch]:
        """Run the named sampler (when configured) and buffer its batches.

        Returns the batches so callers (the spike handler) can also inspect the
        rows they just recorded.  ``Sampler.run`` never raises.
        """
        sampler = self._samplers.get(name)
        if sampler is None:
            return []
        batches = sampler.run(self._ctx)
        for batch in batches:
            self._storage.buffer(batch.table, batch.rows)
        return batches

    # ------------------------------------------------------------------
    # Spike handling
    # ------------------------------------------------------------------

    def _handle_spike(self, decision: Any, snap: Any) -> None:
        """Run T2a+T2b immediately and record a spike_events row.

        The spike_events row is self-contained: top_processes / top_cgroups
        carry the culprit snapshot inline, so committing it atomically is
        sufficient — a power cut can never leave a spike_events row that
        "references" missing data.  The freshly buffered cg/proc samples are
        persisted by the next interval flush; we deliberately do *not* force an
        extra fsync here, since this runs on the 1-second T1 thread precisely
        when the host is already under pressure (WAL + synchronous=NORMAL makes
        the deferred flush equally durable).
        """
        cg_batches = self._run_sampler("cgroup")
        proc_batches = self._run_sampler("process")

        self._storage.record_spike(
            trigger_type=getattr(decision, "trigger_type", "unknown"),
            psi_snapshot=spikes.encode_psi_snapshot(
                getattr(decision, "psi_snapshot", None)
            ),
            top_processes=spikes.encode_top_processes(
                _records(proc_batches, "proc_samples")
            ),
            top_cgroups=spikes.encode_top_cgroups(_records(cg_batches, "cg_samples")),
        )

    # ------------------------------------------------------------------
    # Scheduled job callables
    # ------------------------------------------------------------------

    def _job_system(self) -> None:
        """Tier-1 job: sample system, check trigger, update selfmeter."""
        sampler = self._samplers.get("system")
        if sampler is None:
            return

        self._run_sampler("system")

        # Retrieve the snapshot that T1 deposits on the sampler.
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

    def _job_dir_scan(self) -> None:
        """Tier-3 dir job: watch-dir size scan."""
        sampler = self._samplers.get("disk")
        if sampler is None or not hasattr(sampler, "collect_dirs"):
            return
        try:
            for batch in sampler.collect_dirs(self._ctx):
                self._storage.buffer(batch.table, batch.rows)
        except Exception:
            logger.exception("ディレクトリ走査例外")

    def _job_flush(self) -> None:
        """Flush buffer, run incremental rollup, and (hourly) retention."""
        self._storage.flush(reason="interval")
        self._storage.rollup_incremental()
        if self._session_id is not None:
            # Heartbeat: after a power cut this is when the host went down.
            try:
                self._storage.touch_session(self._session_id)
            except Exception:
                logger.exception("セッション生存記録の例外")

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

            # DB size guard — run immediately after retention so freed pages
            # are already gone before we compute the size.
            try:
                ret = self._config.retention
                self._storage.enforce_size_budget(
                    max_bytes=(
                        int(ret.db_max_size_mb * MiB)
                        if ret.db_max_size_mb is not None
                        else None
                    ),
                    min_free_bytes=int(ret.min_free_disk_mb * MiB),
                )
            except Exception:
                logger.exception("DBサイズガード実行例外")

    def _job_pss_precheck(self) -> None:
        """PSS pre-check job: update pss_targets on the process sampler.

        Reads the recent proc_rollup_1m window and lets
        :func:`~procdiag.diagnosis.memory.select_pss_targets` pick the
        fastest-growing RSS series; the result becomes the process sampler's
        pss_targets.  A changed target set is logged as a ``pss_scrutiny``
        event.  Uses hasattr to remain a no-op when the injected sampler does
        not expose pss_targets (e.g. fake samplers in test_recorder.py).
        """
        from .diagnosis.memory import (  # local import: keeps startup light
            PSS_PRECHECK_WINDOW_HOURS,
            select_pss_targets,
        )

        sampler = self._samplers.get("process")
        if sampler is None or not hasattr(sampler, "pss_targets"):
            return

        now = time.time()
        try:
            proc_rows = self._storage.rows(
                "proc_rollup_1m",
                since=now - PSS_PRECHECK_WINDOW_HOURS * 3600.0,
                until=now,
            )
        except Exception:
            logger.exception("PSS事前チェック: proc_rollup_1m 読み取り例外")
            return

        ranked = select_pss_targets(proc_rows)
        new_targets = set(ranked)
        if new_targets != set(sampler.pss_targets):
            labels = [f"proc_id={pid} name={self._proc_label(pid)}" for pid in ranked]
            self._storage.record_event(
                "pss_scrutiny", "; ".join(labels) if labels else "(なし)"
            )

        sampler.pss_targets = new_targets

    def _proc_label(self, proc_id: int) -> str:
        """Registered process name for an ops_events label (never raises)."""
        try:
            return self._storage.proc_name(proc_id)
        except Exception:
            return f"proc_{proc_id}"

    def _on_gap(self, delay: float, job_name: str) -> None:
        """Record a gap event when a job fires more than 3× its interval late.

        On a suspend/resume every registered job fires on_gap in the *same*
        scheduler iteration (microseconds apart on the monotonic clock).  Record
        the gap only once rather than once per job, otherwise the same real-time
        gap is counted N times and ObservationQuality.gap_seconds is inflated by
        the job count.  A 0.5 s window collapses same-iteration reports while
        preserving genuinely separate gaps (≥ the 1 s T1 interval apart).
        """
        mono = time.monotonic()
        if mono - self._last_gap_mono < 0.5:
            return
        self._last_gap_mono = mono
        # The leading "<delay>s" token is parsed back by
        # engine._parse_gap_delay_seconds to compute ObservationQuality.gap_seconds.
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

        # Open this run's session row (UNCLEAN-SHUTDOWN-01 compares sessions).
        try:
            self._session_id = self._storage.start_session(
                boot_time=self._boot_time,
                hostname=socket.gethostname(),
                procdiag_version=_VERSION,
            )
        except Exception:
            logger.exception("セッション開始記録の例外")

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
        sched.add(
            "t2a_cgroup", sc.cgroup_interval, partial(self._run_sampler, "cgroup")
        )
        job_process = partial(self._run_sampler, "process")

        # T2b interval is a callable so the selfmeter factor is evaluated
        # each scheduling cycle.  The widened interval is capped at the
        # design ceiling (§4.8: 上限60秒) regardless of the factor.
        if self._selfmeter is not None:
            max_interval = self._config.budget.throttle_max_interval
            sched.add(
                "t2b_process",
                lambda: min(sc.process_interval * self._selfmeter.factor, max_interval),
                job_process,
            )
        else:
            sched.add("t2b_process", sc.process_interval, job_process)

        sched.add("t3_disk", sc.disk_interval, partial(self._run_sampler, "disk"))
        sched.add(
            "t3_thermal", sc.thermal_interval, partial(self._run_sampler, "thermal")
        )
        sched.add(
            "t3_service", sc.service_interval, partial(self._run_sampler, "service")
        )
        sched.add(
            "t3_kernel_log",
            sc.kernel_log_interval,
            partial(self._run_sampler, "kernel_log"),
        )
        sched.add("t3_dir", sc.dir_scan_interval, self._job_dir_scan)
        sched.add("flush", self._flush_interval, self._job_flush)

        # PSS pre-check job: only register when pss_precheck_interval > 0.
        if sc.pss_precheck_interval > 0:
            sched.add("pss_precheck", sc.pss_precheck_interval, self._job_pss_precheck)

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
            # Roll up the just-flushed final samples so the last (up to one
            # flush-interval + 1 min) of raw data is promoted to *_rollup_1m
            # before raw retention can ever delete it.  Without this, the
            # most diagnostically valuable window — right before a crash or
            # manual stop — is lost from the rollup tables that trend/leak
            # rules read.  rollup_incremental is watermark-based and
            # idempotent, so it is safe on the stop path.
            self._storage.rollup_incremental()
            self._storage.record_event("stopped", "正常終了")
            if self._session_id is not None:
                self._storage.end_session(self._session_id)
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
        """Register SIGTERM/SIGINT/SIGHUP handlers if we are in the main thread.

        SIGHUP (the terminal running a foreground ``record`` was closed) is a
        deliberate stop too; without a handler the process would die without
        closing its session, which would read as an unclean shutdown.
        """
        if threading.current_thread() is not threading.main_thread():
            return

        def _handler(signum: int, frame: Any) -> None:
            logger.info("シグナル %d を受信しました — 停止します", signum)
            self._stop.set()

        try:
            signal.signal(signal.SIGTERM, _handler)
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGHUP, _handler)
        except (OSError, ValueError):
            # Can happen in non-main threads or restricted environments.
            pass


def _records(batches: list[SampleBatch], table: str) -> list[dict]:
    """Rows of *table* from *batches* as column-name dicts."""
    cols = TABLE_COLUMNS[table]
    return [dict(zip(cols, row)) for b in batches if b.table == table for row in b.rows]
