"""A journalctl or systemctl that hangs does not stop the 1-second sampling.

The kernel-log and service samplers wait on another program, for as long as its
10-second timeout.  On the one scheduler thread that was a 10-second hole in
``sys_samples`` and 10 seconds in which a spike went unseen.  Their reads run
on a thread of their own, the way watch-dir walks do; the scheduler takes what a
read recorded, its notes and what it could not observe once it is done.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Optional

import pytest
from test_recorder import FakeSampler, FakeSelfmeter

from procdiag import recorder as recorder_mod
from procdiag.config import ProcdiagConfig, SamplingConfig
from procdiag.core import Capability
from procdiag.core.sampler import RunContext, SampleBatch, Sampler
from procdiag.recorder import Recorder
from procdiag.storage import make_row


class _TimedSystem(FakeSampler):
    """The 1-second sampler, remembering when each tick happened."""

    def __init__(self) -> None:
        super().__init__()
        self.times: list[float] = []

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        self.times.append(time.monotonic())
        return super().collect(ctx)

    def longest_gap(self) -> float:
        return max(b - a for a, b in zip(self.times, self.times[1:]))


class _KernelLog(Sampler):
    """Stands in for the kernel-log sampler whose journalctl hangs."""

    name = "kernel_log"
    tier = "T3"
    interval_key = "kernel_log_interval"
    capability = Capability.KERNEL_LOG

    def __init__(self, *, hang: float = 0.0, hangs: int = 1, fail: bool = False):
        self.hang, self.hangs, self.fail = hang, hangs, fail
        self.calls = 0
        self.running = 0
        self.most_running = 0
        self._lock = threading.Lock()

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        with self._lock:
            self.calls += 1
            call = self.calls
            self.running += 1
            self.most_running = max(self.most_running, self.running)
        try:
            if call <= self.hangs:
                time.sleep(self.hang)
            if self.fail:
                raise OSError("journalctl でカーネルログを読めませんでした")
            row = make_row(
                "kernel_events", ts=time.time(), kind="io_error", message="x"
            )
            return [SampleBatch("kernel_events", [row])]
        finally:
            with self._lock:
                self.running -= 1


def _config() -> ProcdiagConfig:
    return ProcdiagConfig(
        sampling=SamplingConfig(
            system_interval=0.05, kernel_log_interval=0.2, dir_scan_interval=3600.0
        )
    )


def _record(tmp_path: Path, kernel_log: _KernelLog, *, duration: float):
    system = _TimedSystem()
    db = tmp_path / "slow.db"
    rec = Recorder(
        _config(),
        db_path=db,
        samplers={"system": system, "kernel_log": kernel_log},
        selfmeter=FakeSelfmeter(),
        duration=duration,
    )
    started = time.monotonic()
    code = rec.run()
    return code, system, db, time.monotonic() - started


def _rows(db: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(db)) as con:
        return con.execute(sql).fetchall()


def test_a_read_that_hangs_leaves_no_hole_in_the_system_samples(tmp_path):
    kernel_log = _KernelLog(hang=1.2)

    code, system, _, _ = _record(tmp_path, kernel_log, duration=2.0)

    assert code == 0
    assert kernel_log.calls >= 2
    assert system.longest_gap() < 0.4


def test_what_a_slow_read_found_is_recorded_when_it_ends(tmp_path):
    _, _, db, _ = _record(tmp_path, _KernelLog(hang=0.6), duration=1.5)

    assert len(_rows(db, "SELECT 1 FROM kernel_events")) >= 2


def test_a_read_is_not_started_again_while_the_last_one_runs(tmp_path):
    kernel_log = _KernelLog(hang=0.8)

    _record(tmp_path, kernel_log, duration=1.5)

    assert kernel_log.most_running == 1


def test_a_failing_read_still_leaves_its_note_and_what_it_could_not_observe(tmp_path):
    _, _, db, _ = _record(tmp_path, _KernelLog(fail=True, hangs=0), duration=1.5)

    failed = _rows(db, "SELECT detail FROM ops_events WHERE kind = 'sampler_failed'")
    lacked = _rows(db, "SELECT detail FROM ops_events WHERE kind = 'unobserved'")
    assert len(failed) == 1 and "collect失敗" in failed[0][0]
    assert len(lacked) == 1 and lacked[0][0].startswith("kernel_log: ")
    assert _rows(db, "SELECT 1 FROM kernel_events") == []


def test_stop_does_not_wait_for_a_read_that_is_still_hanging(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_mod, "_STOP_READ_WAIT_MAX_SEC", 0.3, raising=False)
    kernel_log = _KernelLog(hang=6.0, hangs=10)

    code, _, db, took = _record(tmp_path, kernel_log, duration=0.5)

    assert code == 0
    assert took < 3.0
    assert kernel_log.calls == 1
    noted = _rows(db, "SELECT detail FROM ops_events WHERE kind = 'sampler_failed'")
    assert any("停止時" in d for (d,) in noted), noted


def test_stop_reads_once_more_when_nothing_is_running(tmp_path):
    kernel_log = _KernelLog()

    _record(tmp_path, kernel_log, duration=0.3)

    assert kernel_log.calls >= 2


@pytest.mark.parametrize("name", ["kernel_log", "service"])
def test_the_event_samplers_are_read_on_their_own_thread_and_once_more_at_stop(
    tmp_path, name
):
    threads: list[Optional[str]] = []

    class _Named(_KernelLog):
        def collect(self, ctx: RunContext) -> list[SampleBatch]:
            threads.append(threading.current_thread().name)
            return super().collect(ctx)

    sampler = _Named()
    sampler.name = name
    rec = Recorder(
        _config(),
        db_path=tmp_path / "t.db",
        samplers={"system": _TimedSystem(), name: sampler},
        selfmeter=FakeSelfmeter(),
        duration=0.3,
    )
    assert rec.run() == 0

    assert threads[0].startswith("procdiag-")
    assert threads[-1] == threading.main_thread().name
