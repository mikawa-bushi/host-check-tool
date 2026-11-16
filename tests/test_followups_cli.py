"""Small command-line faults left after the exit-code and config work.

A mistyped name is a usage error (4), not "problems found" (1).  A closed
stdout is said in one line instead of a traceback.  A comparison names the DB
that is unusable.  A ``throttle_factor`` of inf or NaN, which procdiag 0.6
accepted, is used as the minimum like any other out-of-range value instead of
ending ``record`` with an exit code the unit does not restart on.  ``doctor``
says what a degraded setting costs, and ``record`` / ``quick`` warn on stderr.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from synth import add_sys_baseline, make_db

from procdiag import doctor as doctor_mod
from procdiag.cli import cli
from procdiag.config import BudgetConfig, load_config
from procdiag.doctor import DoctorCheck


def _db(directory: Path, *, hours: float = 2.0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    store = make_db(directory, hours=hours)
    add_sys_baseline(store, hours=hours, start_ts=time.time() - hours * 3600)
    store.close()
    return store.path


def _closed_stdout(*args: object) -> subprocess.CompletedProcess:
    """``procdiag <args> >&-``: the command runs with file descriptor 1 closed."""
    command = [sys.executable, "-m", "procdiag.cli", *map(str, args)]
    return subprocess.run(
        ["sh", "-c", 'exec "$@" >&-', "sh", *command],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


class TestExplain:
    def test_an_unknown_name_is_a_usage_error(self):
        result = CliRunner().invoke(cli, ["explain", "bogus"])

        assert result.exit_code == 4
        assert "bogus" in result.stderr
        assert result.stdout == ""

    def test_a_rule_and_a_sampler_still_explain(self):
        assert CliRunner().invoke(cli, ["explain", "MEM-LEAK-01"]).exit_code == 0
        assert CliRunner().invoke(cli, ["explain", "system"]).exit_code == 0


class TestAClosedStdout:
    def test_export_says_so_in_one_line(self, tmp_path):
        db = _db(tmp_path)

        done = _closed_stdout("export", "--db", db)

        assert done.returncode == 4, done.stderr
        assert "Traceback" not in done.stderr
        assert done.stderr.startswith("エラー: ")
        assert "--out" in done.stderr
        assert len(done.stderr.strip().splitlines()) <= 2

    def test_export_to_a_file_does_not_need_stdout(self, tmp_path):
        db = _db(tmp_path)
        out = tmp_path / "out.csv"

        done = _closed_stdout("export", "--db", db, "--out", out, "--quiet")

        assert done.returncode == 0, done.stderr
        assert out.read_text(encoding="utf-8").startswith("ts_min,")

    def test_check_keeps_its_verdict_as_the_exit_code(self, tmp_path):
        db = _db(tmp_path)

        done = _closed_stdout("check", "--db", db)

        assert "Traceback" not in done.stderr, done.stderr
        assert done.returncode in (0, 1, 2, 3), done.stderr

    def test_snapshot_does_not_crash_on_the_colour_check(self):
        done = _closed_stdout("snapshot")

        assert "Traceback" not in done.stderr, done.stderr


class TestCompareNamesTheDbThatFails:
    @pytest.fixture
    def dbs(self, tmp_path):
        good = _db(tmp_path / "good", hours=30)
        bad = tmp_path / "bad.db"
        bad.write_text("this is not a database\n", encoding="utf-8")
        return good, bad

    @pytest.mark.parametrize("bad_is", ["before", "after"])
    def test_the_error_names_that_db_and_not_the_other(self, dbs, bad_is):
        good, bad = dbs
        args = [bad, good] if bad_is == "before" else [good, bad]

        result = CliRunner().invoke(cli, ["compare", *map(str, args)])

        assert result.exit_code == 4
        assert str(bad) in result.stderr
        assert str(good) not in result.stderr

    def test_a_single_db_is_named_as_before(self, dbs):
        _, bad = dbs

        result = CliRunner().invoke(
            cli,
            ["compare", "--db", str(bad), "--at", "2026-01-01 00:00"],
        )

        assert result.exit_code == 4
        assert str(bad) in result.stderr


@pytest.mark.parametrize("written", [float("inf"), float("nan")])
class TestAFactorThatIsNotANumber:
    def test_it_is_used_as_the_minimum(self, written):
        budget = BudgetConfig(throttle_factor=written)

        assert budget.throttle_factor == pytest.approx(1.1)

    def test_the_notice_says_what_was_written_and_what_is_used(self, written):
        notice = BudgetConfig(throttle_factor=written).throttle_notice()

        assert notice is not None
        assert "throttle_factor" in notice
        assert str(written) in notice
        assert "1.1" in notice
        assert "1.1 未満" not in notice

    def test_a_config_file_that_writes_it_loads(self, tmp_path, written):
        config = tmp_path / "config.py"
        config.write_text(
            "from procdiag.config import ProcdiagConfig, BudgetConfig\n"
            "config = ProcdiagConfig(budget=BudgetConfig("
            f"throttle_factor=float('{written}')))\n",
            encoding="utf-8",
        )

        assert load_config(config).budget.throttle_factor == pytest.approx(1.1)

    def test_record_runs_instead_of_exiting_4(self, tmp_path, written):
        config = tmp_path / "config.py"
        config.write_text(
            "from procdiag.config import ProcdiagConfig, BudgetConfig\n"
            "config = ProcdiagConfig(budget=BudgetConfig("
            f"throttle_factor=float('{written}')))\n",
            encoding="utf-8",
        )

        result = CliRunner().invoke(
            cli,
            [
                "record",
                "--duration",
                "2s",
                "--config",
                str(config),
                "--db",
                str(tmp_path / "r.db"),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "throttle_factor" in result.stderr
        assert str(written) in result.stderr


def test_a_factor_just_below_the_minimum_is_written_as_it_is_in_the_notice():
    notice = BudgetConfig(throttle_factor=1.0999999).throttle_notice()

    assert notice is not None
    assert "1.0999999" in notice


def test_a_factor_of_one_is_still_written_short():
    notice = BudgetConfig(throttle_factor=1.0).throttle_notice()

    assert notice is not None
    assert "throttle_factor に 1 が" in notice


class TestTheWarningIsOnStderr:
    @staticmethod
    def _config(folder: Path) -> Path:
        path = folder / "config.py"
        path.write_text(
            "from procdiag.config import ProcdiagConfig, BudgetConfig\n"
            "config = ProcdiagConfig(budget=BudgetConfig(throttle_factor=1.05))\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _warned_on_stderr_only(result) -> None:
        warnings = [l for l in result.stderr.splitlines() if l.startswith("警告: ")]
        assert [w for w in warnings if "throttle_factor" in w], result.stderr
        assert not [l for l in result.stdout.splitlines() if l.startswith("警告: ")]

    def test_record(self, tmp_path):
        result = CliRunner().invoke(
            cli,
            [
                "record",
                "--duration",
                "2s",
                "--config",
                str(self._config(tmp_path)),
                "--db",
                str(tmp_path / "r.db"),
            ],
        )

        assert result.exit_code == 0, result.output
        self._warned_on_stderr_only(result)

    def test_quick(self, tmp_path):
        result = CliRunner().invoke(
            cli,
            [
                "quick",
                "--duration",
                "2s",
                "--config",
                str(self._config(tmp_path)),
                "--out",
                str(tmp_path / "out"),
            ],
        )

        assert result.exit_code in (0, 1, 2, 3), result.output
        self._warned_on_stderr_only(result)


class TestDoctorFooter:
    _NAMES = (
        "psi",
        "cgroup_v2",
        "root",
        "io_counters",
        "smaps",
        "journal",
        "sensors",
        "db_disk",
        "config_security",
        "budget",
    )

    def _run(self, monkeypatch, tmp_path, degraded):
        checks = [
            DoctorCheck(n, n, "degraded" if n in degraded else "ok", f"{n} detail")
            for n in self._NAMES
        ]
        monkeypatch.setattr(doctor_mod, "run_doctor", lambda *a, **k: checks)
        result = CliRunner().invoke(cli, ["doctor", "--db", str(tmp_path / "x.db")])
        assert result.exit_code == 2, result.output
        return result.stdout.strip().splitlines()[-1]

    def test_a_degraded_capability_says_the_data_will_be_missing(
        self, monkeypatch, tmp_path
    ):
        footer = self._run(monkeypatch, tmp_path, {"psi"})

        assert "欠損" in footer

    def test_a_degraded_setting_does_not_say_data_will_be_missing(
        self, monkeypatch, tmp_path
    ):
        for setting in ("budget", "config_security"):
            footer = self._run(monkeypatch, tmp_path, {setting})

            assert "欠損" not in footer, footer
            assert "1 件" in footer

    def test_a_setting_next_to_a_capability_keeps_the_capability_wording(
        self, monkeypatch, tmp_path
    ):
        footer = self._run(monkeypatch, tmp_path, {"budget", "journal"})

        assert "欠損" in footer
        assert "2 件" in footer
