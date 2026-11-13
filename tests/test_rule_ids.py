"""Rule IDs and rule settings that name no rule are errors, not a quiet pass.

A typo in ``--only`` / ``--skip`` / ``rules.disabled``, a selection that
leaves nothing to run, or an ``extra_rules`` module that reuses a built-in
rule ID must never make ``check`` / ``compare`` evaluate fewer rules and still
report "no problem".
"""

from __future__ import annotations

import shutil
import sys
import time

import pytest
from click.testing import CliRunner
from synth import add_disk_series, add_sys_baseline, make_db

from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core.rule import _REGISTRY
from procdiag.diagnosis import Engine
from procdiag.storage import Storage

_GB = 1024**3
_HOUR = 3600.0
_HEALTHY = "問題は見つかりませんでした"


@pytest.fixture(scope="module")
def disk_db(tmp_path_factory):
    """A DB on which DISK-FULL-01 is critical (/data full in about 1.5 days)."""
    directory = tmp_path_factory.mktemp("disk")
    now = time.time()
    store = make_db(directory, hours=26)
    add_sys_baseline(store, hours=26, start_ts=now - 26 * _HOUR)
    add_disk_series(
        store,
        "/data",
        total=100 * _GB,
        used_start=80 * _GB,
        daily_growth=8 * _GB,
        days=1.0,
        start_ts=now - 24 * _HOUR,
    )
    path = store.path
    store.close()
    return path


def _config(tmp_path, rules: str = "", report: str = "", encoding="utf-8"):
    path = tmp_path / "config.py"
    path.write_text(
        "from procdiag.config import ProcdiagConfig, ReportConfig, RulesConfig\n"
        f"config = ProcdiagConfig(rules=RulesConfig({rules}),"
        f" report=ReportConfig({report}))\n",
        encoding=encoding,
    )
    return path


def _check(db, config, *args):
    return CliRunner().invoke(
        cli, ["check", "--db", str(db), "--config", str(config), "--no-color", *args]
    )


@pytest.fixture()
def restore_registry():
    saved = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(saved)


