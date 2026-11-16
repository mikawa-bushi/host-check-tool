"""How a recording starts, ends and copes with what it cannot write or finish.

A long ``system_interval`` does not stretch ``--duration``; a stuck watch-dir
walk neither holds the stop until the service manager kills it nor goes
unmentioned; an error that is not SQLite's in the last flush still closes the
session; a watch dir whose name is not UTF-8 does not stop the recording; the
count of CPUs the recorder keeps is read strictly.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_recorder import (
    FakeClock,
    FakeDiskSampler,
    FakeSampler,
    FakeSelfmeter,
    _AdvancingSleep,
    _make_config,
)

from procdiag import recorder, service
from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, SamplingConfig, WatchDir
from procdiag.core.sampler import RunContext, SampleBatch
from procdiag.diagnosis.cpu import _recorded_cpu_count
from procdiag.recorder import Recorder
from procdiag.samplers.disk import DiskSampler
from procdiag.storage import Storage


def _events(db: Path, kind: str) -> list[str]:
    with Storage(db, read_only=True) as store:
        return [
            r["detail"]
            for r in store.rows("ops_events", where="kind = ?", params=(kind,))
        ]


class TestDuration:
    def test_a_long_system_interval_does_not_stretch_the_duration(self, tmp_path):
        clock = FakeClock(0.0)
        slow = dict(
            system_interval=400.0,
            cgroup_interval=400.0,
            process_interval=400.0,
            disk_interval=400.0,
            dir_scan_interval=400.0,
        )
        rec = Recorder(
            _make_config(**slow),
            db_path=tmp_path / "long.db",
            samplers={"system": FakeSampler()},
            selfmeter=FakeSelfmeter(),
            duration=5.0,
            clock=clock,
            sleep=_AdvancingSleep(clock),
        )

        assert rec.run() == 0

        assert clock() < 20.0


class _StuckWalk(FakeDiskSampler):
    """A walk blocked in a hung mount until the test lets it go."""

    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self._release = release

    def collect_dirs(self, ctx: RunContext) -> list[SampleBatch]:
        self._release.wait(5.0)
        return super().collect_dirs(ctx)


def _stuck_walk_recorder(
    tmp_path: Path, release: threading.Event, **budget
) -> Recorder:
    return Recorder(
        ProcdiagConfig(
            sampling=SamplingConfig(
                system_interval=0.05,
                dir_scan_interval=3600.0,
                watch_dirs=[
                    WatchDir(path="/var/log", time_budget_sec=budget["seconds"])
                ],
            )
        ),
        db_path=tmp_path / "stuck.db",
        samplers={"system": FakeSampler(), "disk": _StuckWalk(release)},
        selfmeter=FakeSelfmeter(),
        duration=budget.get("duration", 0.3),
    )


class TestStuckDirWalk:
    def test_the_stop_wait_leaves_room_before_the_units_timeout(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(recorder, "_STOP_WAIT_MAX_SEC", 0.3)
        release = threading.Event()
        rec = _stuck_walk_recorder(tmp_path, release, seconds=100.0)
        started = time.monotonic()
        try:
            assert rec.run() == 0
            took = time.monotonic() - started
        finally:
            release.set()

        assert took < 2.0
        db = tmp_path / "stuck.db"
        assert _events(db, "stopped")
        with Storage(db, read_only=True) as store:
            (session,) = store.sessions()
        assert session["stopped_ts"] is not None
        assert any(
            "停止時" in n and "記録していません" in n
            for n in _events(db, "sampler_failed")
        )

    def test_the_cap_is_well_below_the_units_timeout(self):
        assert recorder._STOP_WAIT_MAX_SEC <= service.TIMEOUT_STOP_SEC / 3

    def test_a_walk_that_outlives_its_budget_is_noted_while_recording(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(recorder, "_WALK_STUCK_SLACK_SEC", 0.0, raising=False)
        release = threading.Event()
        rec = _stuck_walk_recorder(tmp_path, release, seconds=0.2, duration=1.0)
        try:
            assert rec.run() == 0
        finally:
            release.set()

        noted = _events(tmp_path / "stuck.db", "sampler_failed")
        assert any("時間予算" in n for n in noted), noted


class TestStopFlush:
    def test_an_error_that_is_not_sqlites_still_closes_the_session(self, tmp_path):
        db = tmp_path / "boom.db"
        store = Storage(db)
        real_flush = store.flush

        def flush(*, reason: str = "interval") -> int:
            raise ValueError("行を書けません")

        store.flush = flush  # type: ignore[method-assign]
        clock = FakeClock(0.0)
        rec = Recorder(
            _make_config(),
            storage=store,
            samplers={"system": FakeSampler()},
            selfmeter=FakeSelfmeter(),
            duration=0.5,
            clock=clock,
            sleep=_AdvancingSleep(clock),
        )

        rc = rec.run()

        assert rc == 1
        store.flush = real_flush  # type: ignore[method-assign]
        assert _events(db, "stopped") == ["正常終了"]
        store.close()


class TestStartRefusal:
    def test_the_start_message_is_not_printed_for_a_recording_that_cannot_start(
        self, tmp_path, monkeypatch
    ):
        def disk_full(self, entries):
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(Storage, "write_meta", disk_full)

        result = CliRunner().invoke(
            cli, ["record", "--duration", "1s", "--db", str(tmp_path / "full.db")]
        )

        assert result.exit_code == 1
        assert "記録を開始します" not in result.output

    def test_the_start_message_is_printed_once_for_a_recording_that_starts(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            Recorder, "_build_samplers", lambda self: {"system": FakeSampler()}
        )

        result = CliRunner().invoke(
            cli, ["record", "--duration", "1s", "--db", str(tmp_path / "ok.db")]
        )

        assert result.exit_code == 0, result.output
        assert result.output.count("記録を開始します") == 1


class TestConfigThatIsNotUtf8:
    def test_a_watch_dir_whose_name_is_not_utf8_does_not_stop_the_recording(
        self, tmp_path
    ):
        bad = "/data/" + b"\x91\x92gone".decode("utf-8", "surrogateescape")
        db = tmp_path / "nonutf8.db"
        clock = FakeClock(0.0)
        rec = Recorder(
            ProcdiagConfig(
                sampling=SamplingConfig(
                    system_interval=0.1,
                    dir_scan_interval=5.0,
                    watch_dirs=[WatchDir(path=bad)],
                )
            ),
            db_path=db,
            samplers={"system": FakeSampler(), "disk": DiskSampler()},
            selfmeter=FakeSelfmeter(),
            duration=1.0,
            clock=clock,
            sleep=_AdvancingSleep(clock),
        )

        assert rec.run() == 0

        with Storage(db, read_only=True) as store:
            kept = json.loads(store.meta()["config"])
        assert kept["sampling"]["watch_dirs"][0]["path"] == bad
        (note,) = _events(db, "sampler_failed")
        assert "/data/\\x91\\x92gone" in note


class TestRecordedCpuCount:
    @pytest.mark.parametrize(
        ("text", "count"),
        [("4", 4), (" 2 ", None), ("+2", None), ("٣", None), ("0", None), ("", None)],
    )
    def test_only_a_plain_count_is_taken(self, tmp_path, text, count):
        with Storage(tmp_path / "cpu.db") as store:
            store.set_meta("cpu_count", text)
            store.conn.commit()
            assert _recorded_cpu_count(store) == count


class TestWhatTheStopWritesStaysPinned:
    def test_a_new_identity_is_committed_before_its_id_is_returned(self, tmp_path):
        with Storage(tmp_path / "ids.db") as store:
            now = time.time()
            proc = store.proc_id_for("app", "hash-app0000", "app --serve", now)
            unit = store.cg_id_for("system.slice/app.service", now)
            store.conn.rollback()

            names = store.conn.execute(
                "SELECT name FROM proc_registry WHERE proc_id = ?", (proc,)
            ).fetchall()
            units = store.conn.execute(
                "SELECT unit_path FROM cgroup_registry WHERE cg_id = ?", (unit,)
            ).fetchall()
        assert [r[0] for r in names] == ["app"]
        assert [r[0] for r in units] == ["system.slice/app.service"]

    def test_close_releases_the_connection_when_the_last_flush_fails(self, tmp_path):
        store = Storage(tmp_path / "close.db")
        store.buffer("proc_counts", [(time.time(), 1, 0)])
        store.conn.execute("PRAGMA query_only=ON")

        with pytest.raises(sqlite3.Error):
            store.close()

        with pytest.raises(sqlite3.ProgrammingError):
            store.conn.execute("SELECT 1")

    def test_a_clock_set_back_is_committed_when_it_is_detected(self, tmp_path):
        db = tmp_path / "skew.db"
        with Storage(db) as store:
            store.set_meta("rollup_watermark", str(time.time() + 7200))
            store.conn.commit()
            store.rollup_incremental()
            store.conn.rollback()

            (skew,) = store.conn.execute(
                "SELECT COUNT(*) FROM ops_events WHERE kind = 'clock_skew'"
            ).fetchone()
            watermark = float(store.meta()["rollup_watermark"])
        assert skew == 1
        assert watermark <= time.time()

    def test_no_rollup_runs_for_rows_a_failed_flush_left_in_memory(
        self, tmp_path, monkeypatch
    ):
        db = tmp_path / "norollup.db"
        store = Storage(db)
        rolled = []
        monkeypatch.setattr(store, "rollup_incremental", lambda: rolled.append(1))
        fail = {"on": True}
        real_flush = store.flush

        def flush(*, reason: str = "interval") -> int:
            if fail["on"]:
                raise sqlite3.OperationalError("database or disk is full")
            return real_flush(reason=reason)

        monkeypatch.setattr(store, "flush", flush)
        rec = Recorder(
            _make_config(),
            storage=store,
            samplers={},
            selfmeter=FakeSelfmeter(),
            trigger=object(),
        )

        rec._job_flush()
        assert rolled == []
        fail["on"] = False
        rec._job_flush()
        assert rolled == [1]
        monkeypatch.undo()
        store.close()
