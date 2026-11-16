"""Reading a recording leaves no ``-wal`` / ``-shm`` files beside the DB.

SQLite creates both when a WAL database is opened and a connection that can only
read cannot remove them again, so every ``status`` / ``check`` left a pair that
looks like a recording still running.  They stay while a recorder has the DB open,
and a ``-wal`` that holds data is never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from synth import add_sys_baseline, make_db

from procdiag.cli import cli
from procdiag.storage import Storage


@pytest.fixture()
def db(tmp_path) -> Path:
    store = make_db(tmp_path, hours=1)
    add_sys_baseline(store, hours=1)
    store.close()
    path = tmp_path / "synth.db"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["synth.db"]
    return path


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


@pytest.mark.parametrize(
    "command",
    [["status"], ["check"], ["check", "--format", "json"], ["export"]],
)
def test_a_read_command_leaves_only_the_db(db, command):
    result = CliRunner().invoke(cli, [*command, "--db", str(db)])

    assert result.exit_code in (0, 1, 2), result.output
    assert _names(db.parent) == ["synth.db"]


def test_bundle_leaves_only_the_db_and_the_bundle(db, tmp_path):
    out = tmp_path / "out" / "b.tar.gz"

    result = CliRunner().invoke(cli, ["bundle", "--db", str(db), "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert _names(db.parent) == ["out", "synth.db"]


def test_a_read_that_finds_nothing_leaves_only_the_db(db):
    result = CliRunner().invoke(
        cli,
        ["check", "--db", str(db), "--since", "2001-01-01", "--until", "2001-01-02"],
    )

    assert result.exit_code == 3, result.output
    assert _names(db.parent) == ["synth.db"]


def test_files_of_a_running_recorder_stay_and_it_keeps_writing(db):
    recorder = Storage(db)
    recorder.set_meta("probe", "1")
    recorder.conn.commit()

    result = CliRunner().invoke(cli, ["status", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "synth.db-wal" in _names(db.parent)
    recorder.set_meta("probe", "2")
    recorder.conn.commit()
    recorder.close()
    assert _names(db.parent) == ["synth.db"]


def test_a_wal_holding_data_is_left_alone(db, tmp_path):
    recorder = Storage(db)
    recorder.set_meta("probe", "late")
    recorder.conn.commit()
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    shutil.copy(db, crashed / "synth.db")
    shutil.copy(f"{db}-wal", crashed / "synth.db-wal")
    recorder.close()
    wal_size = (crashed / "synth.db-wal").stat().st_size
    assert wal_size > 0

    result = CliRunner().invoke(cli, ["status", "--db", str(crashed / "synth.db")])

    assert result.exit_code == 0, result.output
    assert (crashed / "synth.db-wal").stat().st_size == wal_size


def _rows(db: Path) -> int:
    with Storage(db, read_only=True) as reader:
        return reader.conn.execute(
            "SELECT COUNT(*) FROM ops_events WHERE kind = 'race'"
        ).fetchone()[0]


def test_a_recorder_that_starts_while_a_reader_is_open_keeps_its_files_and_rows(db):
    reader = Storage(db, read_only=True)
    assert _names(db.parent) == ["synth.db", "synth.db-shm", "synth.db-wal"]
    recorder = Storage(db)
    recorder.record_event("race", "before")

    reader.close()

    assert "synth.db-wal" in _names(db.parent)
    assert "synth.db-shm" in _names(db.parent)
    recorder.record_event("race", "after")
    assert _rows(db) == 2
    recorder.close()
    assert _rows(db) == 2
    assert _names(db.parent) == ["synth.db"]
