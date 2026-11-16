"""What the user sees at the door of the CLI: an interrupt, a bare ``procdiag``,
shell completion, a place that cannot be written, a DB that is busy.

None of these is a diagnosis, so none may end like one (exit 1 = problems found).
"""

from __future__ import annotations

import os
import sqlite3
import types
from pathlib import Path

import pytest
from click.shell_completion import BashComplete
from click.testing import CliRunner
from synth import add_sys_baseline, make_db

from procdiag.cli import cli


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def good_db(tmp_path):
    store = make_db(tmp_path, hours=1)
    add_sys_baseline(store, hours=1)
    store.close()
    return tmp_path / "synth.db"


def _interrupt(*_args, **_kwargs):
    raise KeyboardInterrupt


class TestInterrupt:
    @pytest.mark.parametrize(
        ("command", "target"),
        [
            (["snapshot"], "procdiag.snapshot.run_snapshot"),
            (["quick", "--duration", "1s"], "procdiag.snapshot.run_snapshot"),
            (["doctor"], "procdiag.doctor.run_doctor"),
            (["check"], "procdiag.cli._diagnose"),
        ],
    )
    def test_ctrl_c_exits_130_without_click_abort(
        self, runner, tmp_path, good_db, monkeypatch, command, target
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(target, _interrupt)
        args = [*command, "--db", str(good_db)] if command == ["check"] else command
        result = runner.invoke(cli, args)
        assert result.exit_code == 130, result.output
        assert "Aborted!" not in result.output
        assert "中断" in result.output


class TestBareInvocation:
    def test_bare_procdiag_prints_the_help_and_exits_0(self, runner):
        result = runner.invoke(cli, [])
        assert result.exit_code == 0, result.output
        assert "Usage:" in result.stdout
        assert "quick" in result.stdout and "check" in result.stdout


class TestShellCompletion:
    @staticmethod
    def _complete(words: list[str], incomplete: str) -> list[str]:
        completer = BashComplete(cli, {}, "procdiag", "_PROCDIAG_COMPLETE")
        return [c.value for c in completer.get_completions(words, incomplete)]

    @pytest.mark.parametrize("removed", ["report", "list-rules", "advise"])
    def test_completing_after_a_removed_command_does_not_crash(self, removed):
        assert self._complete([removed], "--") == []

    def test_options_of_a_current_command_are_still_offered(self):
        assert "--db" in self._complete(["check"], "--")

    def test_a_removed_command_is_not_offered_by_name(self):
        names = self._complete([], "")
        assert "check" in names and "report" not in names


class TestUnwritablePlace:
    @staticmethod
    def _cannot_write(monkeypatch, folder, *, read_only: bool) -> None:
        """Make *folder* refuse writes, as on a file system mounted read-only or not."""
        real_access, real_statvfs = os.access, os.statvfs

        def access(path, mode, **kwargs):
            if Path(path).is_relative_to(folder):
                return False
            return real_access(path, mode, **kwargs)

        def statvfs(path):
            flag = real_statvfs(path).f_flag
            if read_only and Path(path).is_relative_to(folder):
                flag |= os.ST_RDONLY
            return types.SimpleNamespace(f_flag=flag)

        monkeypatch.setattr(os, "access", access)
        monkeypatch.setattr(os, "statvfs", statvfs)

    def test_quick_out_on_a_read_only_file_system_says_so(
        self, runner, tmp_path, monkeypatch
    ):
        out = tmp_path / "usb"
        out.mkdir()
        self._cannot_write(monkeypatch, out, read_only=True)
        result = runner.invoke(cli, ["quick", "--duration", "1s", "--out", str(out)])
        assert result.exit_code == 4, result.output
        assert "読み取り専用" in result.output
        assert "Permission denied" not in result.output

    def test_check_output_on_a_read_only_file_system_says_so(
        self, runner, tmp_path, good_db, monkeypatch
    ):
        out = tmp_path / "usb" / "r.html"
        out.parent.mkdir()
        self._cannot_write(monkeypatch, out.parent, read_only=True)
        result = runner.invoke(
            cli, ["check", "--db", str(good_db), "--output", str(out)]
        )
        assert result.exit_code == 4, result.output
        assert "読み取り専用" in result.output

    def test_a_plain_permission_problem_is_still_a_permission_problem(
        self, runner, tmp_path, good_db, monkeypatch
    ):
        quick_out = tmp_path / "locked"
        quick_out.mkdir()
        report = tmp_path / "usb" / "r.html"
        report.parent.mkdir()
        self._cannot_write(monkeypatch, tmp_path / "locked", read_only=False)
        quick = runner.invoke(
            cli, ["quick", "--duration", "1s", "--out", str(quick_out)]
        )
        self._cannot_write(monkeypatch, report.parent, read_only=False)
        check = runner.invoke(
            cli, ["check", "--db", str(good_db), "--output", str(report)]
        )
        for result in (quick, check):
            assert result.exit_code == 4, result.output
            assert "読み取り専用" not in result.output
            assert "書き込めません" in result.output


class TestBusyDb:
    @pytest.fixture()
    def locked_db(self, good_db):
        conn = sqlite3.connect(good_db, isolation_level=None)
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("INSERT INTO meta (key, value) VALUES ('held', '1')")
        yield good_db
        conn.close()

    @pytest.mark.parametrize("command", ["status", "check", "prune"])
    def test_a_busy_db_is_called_busy_not_unusable(
        self, runner, tmp_path, locked_db, monkeypatch, command
    ):
        monkeypatch.chdir(tmp_path)
        connect = sqlite3.connect
        monkeypatch.setattr(
            sqlite3,
            "connect",
            lambda *a, **kw: connect(*a, **{"timeout": 0.05, **kw}),
        )
        result = runner.invoke(cli, [command, "--db", str(locked_db)])
        assert result.exit_code == 4, result.output
        assert "使用中" in result.output
        assert "DB を使えません" not in result.output
        assert str(locked_db) in result.output
