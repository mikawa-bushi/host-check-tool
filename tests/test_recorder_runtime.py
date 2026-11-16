"""Recorder runtime behavior: spike culprits, self-throttle, threads, dir walks.

Runs ``Recorder.run()`` with the fakes of ``test_recorder``; only what comes
from the OS (procdiag's own CPU reading, the process table) is faked here.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock, patch

import psutil
import pytest
from test_recorder import (
    FakeClock,
    FakeDiskSampler,
    FakeProcessSampler,
    FakeSampler,
    FakeSelfmeter,
    FakeTrigger,
    _AdvancingSleep,
    _make_config,
    _make_samplers,
)

from procdiag import spikes
from procdiag.config import ProcdiagConfig, SamplingConfig, WatchDir
from procdiag.core.sampler import Note, RunContext, SampleBatch
from procdiag.recorder import Recorder
from procdiag.samplers.process import ProcessSampler
from procdiag.samplers.selfmeter import SelfMeter
from procdiag.storage import Storage


def _count(db: Path, table: str) -> int:
    with Storage(db) as store:
        return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _peek(db: Path, sql: str) -> list[tuple]:
    """Rows of *sql* as another process reading the DB of a running recorder sees them."""
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as con:
        return con.execute(sql).fetchall()


# ---------------------------------------------------------------------------
# Spike culprits (B56)
# ---------------------------------------------------------------------------


def _fake_process(pid: int, name: str, *, cpu_seconds: float, age: float) -> MagicMock:
    """A process that started *age* seconds ago and has used *cpu_seconds* since."""
    p = MagicMock(spec=psutil.Process)
    p.pid = pid
    p.name.return_value = name
    p.cmdline.return_value = [name]
    p.status.return_value = psutil.STATUS_RUNNING
    # psutil's first cpu_percent() has no earlier reading to compare with.
    p.cpu_percent.return_value = 0.0
    p.cpu_times.return_value = MagicMock(user=cpu_seconds, system=0.0)
    # psutil adds the boot time of /proc/stat (whole seconds) to the start
    # time since boot.
    p.create_time.return_value = (
        psutil.boot_time() + time.clock_gettime(time.CLOCK_BOOTTIME) - age
    )
    p.memory_info.return_value = MagicMock(rss=10 * 1024**2)
    p.num_threads.return_value = 1
    p.num_fds.return_value = 3
    p.io_counters.return_value = MagicMock(read_bytes=0, write_bytes=0)
    p.rlimit.return_value = (1024, 524288)
    return p


_HOST = {
    1: _fake_process(1, "systemd", cpu_seconds=300.0, age=86400.0),
    2: _fake_process(2, "kthreadd", cpu_seconds=1.0, age=86400.0),
    3: _fake_process(3, "rcu_gp", cpu_seconds=0.0, age=86400.0),
}


@pytest.mark.parametrize(
    "fire_on_call, batch_job_seen_from_scan",
    [
        # record started while the batch job was already burning CPU: the
        # trigger fires on the first tick, before any process scan.
        (1, 1),
        # the batch job started after the regular process scan of tick 1.
        (2, 2),
    ],
)
def test_spike_names_the_batch_job_that_started_just_before_it(
    tmp_path: Path, fire_on_call: int, batch_job_seen_from_scan: int
):
    batch = _fake_process(4242, "batch-job", cpu_seconds=3.9, age=4.0)
    scans = {"n": 0}

    def pids() -> list[int]:
        scans["n"] += 1
        if scans["n"] < batch_job_seen_from_scan:
            return list(_HOST)
        return [*_HOST, 4242]

    procs = {**_HOST, 4242: batch}
    store = Storage(tmp_path / "spike.db")
    clock = FakeClock(0.0)
    rec = Recorder(
        _make_config(),
        storage=store,
        samplers={
            "system": FakeSampler(),
            "process": ProcessSampler(resolve_proc=store.proc_id_for),
        },
        trigger=FakeTrigger(fire_on_call=fire_on_call),
        selfmeter=FakeSelfmeter(),
        duration=0.25,
        clock=clock,
        sleep=_AdvancingSleep(clock),
    )
    with (
        patch("psutil.pids", side_effect=pids),
        patch("psutil.Process", side_effect=lambda pid: procs[pid]),
    ):
        assert rec.run() == 0

    (raw,) = store.conn.execute("SELECT top_processes FROM spike_events").fetchone()
    top = spikes.culprits(raw, store)
    store.close()
    assert top[0]["name"] == "batch-job"
    assert top[0]["cpu_pct"] == pytest.approx(97.5, abs=0.5)


def test_process_samples_are_recorded_when_the_boot_time_cannot_be_read(tmp_path):
    """psutil raises RuntimeError without a btime line in /proc/stat (some containers)."""
    store = Storage(tmp_path / "noboot.db")
    clock = FakeClock(0.0)
    rec = Recorder(
        _make_config(),
        storage=store,
        samplers={
            "system": FakeSampler(),
            "process": ProcessSampler(resolve_proc=store.proc_id_for),
        },
        trigger=FakeTrigger(fire_on_call=10**9),
        selfmeter=FakeSelfmeter(),
        duration=1.0,
        clock=clock,
        sleep=_AdvancingSleep(clock),
    )
    with (
        patch("psutil.pids", return_value=list(_HOST)),
        patch("psutil.Process", side_effect=lambda pid: _HOST[pid]),
        patch(
            "psutil.boot_time",
            side_effect=RuntimeError("line 'btime' not found in /proc/stat"),
        ),
    ):
        assert rec.run() == 0

    scans = store.conn.execute("SELECT COUNT(*) FROM proc_samples").fetchone()[0]
    failures = store.conn.execute(
        "SELECT detail FROM ops_events WHERE kind = 'sampler_failed'"
    ).fetchall()
    store.close()
    assert scans >= len(_HOST)
    assert failures == []


# ---------------------------------------------------------------------------
# Self-throttle of the process scan (B58, B86)
# ---------------------------------------------------------------------------


class _TimedProcessSampler(FakeProcessSampler):
    """Remembers the (fake) monotonic time of every process scan."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self._clock = clock
        self.times: list[float] = []

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        self.times.append(self._clock())
        return super().collect(ctx)


