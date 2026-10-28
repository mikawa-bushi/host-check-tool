"""Tests for ``procdiag bundle`` (procdiag/bundle.py)."""

from __future__ import annotations

import sqlite3
import tarfile
from contextlib import closing

from click.testing import CliRunner
from synth import add_sys_baseline, make_db

import procdiag.snapshot as snap
from procdiag.bundle import create_bundle
from procdiag.cli import cli
from procdiag.config import ProcdiagConfig


def _recorded_db(tmp_path):
    store = make_db(tmp_path, hours=1.0)
    add_sys_baseline(store, hours=1.0)
    store.proc_id_for("app", "h1", "app --password=hunter2", 1.0)
    store.conn.commit()
    store.close()
    return tmp_path / "synth.db"


def _members(path):
    with tarfile.open(path) as tar:
        return {m.name.split("/", 1)[1] for m in tar.getmembers() if "/" in m.name}


def test_bundle_with_db_redacts_command_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no ./config.py to pick up
    db = _recorded_db(tmp_path)
    out = tmp_path / "b.tar.gz"
    result = create_bundle(
        ProcdiagConfig(db_path=str(db)), out=out, snapshot_sample_sec=0.05
    )

    assert result.has_db and result.cmdline_redacted
    assert {
        "procdiag.db",
        "report.html",
        "report.json",
        "status.json",
        "snapshot.json",
        "doctor.json",
        "README.txt",
    } <= _members(out)

    with tarfile.open(out) as tar:
        tar.extractall(tmp_path / "x", filter="data")
    copy = tmp_path / "x" / "b" / "procdiag.db"
    with closing(sqlite3.connect(copy)) as conn:
        assert conn.execute("SELECT cmdline_sample FROM proc_registry").fetchall() == [
            (None,)
        ]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert b"hunter2" not in copy.read_bytes()
    # The original recording is untouched.
    with closing(sqlite3.connect(db)) as conn:
        row = conn.execute("SELECT cmdline_sample FROM proc_registry").fetchone()
    assert row[0] == "app --password=hunter2"


def test_keep_cmdline_and_bundle_without_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no ./config.py to pick up
    db = _recorded_db(tmp_path)
    kept = create_bundle(
        ProcdiagConfig(db_path=str(db)),
        out=tmp_path / "k.tar.gz",
        keep_cmdline=True,
        snapshot_sample_sec=0.05,
    )
    assert not kept.cmdline_redacted

    bare = create_bundle(
        ProcdiagConfig(db_path=str(tmp_path / "missing.db")),
        out=tmp_path / "n.tar.gz",
        snapshot_sample_sec=0.05,
    )
    assert not bare.has_db
    assert _members(bare.path) == {"snapshot.json", "doctor.json", "README.txt"}


def test_cli_bundle(tmp_path, monkeypatch):
    db = _recorded_db(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snap, "run_snapshot", lambda cfg, sample_sec=1.0: [])
    result = CliRunner().invoke(
        cli, ["bundle", "--db", str(db), "--out", str(tmp_path / "c.tar.gz")]
    )
    assert result.exit_code == 0, result.output
    assert "バンドルを作成しました" in result.output
    assert "コマンドラインを削除" in result.output
    assert (tmp_path / "c.tar.gz").exists()
