"""``procdiag service print/install``: the unit must record unattended.

systemd starts the unit with its own working directory ($HOME or /) and splits
ExecStart on its own quoting rules, so every path in the unit has to be
absolute and quoted, and a configuration mistake must not restart the service
forever.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from procdiag.cli import cli

_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "s": " ",
    "\\": "\\",
    '"': '"',
    "'": "'",
}


def systemd_split(line: str) -> list[str]:
    """Words of an ExecStart= value as systemd.syntax(7) / systemd.service(5) read them.

    Whitespace separates words, single and double quotes group, a backslash
    starts a C escape, ``%%`` is a literal percent sign and ``$$`` a literal
    dollar sign.  A bare ``%x`` or ``$VAR`` would be expanded by systemd, so
    it is an error here.
    """
    words: list[str] = []
    cur: list[str] = []
    in_word = False
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            nxt = line[i + 1]
            if nxt == "x":
                cur.append(chr(int(line[i + 2 : i + 4], 16)))
                i += 4
            elif nxt in _ESCAPES:
                cur.append(_ESCAPES[nxt])
                i += 2
            else:
                raise AssertionError(f"unknown escape \\{nxt} in {line!r}")
            in_word = True
        elif quote:
            if ch == quote:
                quote = ""
            else:
                cur.append(ch)
            i += 1
        elif ch in "\"'":
            quote = ch
            in_word = True
            i += 1
        elif ch in " \t":
            if in_word:
                words.append("".join(cur))
                cur, in_word = [], False
            i += 1
        else:
            cur.append(ch)
            in_word = True
            i += 1
    assert not quote, f"unterminated quote in {line!r}"
    if in_word:
        words.append("".join(cur))
    return [_unescape_specifiers(w) for w in words]


def _unescape_specifiers(word: str) -> str:
    out = re.sub(r"%(.)", lambda m: _only(m, "%"), word)
    return re.sub(r"\$(.|$)", lambda m: _only(m, "$"), out)


def _only(match: re.Match, literal: str) -> str:
    assert match.group(1) == literal, f"systemd would expand {match.group(0)!r}"
    return literal


def _field(unit: str, key: str) -> list[str]:
    return [ln[len(key) + 1 :] for ln in unit.splitlines() if ln.startswith(key + "=")]


def exec_args(unit: str) -> list[str]:
    (line,) = _field(unit, "ExecStart")
    return systemd_split(line)


def _print(*args: str) -> str:
    result = CliRunner().invoke(cli, ["service", "print", *args])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture(autouse=True)
def _no_installed_procdiag(monkeypatch):
    monkeypatch.setattr("procdiag.service.shutil.which", lambda name: None)


def _value(args: list[str], option: str) -> str:
    return args[args.index(option) + 1]


def test_systemd_split_reads_quotes_and_escapes():
    assert systemd_split(r'/bin/x "a b" \x41 "50%%" "$$HOME" it\'s') == [
        "/bin/x",
        "a b",
        "A",
        "50%",
        "$HOME",
        "it's",
    ]


def test_relative_paths_become_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = exec_args(
        _print("--config", "site/config.py", "--db", "data/../data/procdiag.db")
    )
    assert _value(args, "--config") == str(tmp_path.resolve() / "site" / "config.py")
    assert _value(args, "--db") == str(tmp_path.resolve() / "data" / "procdiag.db")
    assert Path(args[0]).is_absolute()


@pytest.mark.parametrize(
    "name",
    [
        "line pc",
        "100%",
        "a$HOME",
        'quo"te',
        "it's",
        "back\\slash",
        "ライン PC",
        "tab\there",
        "new\nline",
    ],
)
def test_paths_survive_systemd_word_splitting(tmp_path, name):
    config = tmp_path / name / "config.py"
    db = tmp_path / name / "p.db"
    unit = _print("--config", str(config), "--db", str(db), "--duration", "1h")
    assert len(_field(unit, "ExecStart")) == 1
    args = exec_args(unit)
    assert _value(args, "--config") == str(config)
    assert _value(args, "--db") == str(db)
    assert _value(args, "--duration") == "1h"


def test_an_installed_procdiag_is_named_by_absolute_path(tmp_path, monkeypatch):
    exe = tmp_path / "bin dir" / "procdiag"
    monkeypatch.setattr("procdiag.service.shutil.which", lambda name: str(exe))
    args = exec_args(_print())
    assert args[:2] == [str(exe), "record"]


@pytest.mark.parametrize("duration", ["2", "abc", "1x", "", "-5s", "1h 30m"])
@pytest.mark.parametrize("verb", ["print", "install"])
def test_an_invalid_duration_is_rejected_before_anything_is_written(
    tmp_path, monkeypatch, verb, duration
):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = CliRunner().invoke(cli, ["service", verb, "--duration", duration])
    assert result.exit_code == 4, result.output
    assert "期間の形式が不正です" in result.output
    assert not (tmp_path / ".config").exists()


def test_a_system_unit_without_config_records_under_var_lib():
    unit = _print("--system")
    assert _value(exec_args(unit), "--db") == "/var/lib/procdiag/procdiag.db"
    assert _field(unit, "StateDirectory") == ["procdiag"]
    assert _field(unit, "WorkingDirectory") == ["/var/lib/procdiag"]


@pytest.mark.parametrize(
    "extra", [["--config", "/etc/procdiag/config.py"], ["--db", "/d/p.db"]]
)
def test_a_system_unit_with_a_database_choice_keeps_it(extra):
    unit = _print("--system", *extra)
    args = exec_args(unit)
    assert "/var/lib/procdiag" not in " ".join(args)
    assert not _field(unit, "StateDirectory")


def test_a_user_unit_without_config_leaves_the_database_to_the_default():
    unit = _print()
    assert "--db" not in exec_args(unit)
    assert not _field(unit, "StateDirectory")


def test_a_failure_restarts_but_a_precondition_error_does_not():
    unit = _print()
    assert _field(unit, "Restart") == ["on-failure"]
    assert _field(unit, "RestartPreventExitStatus") == ["4"]


def _run_exec_start(unit: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        exec_args(unit),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_the_unit_records_when_started_from_another_directory(tmp_path, monkeypatch):
    site = tmp_path / "line pc"
    site.mkdir()
    (site / "config.py").write_text(
        "from procdiag.config import ProcdiagConfig\n"
        'config = ProcdiagConfig(db_path="./data/procdiag.db")\n',
        encoding="utf-8",
    )
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("procdiag.service.shutil.which", lambda name: None)
    unit = _print("--config", "line pc/config.py", "--duration", "1s")
    monkeypatch.chdir(elsewhere)

    done = _run_exec_start(unit, elsewhere)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (site / "data" / "procdiag.db").is_file()
    assert list(elsewhere.iterdir()) == []


def test_a_config_mistake_exits_with_a_code_the_unit_does_not_restart_on(tmp_path):
    unit = _print("--config", str(tmp_path / "missing.py"))
    (prevented,) = _field(unit, "RestartPreventExitStatus")

    done = _run_exec_start(unit, tmp_path)

    assert str(done.returncode) in prevented.split(), done.stdout + done.stderr
    assert "設定ファイルが見つかりません" in done.stderr


def test_install_writes_the_same_absolute_quoted_unit(tmp_path, monkeypatch):
    work = tmp_path / "my site"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(
        cli, ["service", "install", "--config", "config.py", "--db", "p.db"]
    )
    assert result.exit_code == 0, result.output
    unit = (
        tmp_path / "home" / ".config" / "systemd" / "user" / "procdiag-record.service"
    ).read_text(encoding="utf-8")
    args = exec_args(unit)
    assert _value(args, "--config") == str(work.resolve() / "config.py")
    assert _value(args, "--db") == str(work.resolve() / "p.db")
    assert _field(unit, "RestartPreventExitStatus") == ["4"]


def test_install_hint_for_a_system_unit_keeps_the_options(tmp_path, monkeypatch):
    work = tmp_path / "my site"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(
        cli, ["service", "install", "--config", "config.py", "--duration", "1h"]
    )
    (hint,) = [
        ln for ln in result.output.splitlines() if "service print --system" in ln
    ]
    words = shlex.split(hint.split("|")[0])
    assert _value(words, "--config") == str(work.resolve() / "config.py")
    assert _value(words, "--duration") == "1h"
    assert "--system" in words