def _scan_intervals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    own_cpu: Callable[[float], float],
    seconds: float,
    process_interval: float = 10.0,
) -> list[float]:
    """Seconds between process scans with procdiag's own CPU at *own_cpu(t)*.

    *t* counts from the start; the budget is the default 1 %.
    """
    clock = FakeClock(10_000.0)
    monkeypatch.setattr(
        SelfMeter, "sample", lambda self: (own_cpu(clock() - 10_000.0), 50 * 1024**2)
    )
    proc = _TimedProcessSampler(clock)
    rec = Recorder(
        ProcdiagConfig(sampling=SamplingConfig(process_interval=process_interval)),
        db_path=tmp_path / "throttle.db",
        samplers={"system": FakeSampler(), "process": proc},
        duration=seconds,
        clock=clock,
        sleep=_AdvancingSleep(clock),
    )
    assert rec.run() == 0
    return [round(b - a, 3) for a, b in zip(proc.times, proc.times[1:])]


def _steps(intervals: list[float]) -> list[float]:
    """The interval values in the order they were used, repeats collapsed."""
    return [v for i, v in enumerate(intervals) if i == 0 or v != intervals[i - 1]]


def test_process_scan_keeps_slowing_down_while_procdiag_stays_over_budget(
    tmp_path, monkeypatch
):
    """docs: x1.5 per step while over budget, up to 60 s."""
    intervals = _scan_intervals(
        tmp_path, monkeypatch, own_cpu=lambda t: 5.0, seconds=900
    )
    assert _steps(intervals) == pytest.approx([10.0, 15.0, 22.5, 33.75, 50.625, 60.0])
    assert intervals[-3:] == [60.0, 60.0, 60.0]


def test_process_scan_speeds_up_one_step_per_ten_minutes_under_budget(
    tmp_path, monkeypatch
):
    intervals = _scan_intervals(
        tmp_path,
        monkeypatch,
        own_cpu=lambda t: 5.0 if t < 600 else 0.1,
        seconds=2000,
    )
    assert _steps(intervals)[5:] == pytest.approx([60.0, 40.0, 26.667], abs=0.01)


