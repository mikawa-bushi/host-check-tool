"""Tests for procdiag.scheduler.TierScheduler and procdiag.recorder.Recorder.

All tests use fake clocks, fake sleeps, and fake sampler/trigger/selfmeter
implementations so real sampler modules (T02-T04) are never imported.
Real elapsed wall-clock time is kept well under 1 second.

Approach
--------
TierScheduler tests inject a *counting* fake sleep that advances the clock
and terminates the loop after a predetermined number of sleep calls.  The
scheduler is run in a daemon thread with a real-time safety timeout.

Recorder tests call ``run()`` directly in the test thread so that the SQLite
connection is used by the same OS thread that created it.  A fake sleep
advances the fake clock; a duration watcher detects expiry and sets the stop
event, so ``run()`` returns without any real-time waiting.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from procdiag.config import ProcdiagConfig, SamplingConfig
from procdiag.core.sampler import RunContext, SampleBatch, Sampler, SystemSnapshot
from procdiag.recorder import Recorder
from procdiag.scheduler import TierScheduler
from procdiag.storage import Storage

# ---------------------------------------------------------------------------
# Helpers: fake clock and sleep
# ---------------------------------------------------------------------------


class FakeClock:
    """Injected monotonic clock that advances only on explicit advance() calls."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, delta: float) -> None:
        self._t += delta


class CountingSleep:
    """Sleep callable for TierScheduler tests.

    Advances the fake clock and sets the stop event after *max_calls* calls.
    This terminates the scheduler loop without any real-time waiting.
    """

    def __init__(
        self,
        clock: FakeClock,
        stop: threading.Event,
        max_calls: int,
    ) -> None:
        self._clock = clock
        self._stop = stop
        self._max = max_calls
        self.calls: List[float] = []

    def __call__(self, secs: float) -> None:
        self._clock.advance(secs)
        self.calls.append(secs)
        if len(self.calls) >= self._max:
            self._stop.set()


# ---------------------------------------------------------------------------
# TierScheduler tests (scheduler runs in a daemon thread)
# ---------------------------------------------------------------------------


