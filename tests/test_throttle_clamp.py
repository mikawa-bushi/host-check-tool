"""A ``budget.throttle_factor`` that is too small is used as the smallest usable one.

procdiag 0.6 accepted any factor above 0.  A refusal now would end ``record`` with
exit code 4, which the generated unit does not restart (``RestartPreventExitStatus=4``),
so a config that ran before an upgrade would stop the recording for good.  The
factor is raised to 1.1 instead, and the user is told where they read about their
recording: when ``record`` starts, in ``doctor``, and in ``check``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from procdiag.cli import cli
from procdiag.config import BudgetConfig, ProcdiagConfig
from procdiag.doctor import run_doctor


def _write_config(folder: Path, factor: float) -> Path:
    path = folder / "config.py"
    path.write_text(
        "from procdiag.config import ProcdiagConfig, BudgetConfig\n"
        f"config = ProcdiagConfig(budget=BudgetConfig(throttle_factor={factor}))\n",
        encoding="utf-8",
    )
    return path


@dataclass
class Recording:
    folder: Path
    config: Path
    db: Path
    recorded: object


def _record(folder: Path, factor: float) -> Recording:
    config = _write_config(folder, factor)
    db = folder / "r.db"
    result = CliRunner().invoke(
        cli,
        ["record", "--duration", "2s", "--config", str(config), "--db", str(db)],
    )
    return Recording(folder, config, db, result)


@pytest.fixture(scope="module")
def clamped(tmp_path_factory) -> Recording:
    return _record(tmp_path_factory.mktemp("clamped"), 1.05)


@pytest.fixture(scope="module")
def usable(tmp_path_factory) -> Recording:
    return _record(tmp_path_factory.mktemp("usable"), 1.5)


class TestRecord:
    def test_record_runs_instead_of_exiting_4(self, clamped):
        assert clamped.recorded.exit_code == 0, clamped.recorded.output
        assert clamped.db.exists()

    def test_record_says_which_value_it_uses(self, clamped):
        out = clamped.recorded.output
        assert "throttle_factor" in out and "1.05" in out and "1.1" in out
        assert out.startswith("警告: ")

    def test_a_usable_factor_prints_no_warning(self, usable):
        assert usable.recorded.exit_code == 0, usable.recorded.output
        assert "throttle_factor" not in usable.recorded.output

    def test_commands_that_only_read_stay_quiet(self, clamped):
        result = CliRunner().invoke(
            cli, ["status", "--config", str(clamped.config), "--db", str(clamped.db)]
        )
        assert result.exit_code == 0, result.output
        assert "警告" not in result.output

    def test_quick_says_it_too(self, tmp_path):
        config = _write_config(tmp_path, 1.05)
        result = CliRunner().invoke(
            cli,
            [
                "quick",
                "--duration",
                "2s",
                "--config",
                str(config),
                "--out",
                str(tmp_path / "out"),
            ],
        )
        assert result.exit_code in (0, 1, 2, 3), result.output
        assert "throttle_factor" in result.output and "1.05" in result.output


class TestCheck:
    def test_check_names_the_value_the_recording_used(self, clamped):
        result = CliRunner().invoke(
            cli, ["check", "--config", str(clamped.config), "--db", str(clamped.db)]
        )
        assert result.exit_code in (0, 1, 2, 3), result.output
        notes = [line for line in result.output.splitlines() if "備考" in line]
        assert any("throttle_factor" in n and "1.05" in n and "1.1" in n for n in notes)

    def test_a_usable_factor_adds_no_note(self, usable):
        result = CliRunner().invoke(
            cli, ["check", "--config", str(usable.config), "--db", str(usable.db)]
        )
        assert result.exit_code in (0, 1, 2, 3), result.output
        assert "throttle_factor" not in result.output


class TestDoctor:
    def test_doctor_degrades_on_a_clamped_factor(self):
        config = ProcdiagConfig(budget=BudgetConfig(throttle_factor=1.05))
        (check,) = [c for c in run_doctor(config) if c.name == "budget"]
        assert check.status == "degraded"
        assert "1.05" in check.detail and "1.1" in check.detail

    def test_doctor_is_ok_with_the_default(self):
        (check,) = [c for c in run_doctor(ProcdiagConfig()) if c.name == "budget"]
        assert check.status == "ok"
        assert "1.5" in check.detail
