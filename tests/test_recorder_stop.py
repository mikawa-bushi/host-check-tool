"""The stop keeps what is recorded before it waits, and waits a bounded time in all.

The service and kernel-log samplers read once more at stop, and each read can
hang for the 10 seconds of its timeout.  systemd kills a recorder that has not
exited within ``TimeoutStopSec`` (30 s), so a stop that waited first and wrote
afterwards lost up to a minute of samples, and the ``stopped`` event, whenever a
journalctl and a systemctl hung together.  The rows are written first; the last
reads, and a watch-dir walk that still runs, share one capped wait.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
from test_recorder import FakeDiskSampler, FakeSampler, FakeSelfmeter

from procdiag import recorder as recorder_mod
from procdiag.config import ProcdiagConfig, SamplingConfig, WatchDir
from procdiag.core.sampler import RunContext, SampleBatch, Sampler
from procdiag.recorder import Recorder
from procdiag.storage import Storage, make_row

_CAP = 0.6


class _Reader(Sampler):
    """The service or kernel-log sampler, whose reads hang from call *hang_from* on."""

    tier = "T3"

    def __init__(self, name: str, *, hang: float, hang_from: int = 1) -> None:
        self.name = name
        self.interval_key = f"{name}_interval"
        self.hang, self.hang_from = hang, hang_from
        self.calls = 0
        self._lock = threading.Lock()

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        with self._lock:
            self.calls += 1
            call = self.calls
        if call >= self.hang_from:
            time.sleep(self.hang)
        row = make_row(
            "kernel_events",
            ts=time.time(),
            kind="io_error",
            message=f"{self.name} {call}",
        )
        return [SampleBatch("kernel_events", [row])]


class _StuckWalk(FakeDiskSampler):
    """A walk blocked in a hung mount until the test lets it go."""

    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self._release = release

    def collect_dirs(self, ctx: RunContext) -> list[SampleBatch]:
        self._release.wait(5.0)
        return super().collect_dirs(ctx)


@pytest.fixture(autouse=True)
def _real_waits(monkeypatch):
    monkeypatch.setattr(recorder_mod, "_EVENT_READ_WAIT_SEC", 0.1)
    monkeypatch.setattr(recorder_mod, "_STOP_WAIT_MAX_SEC", _CAP, raising=False)


def _config(**sampling) -> ProcdiagConfig:
    return ProcdiagConfig(
        sampling=SamplingConfig(
            system_interval=0.05,
            service_interval=0.2,
            kernel_log_interval=0.2,
            dir_scan_interval=3600.0,
            **sampling,
        )
    )


def _recorder(tmp_path: Path, readers: list[_Reader], duration: float) -> Recorder:
    return Recorder(
        _config(),
        db_path=tmp_path / "stop.db",
        samplers={"system": FakeSampler(), **{r.name: r for r in readers}},
        selfmeter=FakeSelfmeter(),
        duration=duration,
    )


def _rows(db: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(db)) as con:
        return con.execute(sql).fetchall()


def _stop_took(rec: Recorder, duration: float) -> float:
    started = time.monotonic()
    assert rec.run() == 0
    return time.monotonic() - started - duration


@pytest.mark.parametrize(
    ("hang_from", "duration"),
    [(1, 0.5), (2, 0.5), (2, 0.1)],
    ids=["reads in flight", "one read ended, the next is in flight", "idle"],
)
def test_a_journalctl_and_a_systemctl_that_hang_together_do_not_hold_the_stop(
    tmp_path, hang_from, duration
):
    readers = [
        _Reader("service", hang=2.0, hang_from=hang_from),
        _Reader("kernel_log", hang=2.0, hang_from=hang_from),
    ]

    took = _stop_took(_recorder(tmp_path, readers, duration), duration)

    assert took < 1.1, "the two waits are one: they do not add up"
    db = tmp_path / "stop.db"
    assert _rows(db, "SELECT 1 FROM ops_events WHERE kind = 'stopped'")
    assert _rows(db, "SELECT 1 FROM sys_samples")
    noted = _rows(db, "SELECT detail FROM ops_events WHERE kind = 'sampler_failed'")
    assert any("service: 停止時" in d for (d,) in noted), noted
    assert any("kernel_log: 停止時" in d for (d,) in noted), noted


def test_a_hung_read_does_not_cost_the_final_read_of_the_other(tmp_path):
    readers = [_Reader("service", hang=0.0), _Reader("kernel_log", hang=2.0)]

    took = _stop_took(_recorder(tmp_path, readers, 0.5), 0.5)

    assert took < 1.1
    db = tmp_path / "stop.db"
    kept = [m for (m,) in _rows(db, "SELECT message FROM kernel_events")]
    assert readers[0].calls >= 2
    assert f"service {readers[0].calls}" in kept
    assert not any(m.startswith("kernel_log") for m in kept)


def test_what_is_recorded_is_in_the_db_while_the_stop_waits_for_a_read(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(recorder_mod, "_STOP_WAIT_MAX_SEC", 1.5, raising=False)
    db = tmp_path / "stop.db"
    counts: list[tuple[float, int]] = []
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            try:
                if db.exists():
                    (n,) = _rows(db, "SELECT count(*) FROM sys_samples")[0]
                    counts.append((time.monotonic(), n))
            except sqlite3.Error:
                pass
            time.sleep(0.02)

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        rec = _recorder(tmp_path, [_Reader("kernel_log", hang=3.0)], 0.4)
        assert rec.run() == 0
        ended = time.monotonic()
    finally:
        done.set()
        watcher.join()

    (total,) = _rows(db, "SELECT count(*) FROM sys_samples")[0]
    waiting = [n for t, n in counts if t <= ended - 1.0]
    assert total > 3
    assert waiting and max(waiting) == total, (
        f"{max(waiting, default=0)} of {total} samples were in the DB a second "
        "before the stop ended: a SIGKILL during the wait would have lost the rest"
    )


def test_a_read_that_ends_inside_the_stop_wait_is_kept_and_read_once_more(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(recorder_mod, "_STOP_WAIT_MAX_SEC", 3.0, raising=False)
    reader = _Reader("kernel_log", hang=0.7)

    _stop_took(_recorder(tmp_path, [reader], 0.3), 0.3)

    db = tmp_path / "stop.db"
    kept = sorted(m for (m,) in _rows(db, "SELECT message FROM kernel_events"))
    assert kept == ["kernel_log 1", "kernel_log 2"]
    noted = _rows(db, "SELECT detail FROM ops_events WHERE kind = 'sampler_failed'")
    assert not any("停止時" in d for (d,) in noted), noted


def test_the_last_reads_are_written_by_the_stop_itself(tmp_path):
    db = tmp_path / "stop.db"
    store = Storage(db)
    reader = _Reader("kernel_log", hang=0.0)
    rec = Recorder(
        _config(),
        storage=store,
        samplers={"system": FakeSampler(), "kernel_log": reader},
        selfmeter=FakeSelfmeter(),
        duration=0.3,
    )
    try:
        assert rec.run() == 0
        kept = [m for (m,) in _rows(db, "SELECT message FROM kernel_events")]
    finally:
        store.close()

    assert f"kernel_log {reader.calls}" in kept


def test_a_stuck_walk_and_a_hung_read_share_the_stop_wait(tmp_path):
    release = threading.Event()
    config = _config(watch_dirs=[WatchDir(path="/var/log", time_budget_sec=100.0)])
    rec = Recorder(
        config,
        db_path=tmp_path / "stop.db",
        samplers={
            "system": FakeSampler(),
            "kernel_log": _Reader("kernel_log", hang=2.0),
            "disk": _StuckWalk(release),
        },
        selfmeter=FakeSelfmeter(),
        duration=0.3,
    )
    try:
        took = _stop_took(rec, 0.3)
    finally:
        release.set()

    assert took < 1.1
    db = tmp_path / "stop.db"
    assert _rows(db, "SELECT 1 FROM ops_events WHERE kind = 'stopped'")