class TestTierScheduler:
    """Unit tests for TierScheduler that never wait on real time."""

    @staticmethod
    def _run_sched(
        sched: TierScheduler,
        stop: threading.Event,
        timeout_real: float = 3.0,
    ) -> None:
        """Run the scheduler in a daemon thread; join with a real-time cap."""
        t = threading.Thread(target=sched.run, args=(stop,), daemon=True)
        t.start()
        t.join(timeout=timeout_real)
        stop.set()

    def test_1s_job_fires_expected_times(self):
        """A 1-second job fires roughly N times over N seconds of fake time."""
        clock = FakeClock(0.0)
        calls: List[float] = []
        stop = threading.Event()
        fake_sleep = CountingSleep(clock, stop, max_calls=20)

        sched = TierScheduler(clock=clock, sleep=fake_sleep)
        sched.add("t1", 1.0, lambda: calls.append(clock()))

        self._run_sched(sched, stop)

        # 20 sleep calls × ≤0.5 s each = ≤10 s of fake time → 1s job fires ~10×
        assert len(calls) >= 5, f"Expected ≥5 calls, got {len(calls)}"

    def test_5s_job_fires_less_than_1s_job(self):
        """A 5-second job fires fewer times than a 1-second job."""
        clock = FakeClock(0.0)
        t1_calls: List[float] = []
        t5_calls: List[float] = []
        stop = threading.Event()
        fake_sleep = CountingSleep(clock, stop, max_calls=40)

        sched = TierScheduler(clock=clock, sleep=fake_sleep)
        sched.add("t1", 1.0, lambda: t1_calls.append(clock()))
        sched.add("t5", 5.0, lambda: t5_calls.append(clock()))

        self._run_sched(sched, stop)

        assert len(t1_calls) > len(t5_calls), f"t1={len(t1_calls)}, t5={len(t5_calls)}"
        assert len(t5_calls) >= 1, "5s job never fired"

    def test_callable_interval_evaluated_each_cycle(self):
        """A callable interval is re-evaluated before each scheduling decision."""
        clock = FakeClock(0.0)
        calls: List[float] = []
        stop = threading.Event()
        fake_sleep = CountingSleep(clock, stop, max_calls=20)

        state = {"n": 0}

        def dynamic_interval() -> float:
            return 1.0 + state["n"] * 0.5  # 1.0, 1.5, 2.0, ...

        def job() -> None:
            calls.append(clock())
            state["n"] += 1

        sched = TierScheduler(clock=clock, sleep=fake_sleep)
        sched.add("dyn", dynamic_interval, job)

        self._run_sched(sched, stop)

        assert len(calls) >= 2, "callable-interval job must fire at least twice"
        if len(calls) >= 3:
            gap1 = calls[1] - calls[0]
            gap2 = calls[2] - calls[1]
            # Each gap should be ≥ the previous one (interval grows).
            assert (
                gap2 >= gap1 - 0.1
            ), f"Expected growing gaps: gap1={gap1:.2f}, gap2={gap2:.2f}"

    def test_job_exception_does_not_stop_loop(self):
        """A job that raises must not stop the loop."""
        clock = FakeClock(0.0)
        good_calls: List[float] = []
        bad_calls: List[float] = []
        stop = threading.Event()
        fake_sleep = CountingSleep(clock, stop, max_calls=20)

        def bad_job() -> None:
            bad_calls.append(clock())
            raise RuntimeError("壊れたジョブ")

        def good_job() -> None:
            good_calls.append(clock())

        sched = TierScheduler(clock=clock, sleep=fake_sleep)
        sched.add("bad", 1.0, bad_job)
        sched.add("good", 1.0, good_job)

        self._run_sched(sched, stop)

        assert len(good_calls) >= 2, f"good_calls={good_calls}"
        assert len(bad_calls) >= 2, f"bad_calls={bad_calls}"

    def test_on_gap_fires_when_clock_jumps(self):
        """on_gap is called when a job is more than 3× its interval late."""
        gap_events: List[tuple] = []

        def on_gap(delay: float, name: str) -> None:
            gap_events.append((delay, name))

        clock = FakeClock(0.0)
        stop = threading.Event()
        call_count = [0]

        def controlled_sleep(secs: float) -> None:
            clock.advance(secs)
            call_count[0] += 1
            if call_count[0] == 3:
                clock.advance(15.0)  # simulate a big gap
            if call_count[0] >= 8:
                stop.set()

        sched = TierScheduler(clock=clock, sleep=controlled_sleep, on_gap=on_gap)
        sched.add("gap_test", 1.0, lambda: None)

        t = threading.Thread(target=sched.run, args=(stop,), daemon=True)
        t.start()
        t.join(timeout=3.0)
        stop.set()

        assert len(gap_events) >= 1, "on_gap was never called"
        delay, name = gap_events[0]
        assert delay >= 9.0, f"Expected gap ≥9s, got {delay}"
        assert name == "gap_test"

    def test_on_gap_reanchors_next_t(self):
        """After a gap, only one catch-up fire should happen (no burst)."""
        clock = FakeClock(0.0)
        calls_after_init: List[float] = []
        stop = threading.Event()
        phase = [0]

        def controlled_sleep(secs: float) -> None:
            clock.advance(secs)
            phase[0] += 1
            if phase[0] == 2:
                clock.advance(100.0)  # huge gap
            if phase[0] >= 10:
                stop.set()

        first_call = [True]

        def job() -> None:
            if not first_call[0]:
                calls_after_init.append(clock())
            first_call[0] = False

        sched = TierScheduler(clock=clock, sleep=controlled_sleep)
        sched.add("reanchor", 1.0, job)

        t = threading.Thread(target=sched.run, args=(stop,), daemon=True)
        t.start()
        t.join(timeout=3.0)
        stop.set()

        # After the 100 s gap, at most a few calls should follow — no burst.
        assert (
            len(calls_after_init) <= 5
        ), f"Too many catch-up calls: {len(calls_after_init)}: {calls_after_init}"


# ---------------------------------------------------------------------------
# Fake sampler / trigger / selfmeter implementations
# ---------------------------------------------------------------------------