@pytest.fixture()
def plugin(tmp_path, monkeypatch, restore_registry):
    """Write a rule module importable as ``site_rules_under_test``.

    It registers a rule of its own, then one under the built-in ID
    DISK-FULL-01 that finds nothing.
    """
    (tmp_path / "site_rules_under_test.py").write_text(
        "from procdiag.core import Rule, register\n"
        "\n"
        "@register\n"
        "class SiteOwnRule(Rule):\n"
        "    rule_id = 'SITE-OWN-01'\n"
        "    min_observation_hours = 0.0\n"
        "    def evaluate(self, store, window):\n"
        "        return []\n"
        "\n"
        "@register\n"
        "class SiteDiskRule(Rule):\n"
        "    rule_id = 'DISK-FULL-01'\n"
        "    min_observation_hours = 0.0\n"
        "    def evaluate(self, store, window):\n"
        "        return []\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "site_rules_under_test"
    sys.modules.pop("site_rules_under_test", None)


class TestOnlyAndSkip:
    def test_known_id_finds_the_critical_disk(self, disk_db, tmp_path):
        result = _check(disk_db, _config(tmp_path), "--only", "DISK-FULL-01")
        assert result.exit_code == 1
        assert "[DISK-FULL-01]" in result.output

    @pytest.mark.parametrize(
        "only", ["DISK-FULL-1", "disk-full-01", "DISK-FULL-01;MEM-LEAK-01"]
    )
    def test_unknown_only_id_is_an_error(self, disk_db, tmp_path, only):
        result = _check(disk_db, _config(tmp_path), "--only", only)
        assert result.exit_code == 4
        assert only in result.output
        assert _HEALTHY not in result.output

    def test_lower_case_id_points_to_the_registered_one(self, disk_db, tmp_path):
        result = _check(disk_db, _config(tmp_path), "--only", "disk-full-01")
        assert result.exit_code == 4
        assert "DISK-FULL-01" in result.output

    def test_unknown_skip_id_is_an_error(self, disk_db, tmp_path):
        result = _check(disk_db, _config(tmp_path), "--skip", "disk-full-01")
        assert result.exit_code == 4
        assert "disk-full-01" in result.output

    @pytest.mark.parametrize(
        "only, exit_code", [("DISK-FULL-01", 1), ("DISK-FULL-1", 4)]
    )
    def test_compare_checks_only_too(self, disk_db, tmp_path, only, exit_code):
        after = tmp_path / "after.db"
        shutil.copy(disk_db, after)
        result = CliRunner().invoke(
            cli,
            ["compare", str(disk_db), str(after), "--config", str(_config(tmp_path))]
            + ["--only", only, "--no-color"],
        )
        assert result.exit_code == exit_code

    @pytest.mark.parametrize(
        "rules, args",
        [
            ("disabled=['DISK-FULL-01']", ["--only", "DISK-FULL-01"]),
            ("", ["--only", "DISK-FULL-01", "--skip", "DISK-FULL-01"]),
        ],
    )
    def test_selection_with_no_rule_left_is_an_error(
        self, disk_db, tmp_path, rules, args
    ):
        result = _check(disk_db, _config(tmp_path, rules=rules), *args)
        assert result.exit_code == 4
        assert _HEALTHY not in result.output

    def test_only_naming_a_disabled_rule_is_an_error(self, disk_db, tmp_path):
        config = _config(tmp_path, rules="disabled=['DISK-FULL-01']")
        result = _check(disk_db, config, "--only", "DISK-FULL-01,ZOMBIE-01")
        assert result.exit_code == 4
        assert "DISK-FULL-01" in result.output

    def test_engine_rejects_an_unknown_id(self, disk_db):
        with Storage(disk_db, read_only=True) as store:
            with pytest.raises(ValueError, match="DISK-FULL-1"):
                Engine(ProcdiagConfig()).run(store, only=["DISK-FULL-1"])


class TestConfigFile:
    def test_disabled_typo_is_rejected_at_startup(self, disk_db, tmp_path):
        config = _config(tmp_path, rules="disabled=['DISK-FULL-1']")
        result = _check(disk_db, config)
        assert result.exit_code == 4
        assert "DISK-FULL-1" in result.output

    @pytest.mark.parametrize(
        "encoding, header",
        [("utf-8-sig", ""), ("cp932", "# -*- coding: cp932 -*-\n")],
    )
    def test_python_source_encodings_are_read(
        self, disk_db, tmp_path, encoding, header
    ):
        config = tmp_path / "config.py"
        config.write_bytes(
            (
                header + "from procdiag.config import ProcdiagConfig, ReportConfig\n"
                "config = ProcdiagConfig("
                "report=ReportConfig(hostname_label='ライン制御PC'))\n"
            ).encode(encoding)
        )
        result = _check(disk_db, config, "--only", "DISK-FULL-01", "--format", "html")
        assert result.exit_code == 1
        assert "<title>ライン制御PC</title>" in result.output


class TestExtraRules:
    def test_plugin_cannot_replace_a_builtin_rule(self, disk_db, tmp_path, plugin):
        config = _config(tmp_path, rules=f"extra_rules=[{plugin!r}]")
        result = _check(disk_db, config, "--only", "DISK-FULL-01", "--verbose")
        assert result.exit_code == 1
        assert "[DISK-FULL-01]" in result.output
        load_failures = [
            line
            for line in result.output.splitlines()
            if f"追加ルール {plugin}" in line
        ]
        assert len(load_failures) == 1 and "DISK-FULL-01" in load_failures[0]

    def test_later_engine_still_runs_the_builtin(self, disk_db, plugin):
        with Storage(disk_db, read_only=True) as store:
            Engine(ProcdiagConfig(rules=RulesConfig(extra_rules=[plugin]))).run(store)
            diag = Engine(ProcdiagConfig()).run(store, only=["DISK-FULL-01"])
        assert [f.rule_id for f in diag.findings] == ["DISK-FULL-01"]
