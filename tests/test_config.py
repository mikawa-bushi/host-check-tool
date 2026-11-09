"""Tests for procdiag.config — Pydantic models and load_config."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from procdiag.cli import cli
from procdiag.config import (
    CONFIG_TEMPLATE,
    BudgetConfig,
    ProcdiagConfig,
    RetentionConfig,
    RulesConfig,
    SamplingConfig,
    TriggerConfig,
    WatchDir,
    load_config,
)

# ---------------------------------------------------------------------------
# Default values pass validation
# ---------------------------------------------------------------------------


def test_default_config_is_valid():
    cfg = ProcdiagConfig()
    assert cfg.db_path == "./procdiag.db"
    assert cfg.sampling.system_interval == 1.0
    assert cfg.sampling.process_interval == 10.0
    assert cfg.trigger.psi_cpu_some10 == 20.0
    assert cfg.budget.overhead_budget_pct == 1.0
    assert cfg.retention.raw == "48h"
    assert cfg.retention.rollup == "90d"
    assert cfg.rules.min_observation_hours == 6.0
    assert cfg.report.lang == "ja"


def test_sampling_config_defaults():
    s = SamplingConfig()
    assert s.system_interval == 1.0
    assert s.cgroup_interval == 5.0
    assert s.process_interval == 10.0
    assert s.disk_interval == 60.0
    assert s.dir_scan_interval == 3600.0
    assert s.cmdline_normalize is True
    assert s.watch_dirs == []


def test_watch_dir_valid():
    wd = WatchDir(path="/var/log", time_budget_sec=10.0)
    assert wd.path == "/var/log"
    assert wd.time_budget_sec == 10.0


# ---------------------------------------------------------------------------
# Invalid values produce Japanese error messages
# ---------------------------------------------------------------------------


def test_negative_system_interval_raises():
    with pytest.raises(Exception, match="正"):
        SamplingConfig(system_interval=-1.0)


def test_zero_cgroup_interval_raises():
    with pytest.raises(Exception, match="正"):
        SamplingConfig(cgroup_interval=0.0)


def test_negative_overhead_budget_raises():
    with pytest.raises(Exception, match="正"):
        BudgetConfig(overhead_budget_pct=-0.5)


def test_negative_psi_threshold_raises():
    with pytest.raises(Exception, match="0以上"):
        TriggerConfig(psi_cpu_some10=-1.0)


def test_invalid_retention_raw_raises():
    with pytest.raises(Exception, match="保持期間"):
        RetentionConfig(raw="abc")


def test_invalid_retention_rollup_raises():
    with pytest.raises(Exception, match="保持期間"):
        RetentionConfig(rollup="xyz")


def test_negative_min_observation_hours_raises():
    with pytest.raises(Exception, match="0以上"):
        RulesConfig(min_observation_hours=-1.0)


def test_zero_min_observation_hours_allowed():
    # 0 = no minimum: instantaneous rules / tests may waive the global guard.
    assert RulesConfig(min_observation_hours=0.0).min_observation_hours == 0.0


def test_negative_watch_dir_budget_raises():
    with pytest.raises(Exception, match="正"):
        WatchDir(path="/tmp", time_budget_sec=-5.0)


def test_extra_fields_forbidden():
    with pytest.raises(Exception):
        ProcdiagConfig(unknown_field="oops")


# ---------------------------------------------------------------------------
# load_config — no file → defaults
# ---------------------------------------------------------------------------


def test_load_config_no_file_returns_defaults(tmp_path):
    """load_config with a missing optional file path returns ProcdiagConfig()."""
    cfg = load_config(None)
    assert isinstance(cfg, ProcdiagConfig)
    assert cfg.db_path == "./procdiag.db"


def test_load_config_missing_explicit_path_raises(tmp_path):
    with pytest.raises(ValueError, match="見つかりません"):
        load_config(tmp_path / "nonexistent.py")


# ---------------------------------------------------------------------------
# load_config — template file is valid
# ---------------------------------------------------------------------------


def test_load_config_reads_template(tmp_path):
    """Writing CONFIG_TEMPLATE to a file and loading it should succeed."""
    cfg_file = tmp_path / "config.py"
    cfg_file.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    cfg = load_config(cfg_file)
    assert isinstance(cfg, ProcdiagConfig)
    assert cfg.sampling.system_interval == 1.0


def test_load_config_custom_values(tmp_path):
    """A config that overrides a field should be reflected in the loaded object."""
    source = (
        "from procdiag.config import ProcdiagConfig, SamplingConfig\n"
        "config = ProcdiagConfig(sampling=SamplingConfig(system_interval=2.0))\n"
    )
    cfg_file = tmp_path / "custom.py"
    cfg_file.write_text(source, encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.sampling.system_interval == 2.0


def test_load_config_missing_config_variable_raises(tmp_path):
    cfg_file = tmp_path / "bad.py"
    cfg_file.write_text("x = 42\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'config' 変数"):
        load_config(cfg_file)


def test_load_config_wrong_type_raises(tmp_path):
    cfg_file = tmp_path / "wrong.py"
    cfg_file.write_text("config = {'db_path': './test.db'}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ProcdiagConfig"):
        load_config(cfg_file)


def test_config_template_is_string():
    assert isinstance(CONFIG_TEMPLATE, str)
    assert "ProcdiagConfig" in CONFIG_TEMPLATE
    assert "SamplingConfig" in CONFIG_TEMPLATE


@pytest.mark.parametrize(
    "source, message",
    [
        (None, "見つかりません"),
        ("x = 1\n", "'config' 変数が定義されていません"),
        ("config = 1\n", "ProcdiagConfig のインスタンスではありません"),
        ("config = (\n", "読み込みに失敗しました"),
    ],
)
@pytest.mark.parametrize(
    "command",
    [["check"], ["status"], ["snapshot"], ["quick", "--duration", "1s", "--out", "q"]],
    ids=["check", "status", "snapshot", "quick"],
)
def test_cli_reports_a_broken_config_file_and_exits_4(
    tmp_path, monkeypatch, command, source, message
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "broken_config.py"
    if source is not None:
        path.write_text(source, encoding="utf-8")
    result = CliRunner().invoke(cli, [*command, "--config", str(path)])
    assert result.exit_code == 4, result.output
    assert isinstance(result.exception, SystemExit)
    assert message in result.output
    assert not (tmp_path / "q").exists()