class FakeSampler(Sampler):
    """Returns a single sys_samples row on every collect()."""

    name = "system"
    tier = "T1"

    def __init__(self) -> None:
        self.setup_called = False
        self.run_count = 0
        self.last_snapshot: Optional[SystemSnapshot] = None

    def setup(self, ctx: RunContext) -> None:
        self.setup_called = True

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        self.run_count += 1
        ts = 1000.0 + self.run_count
        self.last_snapshot = SystemSnapshot(
            ts=ts,
            cpu_total=10.0,
            cpu_max_core=20.0,
            mem_total=8 * 1024**3,
            mem_available=4 * 1024**3,
            swap_out_delta=0,
            psi=None,
        )
        row = (
            ts,  # ts
            10.0,  # cpu_total
            20.0,  # cpu_max_core
            1024,  # mem_used
            4096,  # mem_available
            0,  # swap_used
            0,  # swap_in
            0,  # swap_out
            0.1,  # load1
            None,  # psi_cpu_some10
            None,  # psi_mem_some10
            None,  # psi_mem_full10
            None,  # psi_io_some10
            None,  # psi_io_full10
            0,  # disk_read_bytes
            0,  # disk_write_bytes
            0.0,  # self_cpu
            1024,  # self_rss
        )
        return [SampleBatch(table="sys_samples", rows=[row])]


class FakeCgroupSampler(Sampler):
    """Returns a single cg_samples row on every collect()."""

    name = "cgroup"
    tier = "T2a"

    def __init__(self) -> None:
        self.run_count = 0

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        self.run_count += 1
        ts = 2000.0 + self.run_count
        row = (ts, 1, 100, 512, 0, 0, 0, 2)
        return [SampleBatch(table="cg_samples", rows=[row])]


class FakeProcessSampler(Sampler):
    """Returns a single proc_samples row on every collect()."""

    name = "process"
    tier = "T2b"

    def __init__(self) -> None:
        self.run_count = 0

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        self.run_count += 1
        ts = 3000.0 + self.run_count
        row = (ts, 1, 1234, 5.0, 1024, None, 4, 10, None, None)
        return [SampleBatch(table="proc_samples", rows=[row])]


class FakeDiskSampler(Sampler):
    """Returns disk_samples rows from collect() and dir_samples from collect_dirs()."""

    name = "disk"
    tier = "T3"

    def __init__(self) -> None:
        self.run_count = 0
        self.dir_scan_count = 0

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        self.run_count += 1
        ts = 4000.0 + self.run_count
        row = (ts, "/", 100 * 1024**3, 50 * 1024**3, None, None, 0)
        return [SampleBatch(table="disk_samples", rows=[row])]

    def collect_dirs(self, ctx: RunContext) -> list[SampleBatch]:
        self.dir_scan_count += 1
        ts = 5000.0 + self.dir_scan_count
        row = (ts, "/var/log", 1024**2, 0)
        return [SampleBatch(table="dir_samples", rows=[row])]


@dataclass
class FakeSpikeDecision:
    trigger_type: str
    reason: str
    psi_snapshot: Optional[dict]


class FakeTrigger:
    """Fires a spike on exactly the N-th call to observe()."""

    def __init__(self, fire_on_call: int = 2) -> None:
        self._call_count = 0
        self._fire_on = fire_on_call
        self.fired_count = 0

    def observe(
        self, snap: SystemSnapshot, mono_now: float
    ) -> Optional[FakeSpikeDecision]:
        self._call_count += 1
        if self._call_count == self._fire_on:
            self.fired_count += 1
            return FakeSpikeDecision(
                trigger_type="memory",
                reason="テスト発火",
                psi_snapshot={"mem_some10": 10.0},
            )
        return None


class FakeSelfmeter:
    """Simulates selfmeter with a configurable one-shot throttle response."""

    def __init__(self, throttle_on_call: Optional[int] = None) -> None:
        self._call_count = 0
        self._fire_on = throttle_on_call
        self.factor: float = 1.0
        self.results: List[Optional[str]] = []

    def sample(self) -> tuple[float, int]:
        return 0.5, 1024 * 1024

    def update(self, mono_now: float) -> Optional[str]:
        self._call_count += 1
        if self._fire_on is not None and self._call_count == self._fire_on:
            self.factor = 1.5
            result: Optional[str] = "throttle_down"
        else:
            result = None
        self.results.append(result)
        return result


# ---------------------------------------------------------------------------
# Recorder helpers
# ---------------------------------------------------------------------------


