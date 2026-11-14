"""Which config file runs, where its ``db_path`` points, and which values it may hold."""

from __future__ import annotations

import math
import os
import typing
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import BaseModel

from procdiag import config as config_module
from procdiag.cli import cli
from procdiag.config import (
    BudgetConfig,
    ProcdiagConfig,
    RetentionConfig,
    RulesConfig,
    SamplingConfig,
    TriggerConfig,
    WatchDir,
    load_config,
)
from procdiag.doctor import _check_config_security
from procdiag.storage import Storage

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root owns what it creates")

PLANTED = (
    "import pathlib\n"
    'pathlib.Path("PWNED").write_text("ran")\n'
    "from procdiag.config import ProcdiagConfig\n"
    "config = ProcdiagConfig()\n"
)


def _write_config(path: Path, db_path: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from procdiag.config import ProcdiagConfig\n"
        f"config = ProcdiagConfig(db_path={db_path!r})\n",
        encoding="utf-8",
    )
    return path


def _make_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Storage(path).close()
    return path


@pytest.fixture
def someone_else(monkeypatch):
    """Make the files this process creates look like another user's."""
    uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: uid + 4242)


# ---------------------------------------------------------------------------
# ./config.py is only run when nobody else could have written it
# ---------------------------------------------------------------------------


@NOT_ROOT
@pytest.mark.parametrize("command", [["status"], ["snapshot"], ["doctor"]])
def test_another_users_cwd_config_is_not_executed(
    tmp_path, monkeypatch, someone_else, command
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.py").write_text(PLANTED, encoding="utf-8")

    result = CliRunner().invoke(cli, command)

    assert result.exit_code == 4, result.output
    assert not (tmp_path / "PWNED").exists()
    assert "他のユーザー" in result.output
    assert "--config" in result.output


def test_the_users_own_cwd_config_is_still_the_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path / "config.py", str(_make_db(tmp_path / "mine.db")))

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert str(tmp_path / "mine.db") in result.output


@NOT_ROOT
def test_an_explicit_config_is_run_whoever_owns_it(tmp_path, monkeypatch, someone_else):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "other.py").write_text(PLANTED, encoding="utf-8")

    result = CliRunner().invoke(cli, ["status", "--config", "other.py"])

    assert (tmp_path / "PWNED").exists()
    assert "他のユーザー" not in result.output


def test_a_world_writable_cwd_config_is_not_executed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.py"
    config.write_text(PLANTED, encoding="utf-8")
    config.chmod(0o666)

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 4, result.output
    assert not (tmp_path / "PWNED").exists()
    assert "world-writable" in result.output


@pytest.mark.parametrize(
    ("mode", "refused"), [(0o777, True), (0o1777, False)], ids=["open", "sticky"]
)
def test_a_cwd_config_in_a_shared_directory_needs_the_sticky_bit(
    tmp_path, monkeypatch, mode, refused
):
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_config(shared / "config.py", str(_make_db(shared / "p.db")))
    shared.chmod(mode)
    monkeypatch.chdir(shared)

    result = CliRunner().invoke(cli, ["status"])

    if refused:
        assert result.exit_code == 4, result.output
        assert "sticky" in result.output
    else:
        assert result.exit_code == 0, result.output


@NOT_ROOT
def test_doctor_reports_what_the_loader_refuses(tmp_path, monkeypatch, someone_else):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.py").write_text(PLANTED, encoding="utf-8")

    check = _check_config_security(None)

    assert check.status == "degraded"
    assert "他のユーザー" in check.detail


def test_doctor_reports_a_config_in_a_shared_directory(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    config = _write_config(shared / "config.py", "p.db")
    config.chmod(0o600)
    shared.chmod(0o777)

    check = _check_config_security(str(config))

    assert check.status == "degraded"
    assert "sticky" in check.detail


def test_doctor_accepts_the_users_own_private_config(tmp_path):
    config = _write_config(tmp_path / "config.py", "p.db")
    config.chmod(0o600)
    assert _check_config_security(str(config)).status == "ok"


@pytest.mark.parametrize("option", ["--config", "--db"])
def test_an_empty_option_value_is_an_error_not_the_default(
    tmp_path, monkeypatch, option
):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["status", option, ""])
    assert result.exit_code == 4, result.output
    assert option in result.output


