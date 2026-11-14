"""The first-run flow: ``init``, ``status`` and the ``--since`` / ``--until`` options."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner
from synth import add_sys_baseline, make_db

from procdiag.cli import cli
from procdiag.config import CONFIG_TEMPLATE, load_config

REPO = Path(__file__).resolve().parent.parent


def _printed_commands(output: str) -> list[list[str]]:
    return [
        shlex.split(line)
        for line in output.splitlines()
        if line.strip().startswith("procdiag ")
    ]


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", [".", "site", "my site/line pc"])
def test_init_next_steps_name_the_config_it_generated(tmp_path, monkeypatch, target):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["init", target])
    assert result.exit_code == 0, result.output

    commands = _printed_commands(result.output)

    assert [c[1] for c in commands][:2] == ["doctor", "record"]
    for command in commands:
        config = Path(command[command.index("--config") + 1])
        assert config.is_file()
        assert config.resolve() == (tmp_path / target / "config.py").resolve()


def test_init_gives_the_database_the_directory_it_creates(tmp_path):
    result = CliRunner().invoke(cli, ["init", str(tmp_path / "site")])
    assert result.exit_code == 0, result.output

    cfg = load_config(tmp_path / "site" / "config.py")

    assert Path(cfg.db_path) == (tmp_path / "site" / "data" / "procdiag.db").resolve()
    assert Path(cfg.db_path).parent.is_dir()


def test_readme_shows_the_database_path_init_writes():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    shown = re.search(r'db_path="([^"]+)"', readme)
    written = re.search(r'db_path="([^"]+)"', CONFIG_TEMPLATE)
    assert shown and written
    assert shown.group(1) == written.group(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reads_the_span_from_rollups_when_raw_samples_are_gone(tmp_path):
    store = make_db(tmp_path)
    add_sys_baseline(store, hours=2)
    store.conn.execute("DELETE FROM sys_samples")
    store.conn.commit()

    text = CliRunner().invoke(cli, ["status", "--db", str(store.path)])
    data = json.loads(
        CliRunner()
        .invoke(cli, ["status", "--db", str(store.path), "--format", "json"])
        .output
    )
    store.close()

    assert data["counts"]["sys_samples"] == 0
    assert data["has_data"] is True
    assert data["start"] < data["end"]
    assert "まだサンプルがありません" not in text.output
    assert "期間: —" not in text.output


def test_status_still_reports_an_empty_db_as_empty(tmp_path):
    store = make_db(tmp_path)
    result = CliRunner().invoke(cli, ["status", "--db", str(store.path)])
    store.close()
    assert "まだサンプルがありません" in result.output


def test_status_size_includes_the_wal_file(tmp_path):
    store = make_db(tmp_path)
    add_sys_baseline(store, hours=2)
    wal = Path(f"{store.path}-wal")
    assert wal.stat().st_size > 0

    result = CliRunner().invoke(
        cli, ["status", "--db", str(store.path), "--format", "json"]
    )
    size = store.path.stat().st_size + wal.stat().st_size
    store.close()

    assert json.loads(result.output)["counts"]["db_bytes"] == size


# ---------------------------------------------------------------------------
# --since / --until
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--since", ""],
        ["--until", ""],
        ["--since", "", "--until", ""],
        ["--since", " "],
    ],
)
def test_an_empty_period_bound_is_an_error_not_the_default_period(tmp_path, args):
    store = make_db(tmp_path)
    add_sys_baseline(store, hours=2)
    store.close()

    result = CliRunner().invoke(cli, ["check", "--db", str(store.path), *args])

    assert result.exit_code == 4, result.output
    assert "時刻の形式が不正です" in result.output