@pytest.mark.parametrize("own_cpu", [0.2, 5.0])
def test_process_interval_longer_than_the_throttle_ceiling_is_kept(
    tmp_path, monkeypatch, own_cpu
):
    """throttle_max_interval caps the slowdown, not the configured interval."""
    intervals = _scan_intervals(
        tmp_path,
        monkeypatch,
        own_cpu=lambda t: own_cpu,
        seconds=1800,
        process_interval=120.0,
    )
    assert set(intervals) == {120.0}


# ---------------------------------------------------------------------------
# Library use: threads and repeated runs (B85, B91)
# ---------------------------------------------------------------------------


def test_run_from_another_thread_than_the_one_that_built_the_recorder(tmp_path):
    clock = FakeClock(0.0)
    rec = Recorder(
        _make_config(),
        db_path=tmp_path / "worker.db",
        samplers=_make_samplers(),
        duration=1.0,
        clock=clock,
        sleep=_AdvancingSleep(clock),
    )
    result: dict[str, int] = {}
    worker = threading.Thread(target=lambda: result.update(rc=rec.run()))
    worker.start()
    worker.join(30)

    assert result == {"rc": 0}
    assert _count(tmp_path / "worker.db", "sys_samples") >= 5


@pytest.mark.parametrize("own_storage", [True, False])
def test_a_second_run_raises_instead_of_recording_nothing(tmp_path, own_storage):
    db = tmp_path / "twice.db"
    storage = None if own_storage else Storage(db)
    clock = FakeClock(0.0)
    rec = Recorder(
        _make_config(),
        db_path=db,
        storage=storage,
        samplers=_make_samplers(),
        duration=1.0,
        clock=clock,
        sleep=_AdvancingSleep(clock),
    )
    try:
        assert rec.run() == 0
        with pytest.raises(RuntimeError):
            rec.run()
    finally:
        if storage is not None:
            storage.close()


# ---------------------------------------------------------------------------
# Watch-dir walks (B88)
# ---------------------------------------------------------------------------


class _WalkThatWaitsForSystemSamples(FakeDiskSampler):
    """A slow walk: it ends once the system sampler has run twice during it."""

    def __init__(self, system: FakeSampler) -> None:
        super().__init__()
        self._system = system
        self.system_sampled_during_walk = False

    def collect_dirs(self, ctx: RunContext) -> list[SampleBatch]:
        seen = self._system.run_count
        deadline = time.monotonic() + 3.0
        while self._system.run_count < seen + 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.system_sampled_during_walk = self._system.run_count >= seen + 2
        return super().collect_dirs(ctx)


def test_system_sampling_goes_on_while_a_directory_is_walked(tmp_path):
    system = FakeSampler()
    disk = _WalkThatWaitsForSystemSamples(system)
    rec = Recorder(
        _make_config(system_interval=0.05, dir_scan_interval=3600.0),
        db_path=tmp_path / "walk.db",
        samplers={"system": system, "disk": disk},
        selfmeter=FakeSelfmeter(),
        duration=1.0,
    )
    assert rec.run() == 0

    assert disk.system_sampled_during_walk
    assert _count(tmp_path / "walk.db", "dir_samples") == 1


def test_stop_waits_for_a_directory_walk_no_longer_than_its_time_budget(tmp_path):
    """A walk stuck past its budget (a hung mount) does not hold the stop."""
    release = threading.Event()

    class _StuckWalk(FakeDiskSampler):
        def collect_dirs(self, ctx: RunContext) -> list[SampleBatch]:
            release.wait(5.0)
            return super().collect_dirs(ctx)

    rec = Recorder(
        ProcdiagConfig(
            sampling=SamplingConfig(
                system_interval=0.05,
                watch_dirs=[WatchDir(path="/var/log", time_budget_sec=0.5)],
            )
        ),
        db_path=tmp_path / "stuck.db",
        samplers={"system": FakeSampler(), "disk": _StuckWalk()},
        selfmeter=FakeSelfmeter(),
        duration=0.3,
    )
    started = time.monotonic()
    try:
        assert rec.run() == 0
        took = time.monotonic() - started
    finally:
        release.set()

    assert took < 3.0
    assert _count(tmp_path / "stuck.db", "sys_samples") >= 3