def _make_config(
    system_interval: float = 0.1,
    cgroup_interval: float = 0.3,
    process_interval: float = 0.5,
    disk_interval: float = 2.0,
    dir_scan_interval: float = 5.0,
) -> ProcdiagConfig:
    return ProcdiagConfig(
        sampling=SamplingConfig(
            system_interval=system_interval,
            cgroup_interval=cgroup_interval,
            process_interval=process_interval,
            disk_interval=disk_interval,
            dir_scan_interval=dir_scan_interval,
        )
    )


def _make_samplers() -> dict[str, Sampler]:
    return {
        "system": FakeSampler(),
        "cgroup": FakeCgroupSampler(),
        "process": FakeProcessSampler(),
        "disk": FakeDiskSampler(),
    }


class _AdvancingSleep:
    """Sleep callable that advances the fake clock.

    Used in Recorder tests where run() is called directly (same thread as
    the Storage connection).  The recorder's duration-watcher thread fires
    the stop event when the fake clock reaches the duration.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def __call__(self, secs: float) -> None:
        self._clock.advance(secs)


# ---------------------------------------------------------------------------
# Recorder integration tests
# ---------------------------------------------------------------------------


class TestRecorder:
    """Integration tests for Recorder run directly in the test thread."""

    def test_sys_samples_populated(self, tmp_path: Path):
        """After a short run, sys_samples must contain at least one row."""
        config = _make_config()
        clock = FakeClock(0.0)
        fake_sleep = _AdvancingSleep(clock)
        samplers = _make_samplers()

        rec = Recorder(
            config,
            db_path=tmp_path / "test.db",
            samplers=samplers,
            duration=1.0,
            clock=clock,
            sleep=fake_sleep,
        )
        result = rec.run()

        assert result == 0
        storage = Storage(tmp_path / "test.db")
        rows = storage.conn.execute("SELECT COUNT(*) AS c FROM sys_samples").fetchone()
        assert rows["c"] >= 1, "sys_samples must have at least one row"

    def test_spike_event_recorded(self, tmp_path: Path):
        """A spike on the 2nd T1 call produces a spike_events row with valid JSON."""
        config = _make_config()
        clock = FakeClock(0.0)
        fake_sleep = _AdvancingSleep(clock)
        samplers = _make_samplers()
        trigger = FakeTrigger(fire_on_call=2)

        rec = Recorder(
            config,
            db_path=tmp_path / "test.db",
            samplers=samplers,
            trigger=trigger,
            duration=1.0,
            clock=clock,
            sleep=fake_sleep,
        )
        rec.run()

        storage = Storage(tmp_path / "test.db")
        rows = storage.conn.execute("SELECT * FROM spike_events").fetchall()
        assert len(rows) >= 1, "spike_events must have at least one row"

        for row in rows:
            psi_data = json.loads(row["psi_snapshot"])
            assert isinstance(psi_data, dict)
            top_procs = json.loads(row["top_processes"])
            assert "by_cpu" in top_procs
            assert "by_rss" in top_procs
            top_cgroups = json.loads(row["top_cgroups"])
            assert "by_mem" in top_cgroups

    def test_on_gap_dedupes_simultaneous_job_reports(self, tmp_path: Path):
        """Jobs reporting the same gap in one iteration record one event (§3-6)."""
        rec = Recorder(
            _make_config(),
            db_path=tmp_path / "gap.db",
            samplers=_make_samplers(),
            duration=1.0,
            clock=FakeClock(0.0),
            sleep=lambda _s: None,
        )
        # Three jobs reporting the same resume gap within one scheduler tick.
        rec._on_gap(7200.0, "process")
        rec._on_gap(7200.0, "system")
        rec._on_gap(7200.0, "cgroup")
        n = rec._storage.conn.execute(
            "SELECT COUNT(*) AS c FROM ops_events WHERE kind='gap'"
        ).fetchone()["c"]
        assert n == 1

    def test_ops_events_has_stopped(self, tmp_path: Path):
        """ops_events must contain a 'stopped' row after normal shutdown."""
        config = _make_config()
        clock = FakeClock(0.0)
        fake_sleep = _AdvancingSleep(clock)
        samplers = _make_samplers()

        rec = Recorder(
            config,
            db_path=tmp_path / "test.db",
            samplers=samplers,
            duration=0.5,
            clock=clock,
            sleep=fake_sleep,
        )
        rec.run()

        storage = Storage(tmp_path / "test.db")
        rows = storage.conn.execute(
            "SELECT * FROM ops_events WHERE kind='stopped'"
        ).fetchall()
        assert len(rows) >= 1, "ops_events must contain a 'stopped' row"

    def test_meta_populated(self, tmp_path: Path):
        """meta table must have hostname, kernel, started_ts, and config entries."""
        config = _make_config()
        clock = FakeClock(0.0)
        fake_sleep = _AdvancingSleep(clock)
        samplers = _make_samplers()

        rec = Recorder(
            config,
            db_path=tmp_path / "test.db",
            samplers=samplers,
            duration=0.3,
            clock=clock,
            sleep=fake_sleep,
        )
        rec.run()

        storage = Storage(tmp_path / "test.db")
        meta = storage.meta()
        assert "hostname" in meta, "meta must have hostname"
        assert "kernel" in meta, "meta must have kernel"
        assert "started_ts" in meta, "meta must have started_ts"
        assert "config" in meta, "meta must have config"
        cfg_data = json.loads(meta["config"])
        assert "sampling" in cfg_data

    def test_throttle_ops_event(self, tmp_path: Path):
        """selfmeter returning throttle_down is recorded in ops_events."""
        config = _make_config()
        clock = FakeClock(0.0)
        fake_sleep = _AdvancingSleep(clock)
        samplers = _make_samplers()
        selfmeter = FakeSelfmeter(throttle_on_call=2)

        rec = Recorder(
            config,
            db_path=tmp_path / "test.db",
            samplers=samplers,
            selfmeter=selfmeter,
            duration=1.0,
            clock=clock,
            sleep=fake_sleep,
        )
        rec.run()

        storage = Storage(tmp_path / "test.db")
        rows = storage.conn.execute(
            "SELECT * FROM ops_events WHERE kind='throttle_down'"
        ).fetchall()
        assert len(rows) >= 1, "throttle_down must appear in ops_events"

    def test_t2b_callable_interval_respects_factor(self, tmp_path: Path):
        """T2b callable interval reflects selfmeter.factor (fires less often)."""
        config = _make_config(
            system_interval=0.05,
            process_interval=0.2,
        )
        clock = FakeClock(0.0)
        fake_sleep = _AdvancingSleep(clock)
        samplers = _make_samplers()

        class SlowMeter:
            factor: float = 2.0

            def sample(self) -> tuple[float, int]:
                return 0.0, 0

            def update(self, mono_now: float) -> Optional[str]:
                return None

        selfmeter = SlowMeter()

        rec = Recorder(
            config,
            db_path=tmp_path / "test.db",
            samplers=samplers,
            selfmeter=selfmeter,
            duration=2.0,
            clock=clock,
            sleep=fake_sleep,
        )
        rec.run()

        t1 = samplers["system"].run_count  # type: ignore[attr-defined]
        proc = samplers["process"].run_count  # type: ignore[attr-defined]

        # With factor=2 the process sampler fires at 0.4s intervals vs 0.05s T1.
        # T1 should fire significantly more often.
        assert t1 > proc, f"T1 count ({t1}) should exceed process count ({proc})"

    def test_sampler_exception_does_not_crash_recorder(self, tmp_path: Path):
        """A sampler that always raises must not crash run()."""

        class BrokenSampler(Sampler):
            name = "system"
            tier = "T1"

            @property
            def last_snapshot(self) -> None:
                return None

            def collect(self, ctx: RunContext) -> list[SampleBatch]:
                raise RuntimeError("常に失敗")

        config = _make_config()
        clock = FakeClock(0.0)
        fake_sleep = _AdvancingSleep(clock)

        rec = Recorder(
            config,
            db_path=tmp_path / "test.db",
            samplers={
                "system": BrokenSampler(),
                "cgroup": FakeCgroupSampler(),
                "process": FakeProcessSampler(),
                "disk": FakeDiskSampler(),
            },
            duration=0.5,
            clock=clock,
            sleep=fake_sleep,
        )
        result = rec.run()
        assert result == 0, "run() must return 0 even when a sampler always raises"