# ---------------------------------------------------------------------------
# db_path
# ---------------------------------------------------------------------------


def test_a_relative_db_path_is_relative_to_the_config_file(tmp_path, monkeypatch):
    site = tmp_path / "site"
    _write_config(site / "config.py", "./data/procdiag.db")
    _make_db(site / "data" / "procdiag.db")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    cfg = load_config(Path("../site/config.py"))
    status = CliRunner().invoke(cli, ["status", "--config", "../site/config.py"])

    assert cfg.db_path == str(site.resolve() / "data" / "procdiag.db")
    assert status.exit_code == 0, status.output
    assert list(elsewhere.iterdir()) == []


def test_without_a_config_file_the_default_db_is_in_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config(None).db_path == "./procdiag.db"


def test_a_tilde_db_path_is_the_home_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    _make_db(home / "procdiag" / "p.db")
    config = _write_config(tmp_path / "config.py", "~/procdiag/p.db")

    assert load_config(config).db_path == str(home / "procdiag" / "p.db")
    result = CliRunner().invoke(cli, ["status", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "~").exists()


def test_a_tilde_db_option_is_the_home_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    _make_db(home / "p.db")
    result = CliRunner().invoke(cli, ["status", "--db", "~/p.db"])
    assert result.exit_code == 0, result.output
    assert str(home / "p.db") in result.output


@pytest.mark.parametrize("value", ["", "  "])
def test_an_empty_db_path_is_rejected(value):
    with pytest.raises(Exception, match="db_path"):
        ProcdiagConfig(db_path=value)


# ---------------------------------------------------------------------------
# NaN / inf
# ---------------------------------------------------------------------------

MODELS = [
    WatchDir,
    SamplingConfig,
    TriggerConfig,
    BudgetConfig,
    RetentionConfig,
    RulesConfig,
]


def _float_fields(model: type[BaseModel]) -> list[str]:
    return [
        name
        for name, field in model.model_fields.items()
        if float in typing.get_args(field.annotation) or field.annotation is float
    ]


def _model_float_cases() -> list:
    return [
        pytest.param(model, name, id=f"{model.__name__}.{name}")
        for model in MODELS
        for name in _float_fields(model)
    ]


def test_every_numeric_field_is_covered():
    covered = {f"{m.__name__}.{n}" for m in MODELS for n in _float_fields(m)}
    assert {
        "SamplingConfig.system_interval",
        "SamplingConfig.pss_precheck_interval",
        "WatchDir.time_budget_sec",
        "TriggerConfig.psi_io_some10",
        "BudgetConfig.warmup_sec",
        "RetentionConfig.db_max_size_mb",
        "RulesConfig.journal_size_warn_gb",
    } <= covered


@pytest.mark.parametrize(("model", "field"), _model_float_cases())
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_non_finite_number_is_rejected(model, field, value):
    kwargs = {"path": "/x"} if model is WatchDir else {}
    with pytest.raises(Exception, match=field):
        model(**kwargs, **{field: value})


def test_a_nan_rule_override_is_rejected():
    with pytest.raises(Exception, match="overrides"):
        RulesConfig(overrides={"CPU-SATURATION-01": {"psi_threshold": math.nan}})


def test_an_unbounded_rule_override_is_still_allowed():
    overrides = {"CPU-PERIODIC-01": {"tol_sec": math.inf}}
    assert RulesConfig(overrides=overrides).overrides == overrides


def test_a_nan_interval_in_a_config_file_stops_record_at_start(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        "from procdiag.config import ProcdiagConfig, SamplingConfig\n"
        f"config = ProcdiagConfig(db_path={str(tmp_path / 'p.db')!r},\n"
        '    sampling=SamplingConfig(system_interval=float("nan")))\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli, ["record", "--config", str(config), "--duration", "2s"]
    )

    assert result.exit_code == 4, result.output
    assert "system_interval" in result.output
    assert not (tmp_path / "p.db").exists()


def test_config_module_has_no_other_float_model():
    models = [
        obj
        for obj in vars(config_module).values()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel
    ]
    assert {m for m in models if _float_fields(m)} <= set(MODELS)
