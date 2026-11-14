"""Recording while the DB cannot be written (disk full, I/O error, read-only
file): memory stays bounded and the loss is recorded, samples keep their
program after the DB recovers, the session still ends cleanly, and
``procdiag prune`` can run between the recorder's writes."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner
from synth import add_sys_baseline, make_db

from procdiag.cli import cli
from procdiag.config import ProcdiagConfig
from procdiag.diagnosis.engine import Engine
from procdiag.recorder import Recorder
from procdiag.storage import Storage


def _sys_row(ts: float) -> tuple:
    return (
        (ts, 25.0, 40.0, 4 * 10**9, 2 * 10**9, 0, 0, 0, 1.5)
        + (None,) * 5
        + (
            0,
            0,
            0.5,
            50_000_000,
        )
    )


def _proc_row(ts: float, proc_id: int, pid: int) -> tuple:
    return (ts, proc_id, pid, 10.0, 200_000_000, None, 4, 50, None, None)


def _cg_row(ts: float, cg_id: int) -> tuple:
    return (ts, cg_id, 1000, 512_000_000, 0, 4096, 8192, 5)


@contextmanager
def _writes_fail(store: Storage):
    """Every write of *store* fails, as on a full disk or a read-only DB file."""
    store.conn.execute("PRAGMA query_only=ON")
    try:
        yield
    finally:
        store.conn.execute("PRAGMA query_only=OFF")


def test_samples_keep_their_program_after_a_failed_flush(tmp_path: Path):
    store = Storage(tmp_path / "rec.db")
    ts = time.time()
    alpha = store.proc_id_for("python3", "hash-alpha00", "python3 --role=alpha", ts)
    alpha_unit = store.cg_id_for("system.slice/alpha.service", ts)
    store.set_proc_fd_limits(alpha, 1024, 4096)
    store.buffer("proc_samples", [_proc_row(ts, alpha, pid=101)])
    store.buffer("cg_samples", [_cg_row(ts, alpha_unit)])
    with _writes_fail(store), pytest.raises(sqlite3.Error):
        store.flush()

    kilo = store.proc_id_for("python3", "hash-kilo000", "python3 --role=kilo", ts + 10)
    kilo_unit = store.cg_id_for("system.slice/kilo.service", ts + 10)
    store.buffer("proc_samples", [_proc_row(ts + 10, kilo, pid=102)])
    store.buffer("cg_samples", [_cg_row(ts + 10, kilo_unit)])
    store.flush()

    programs = store.conn.execute(
        "SELECT s.pid, r.cmdline_sample FROM proc_samples s "
        "LEFT JOIN proc_registry r USING (proc_id) ORDER BY s.pid"
    ).fetchall()
    assert [tuple(r) for r in programs] == [
        (101, "python3 --role=alpha"),
        (102, "python3 --role=kilo"),
    ]
    units = store.conn.execute(
        "SELECT r.unit_path FROM cg_samples s "
        "LEFT JOIN cgroup_registry r USING (cg_id) ORDER BY s.ts"
    ).fetchall()
    assert [r[0] for r in units] == [
        "system.slice/alpha.service",
        "system.slice/kilo.service",
    ]
    assert store.proc_fd_limits(alpha) == (1024, 4096)
    store.close()


def test_unsaved_rows_are_capped_and_the_loss_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr("procdiag.storage._MAX_UNSAVED_ROWS", 10, raising=False)
    store = Storage(tmp_path / "full.db")
    t0 = time.time() - 600
    minutes = [[t0 + m * 60 + s for s in range(6)] for m in range(5)]
    with caplog.at_level(logging.WARNING), _writes_fail(store):
        for stamps in minutes:
            store.buffer("sys_samples", [_sys_row(ts) for ts in stamps])
            with pytest.raises(sqlite3.Error):
                store.flush()
    store.flush()

    saved = [r["ts"] for r in store.conn.execute("SELECT ts FROM sys_samples")]
    assert sorted(saved) == [ts for stamps in minutes for ts in stamps][-10:]
    (lost,) = store.conn.execute(
        "SELECT detail FROM ops_events WHERE kind = 'data_lost'"
    ).fetchall()
    assert "20 行" in lost["detail"]
    assert any(
        r.name == "procdiag.storage" and r.levelno >= logging.WARNING
        for r in caplog.records
    )
    store.close()


def test_check_notes_the_samples_dropped_while_the_db_could_not_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("procdiag.storage._MAX_UNSAVED_ROWS", 10, raising=False)
    store = make_db(tmp_path, hours=2)
    now = time.time()
    add_sys_baseline(store, hours=1, start_ts=now - 7200)
    with _writes_fail(store):
        store.buffer("sys_samples", [_sys_row(now - 3000 + s) for s in range(30)])
        with pytest.raises(sqlite3.Error):
            store.flush()
    store.flush()
    store.buffer("sys_samples", [_sys_row(time.time() + 1)])
    store.flush()

    diag = Engine(ProcdiagConfig()).run(store, only=["CPU-SATURATION-01"])

    assert any("20 行" in note for note in diag.quality.notes), diag.quality.notes
    store.close()


def test_prune_runs_while_the_recorder_waits_for_its_next_flush(tmp_path: Path):
    db = tmp_path / "rec.db"
    recorder_db = Storage(db)
    ts = time.time()
    for tick in range(2):
        proc = recorder_db.proc_id_for(
            "app", "hash-app0000", "app --serve", ts + 61 * tick
        )
        recorder_db.cg_id_for("system.slice/app.service", ts + 61 * tick)
    recorder_db.set_proc_fd_limits(proc, 1024, 4096)

    result = CliRunner().invoke(cli, ["prune", "--db", str(db)])

    assert result.exit_code == 0, result.output
    recorder_db.close()


def _record_while_every_flush_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, Path]:
    """Run a recorder for just over an hour (fake clock) on a DB whose flushes fail.

    The DB holds one sample older than the raw retention, so the hourly
    retention run is visible.
    """
    db = tmp_path / "rec.db"
    store = Storage(db)
    store.buffer("sys_samples", [_sys_row(time.time() - 3 * 86400)])
    store.flush()

    def disk_full(**_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(store, "flush", disk_full)
    now = [0.0]
    rec = Recorder(
        ProcdiagConfig(),
        storage=store,
        samplers={},
        duration=3700.0,
        clock=lambda: now[0],
        sleep=lambda secs: now.__setitem__(0, now[0] + secs),
    )
    rc = rec.run()
    monkeypatch.undo()
    store.close()
    return rc, db


def test_retention_keeps_running_while_flushes_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, db = _record_while_every_flush_fails(tmp_path, monkeypatch)

    with Storage(db, read_only=True) as store:
        assert store.counts()["sys_samples"] == 0


def test_stop_closes_the_session_and_fails_when_the_last_flush_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rc, db = _record_while_every_flush_fails(tmp_path, monkeypatch)

    assert rc != 0
    with Storage(db, read_only=True) as store:
        (session,) = store.sessions()
        assert session["stopped_ts"] is not None
        assert store.rows("ops_events", where="kind = 'stopped'")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
def test_record_refuses_a_db_it_cannot_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    db = tmp_path / "ro.db"
    Storage(db).close()
    db.chmod(0o444)

    result = CliRunner().invoke(cli, ["record", "--duration", "2s", "--db", str(db)])

    assert result.exit_code == 1
    assert any(
        r.levelno >= logging.ERROR and str(db) in r.getMessage() for r in caplog.records
    )