@pytest.mark.parametrize("own_storage", [True, False])
def test_stop_keeps_a_directory_walk_that_ends_within_its_time_budget(
    tmp_path, own_storage
):
    """With a storage the caller passed in nothing flushes it after run()."""

    class _SlowWalk(FakeDiskSampler):
        def collect_dirs(self, ctx: RunContext) -> list[SampleBatch]:
            time.sleep(0.5)
            return super().collect_dirs(ctx)

    db = tmp_path / "slow.db"
    storage = None if own_storage else Storage(db)
    rec = Recorder(
        ProcdiagConfig(
            sampling=SamplingConfig(
                system_interval=0.05,
                watch_dirs=[WatchDir(path="/var/log", time_budget_sec=2.0)],
            )
        ),
        db_path=db,
        storage=storage,
        samplers={"system": FakeSampler(), "disk": _SlowWalk()},
        selfmeter=FakeSelfmeter(),
        duration=0.1,
    )
    try:
        assert rec.run() == 0
        assert len(_peek(db, "SELECT 1 FROM dir_samples")) == 1
    finally:
        if storage is not None:
            storage.close()


def test_stop_writes_what_is_recorded_before_it_waits_for_a_directory_walk(tmp_path):
    """A stuck mount must not keep the samples back until the stop is killed."""
    db = tmp_path / "flush-first.db"
    seen: dict[str, int] = {}

    class _WalkThatWaitsForTheStopFlush(FakeDiskSampler):
        def collect_dirs(self, ctx: RunContext) -> list[SampleBatch]:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                seen["sys_samples"] = len(_peek(db, "SELECT 1 FROM sys_samples"))
                if seen["sys_samples"] >= 3:
                    break
                time.sleep(0.01)
            return super().collect_dirs(ctx)

    rec = Recorder(
        ProcdiagConfig(
            sampling=SamplingConfig(
                system_interval=0.05,
                watch_dirs=[WatchDir(path="/var/log", time_budget_sec=10.0)],
            )
        ),
        db_path=db,
        samplers={"system": FakeSampler(), "disk": _WalkThatWaitsForTheStopFlush()},
        selfmeter=FakeSelfmeter(),
        duration=0.3,
    )
    assert rec.run() == 0

    assert seen["sys_samples"] >= 3
    assert _count(db, "dir_samples") == 1


def test_a_walks_notes_are_kept_once_the_walk_is_taken_not_while_it_runs(tmp_path):
    """The walk works on its own copy of the context, not the scheduler's."""
    db = tmp_path / "notes.db"
    walk_noted = threading.Event()
    release = threading.Event()
    kept_mid_walk: list[str] = []

    class _NotingWalk(FakeDiskSampler):
        def collect_dirs(self, ctx: RunContext) -> list[SampleBatch]:
            ctx.notes.append(Note("dir", "dir: /var/log を読めません"))
            walk_noted.set()
            release.wait(5.0)
            return super().collect_dirs(ctx)

    class _NotingSystem(FakeSampler):
        ticks_after_note = 0

        def collect(self, ctx: RunContext) -> list[SampleBatch]:
            if walk_noted.is_set():
                self.ticks_after_note += 1
                if self.ticks_after_note == 1:
                    ctx.notes.append(Note("system", "system: 一時的な失敗"))
                elif self.ticks_after_note == 2:
                    kept_mid_walk.extend(
                        d
                        for (d,) in _peek(
                            db,
                            "SELECT detail FROM ops_events WHERE kind='sampler_failed'",
                        )
                    )
                    release.set()
            return super().collect(ctx)

    rec = Recorder(
        _make_config(system_interval=0.05, dir_scan_interval=3600.0),
        db_path=db,
        samplers={"system": _NotingSystem(), "disk": _NotingWalk()},
        selfmeter=FakeSelfmeter(),
        duration=1.0,
    )
    try:
        assert rec.run() == 0
    finally:
        release.set()

    assert kept_mid_walk == ["system: 一時的な失敗"]
    kept = [
        d
        for (d,) in _peek(
            db, "SELECT detail FROM ops_events WHERE kind='sampler_failed'"
        )
    ]
    assert sorted(kept) == ["dir: /var/log を読めません", "system: 一時的な失敗"]
