"""Tests for procdiag.cli / procdiag.service / procdiag.export.

All tests use ``click.testing.CliRunner`` for CLI invocation.  Tests for
``record`` and ``check`` only verify ``--help`` output and argument
validation — they never invoke the real Recorder, Engine, or report
renderers (those are T05/T06/T07, parallel tasks).

Conventions that mirror the netdiag test suite:
- ``CliRunner(mix_stderr=False)`` keeps stdout/stderr separate.
- Fixture ``tmp_db`` (from conftest.py) provides a ready Storage instance.
- Tests that write files use ``tmp_path`` via monkeypatching or direct paths.
"""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from procdiag.cli import cli
from procdiag.config import load_config
from procdiag.core.rule import Rule, register
from procdiag.core.sampler import Sampler, register_sampler
from procdiag.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner():
    """CliRunner instance."""
    return CliRunner()


@pytest.fixture()
def db_with_data(tmp_path) -> Path:
    """A Storage DB with a handful of sys_samples rows."""
    db_path = tmp_path / "test.db"
    with Storage(db_path) as store:
        store.set_meta("schema_version", "1")
        store.set_meta("hostname", "testhost")
        now = time.time()
        rows = []
        for i in range(5):
            rows.append(
                (
                    now - (4 - i) * 60,  # ts
                    20.0 + i,  # cpu_total
                    30.0,  # cpu_max_core
                    1_000_000_000,  # mem_used
                    500_000_000,  # mem_available
                    0,  # swap_used
                    0,  # swap_in
                    0,  # swap_out
                    0.5,  # load1
                    0.0,  # psi_cpu_some10
                    0.0,  # psi_mem_some10
                    0.0,  # psi_mem_full10
                    0.0,  # psi_io_some10
                    0.0,  # psi_io_full10
                    1024,  # disk_read_bytes
                    512,  # disk_write_bytes
                    0.1,  # self_cpu
                    50_000_000,  # self_rss
                )
            )
        store.buffer("sys_samples", rows)
        store.flush()
        # Also insert an ops_event
        store.record_event("test_event", "テストイベント")
    return db_path


@pytest.fixture()
def db_with_rollup(tmp_path) -> Path:
    """A DB with sys_rollup_1m rows for export tests."""
    db_path = tmp_path / "rollup.db"
    with Storage(db_path) as store:
        now = time.time()
        minute = int(now / 60) * 60.0
        rows = []
        for i in range(3):
            rows.append(
                (
                    minute - (2 - i) * 60,  # ts_min
                    20.0 + i * 5,  # cpu_avg
                    50.0,  # cpu_max
                    45.0,  # cpu_p95
                    60.0,  # cpu_max_core_max
                    800_000_000,  # mem_used_avg
                    900_000_000,  # mem_used_max
                    0.0,  # psi_cpu_max
                    0.0,  # psi_mem_max
                    0.0,  # psi_io_max
                    0,  # swap_out_sum
                )
            )
        store.buffer("sys_rollup_1m", rows)
        store.flush()
    return db_path


# ---------------------------------------------------------------------------
# --help and --version
# ---------------------------------------------------------------------------


class TestHelpAndVersion:
    def test_root_help_lists_commands(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # Core commands must appear in help
        for cmd in (
            "init",
            "record",
            "check",
            "report",
            "status",
            "explain",
            "list-rules",
            "list-samplers",
            "export",
            "prune",
            "service",
        ):
            assert cmd in result.output, f"'{cmd}' が --help に見つかりません"

    def test_advise_is_hidden(self, runner):
        """advise must not appear in the main --help listing."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "advise" not in result.output

    def test_advise_help_works(self, runner):
        """advise must still be callable (just hidden)."""
        result = runner.invoke(cli, ["advise", "--help"])
        assert result.exit_code == 0

    def test_version(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "procdiag" in result.output.lower() or "0." in result.output

    def test_record_help(self, runner):
        result = runner.invoke(cli, ["record", "--help"])
        assert result.exit_code == 0
        assert "--duration" in result.output
        assert "--config" in result.output
        assert "--db" in result.output

    def test_check_help(self, runner):
        result = runner.invoke(cli, ["check", "--help"])
        assert result.exit_code == 0
        for opt in (
            "--db",
            "--config",
            "--only",
            "--skip",
            "--format",
            "--output",
            "--verbose",
            "--no-color",
        ):
            assert opt in result.output

    def test_service_subcommands_help(self, runner):
        result = runner.invoke(cli, ["service", "--help"])
        assert result.exit_code == 0
        assert "print" in result.output
        assert "install" in result.output


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


class TestInit:
    def test_creates_config_and_data_dir(self, runner, tmp_path):
        result = runner.invoke(cli, ["init", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "config.py").exists()
        assert (tmp_path / "data").is_dir()

    def test_does_not_overwrite_existing_config(self, runner, tmp_path):
        config_path = tmp_path / "config.py"
        config_path.write_text("# existing", encoding="utf-8")
        result = runner.invoke(cli, ["init", str(tmp_path)])
        assert result.exit_code == 0
        # Original content preserved
        assert config_path.read_text() == "# existing"
        # Warning emitted
        assert "警告" in result.output or "上書き" in result.output

    def test_generated_config_is_loadable(self, runner, tmp_path):
        runner.invoke(cli, ["init", str(tmp_path)])
        config_path = tmp_path / "config.py"
        cfg = load_config(config_path)
        from procdiag.config import ProcdiagConfig

        assert isinstance(cfg, ProcdiagConfig)

    def test_default_dir_is_cwd(self, runner, tmp_path):
        """init without DIR argument writes to CWD."""
        result = runner.invoke(
            cli, ["init"], catch_exceptions=False, env={"HOME": str(tmp_path)}
        )
        # Just check it doesn't crash; actual file written to real CWD.
        # We don't assert file existence here to avoid polluting the repo.
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# explain / list-rules / list-samplers
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_rule():
    """Register a fake rule for the duration of the test, then clean up."""
    from procdiag.core.rule import _REGISTRY

    @register
    class FakeRule(Rule):
        rule_id = "FAKE-TEST-01"
        category = "test"
        title = "テスト用フェイクルール"
        what = "テスト検出内容"
        why = "テスト理由"
        manual_commands = ["echo test"]

    yield FakeRule
    _REGISTRY.pop("FAKE-TEST-01", None)


@pytest.fixture()
def fake_sampler():
    """Register a fake sampler for the duration of the test, then clean up."""
    from procdiag.core.sampler import _SAMPLER_REGISTRY

    @register_sampler
    class FakeSampler(Sampler):
        name = "fake_sampler"
        title = "フェイクサンプラー"
        tier = "T1"
        interval_key = "system_interval"
        what = "フェイク計測"
        why = "フェイク理由"
        manual_commands = ["echo sampler"]

    yield FakeSampler
    _SAMPLER_REGISTRY.pop("fake_sampler", None)


class TestExplain:
    def test_list_all_no_args(self, runner, fake_rule, fake_sampler):
        result = runner.invoke(cli, ["explain"])
        assert result.exit_code == 0
        assert "FAKE-TEST-01" in result.output
        assert "fake_sampler" in result.output

    def test_explain_rule_detail(self, runner, fake_rule):
        result = runner.invoke(cli, ["explain", "FAKE-TEST-01"])
        assert result.exit_code == 0
        assert "FAKE-TEST-01" in result.output
        assert "テスト検出内容" in result.output
        assert "echo test" in result.output

    def test_explain_sampler_detail(self, runner, fake_sampler):
        result = runner.invoke(cli, ["explain", "fake_sampler"])
        assert result.exit_code == 0
        assert "fake_sampler" in result.output
        assert "フェイク計測" in result.output

    def test_explain_unknown_exits_nonzero(self, runner):
        result = runner.invoke(cli, ["explain", "NO-SUCH-RULE"])
        assert result.exit_code != 0

    def test_explain_works_without_diagnosis_module(self, runner, fake_rule):
        """explain must not crash even when diagnosis is not yet importable."""
        result = runner.invoke(cli, ["explain"])
        assert result.exit_code == 0


class TestListRules:
    def test_shows_fake_rule(self, runner, fake_rule):
        result = runner.invoke(cli, ["list-rules"])
        assert result.exit_code == 0
        assert "FAKE-TEST-01" in result.output
        assert "テスト用フェイクルール" in result.output

    def test_no_crash_without_rules(self, runner):
        result = runner.invoke(cli, ["list-rules"])
        assert result.exit_code == 0


class TestListSamplers:
    def test_shows_fake_sampler(self, runner, fake_sampler):
        result = runner.invoke(cli, ["list-samplers"])
        assert result.exit_code == 0
        assert "fake_sampler" in result.output
        assert "T1" in result.output

    def test_no_crash_without_samplers(self, runner):
        result = runner.invoke(cli, ["list-samplers"])
        assert result.exit_code == 0

    def test_lists_every_builtin_sampler(self, runner):
        """All recorder samplers are listed (thermal used to be missing)."""
        result = runner.invoke(cli, ["list-samplers"])
        assert result.exit_code == 0
        for name in ("system", "cgroup", "process", "disk", "thermal"):
            assert name in result.output

    def test_explain_thermal_sampler(self, runner):
        result = runner.invoke(cli, ["explain", "thermal"])
        assert result.exit_code == 0
        assert "thermal_interval" in result.output


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_shows_summary(self, runner, db_with_data):
        result = runner.invoke(cli, ["status", "--db", str(db_with_data)])
        assert result.exit_code == 0
        assert "DB:" in result.output
        assert "サンプル数" in result.output

    def test_shows_events(self, runner, db_with_data):
        result = runner.invoke(cli, ["status", "--db", str(db_with_data)])
        assert result.exit_code == 0
        assert "test_event" in result.output or "イベント" in result.output

    def test_json_format(self, runner, db_with_data):
        result = runner.invoke(
            cli, ["status", "--db", str(db_with_data), "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "counts" in data
        assert "meta" in data

    def test_json_reports_span_and_self_cpu(self, runner, db_with_data):
        result = runner.invoke(
            cli, ["status", "--db", str(db_with_data), "--format", "json"]
        )
        data = json.loads(result.output)
        assert data["has_data"] is True
        assert data["span"] != "—" and data["start"] < data["end"]
        assert data["self_cpu_avg"] == pytest.approx(0.1)
        assert [e["kind"] for e in data["recent_events"]] == ["test_event"]

    def test_db_path_taken_from_config(self, runner, db_with_data, tmp_path):
        """status --config resolves the DB from the config file's db_path."""
        cfg = tmp_path / "site.py"
        cfg.write_text(
            "from procdiag.config import ProcdiagConfig\n"
            f"config = ProcdiagConfig(db_path={str(db_with_data)!r})\n",
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", "--config", str(cfg)])
        assert result.exit_code == 0, result.output
        assert str(db_with_data) in result.output

    def test_no_db_friendly_error(self, runner, tmp_path):
        missing = str(tmp_path / "nonexistent.db")
        result = runner.invoke(cli, ["status", "--db", missing])
        assert result.exit_code != 0
        # No traceback — just a Japanese message
        assert "エラー" in result.output
        assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class TestExport:
    def test_csv_header_and_rows(self, runner, db_with_rollup):
        result = runner.invoke(
            cli,
            [
                "export",
                "--db",
                str(db_with_rollup),
                "--format",
                "csv",
                "--rollup",
                "1min",
            ],
        )
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        # First line is header
        assert "ts_min" in lines[0]
        assert "cpu_avg" in lines[0]
        # At least 3 data rows + 1 header
        assert len(lines) >= 4

    def test_json_format_loadable(self, runner, db_with_rollup):
        result = runner.invoke(
            cli,
            [
                "export",
                "--db",
                str(db_with_rollup),
                "--format",
                "json",
                "--rollup",
                "1min",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 3
        assert "ts_min" in data[0]

    def test_raw_table_export(self, runner, db_with_data):
        result = runner.invoke(
            cli,
            ["export", "--db", str(db_with_data), "--format", "csv", "--rollup", "raw"],
        )
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert "ts" in lines[0]
        assert "cpu_total" in lines[0]
        assert len(lines) >= 6  # 1 header + 5 data rows

    def test_export_to_file(self, runner, db_with_rollup, tmp_path):
        out_file = str(tmp_path / "out.csv")
        result = runner.invoke(
            cli,
            [
                "export",
                "--db",
                str(db_with_rollup),
                "--format",
                "csv",
                "--out",
                out_file,
            ],
        )
        assert result.exit_code == 0
        assert Path(out_file).exists()
        content = Path(out_file).read_text()
        assert "ts_min" in content

    def test_no_db_friendly_error(self, runner, tmp_path):
        missing = str(tmp_path / "nonexistent.db")
        result = runner.invoke(cli, ["export", "--db", missing])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_deletes_old_rows(self, runner, tmp_path):
        """Insert rows older than 48h and verify they are pruned."""
        db_path = tmp_path / "prune.db"
        old_ts = time.time() - 72 * 3600  # 72 hours ago

        with Storage(db_path) as store:
            row = (
                old_ts,
                10.0,
                20.0,
                500_000_000,
                200_000_000,
                0,
                0,
                0,
                0.1,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0,
                0,
                0.05,
                10_000_000,
            )
            store.buffer("sys_samples", [row])
            store.flush()

        result = runner.invoke(
            cli,
            ["prune", "--db", str(db_path), "--raw", "48h", "--rollup", "90d"],
        )
        assert result.exit_code == 0
        assert "削除完了" in result.output

        # Verify the row was actually deleted
        with Storage(db_path, read_only=True) as store:
            counts = store.counts()
        assert counts["sys_samples"] == 0

    def test_prune_output_shows_counts(self, runner, tmp_path):
        db_path = tmp_path / "prune2.db"
        with Storage(db_path) as _:
            pass  # just create the schema

        result = runner.invoke(
            cli,
            ["prune", "--db", str(db_path)],
        )
        assert result.exit_code == 0
        assert "削除完了" in result.output


# ---------------------------------------------------------------------------
# service print
# ---------------------------------------------------------------------------


class TestServicePrint:
    def test_contains_cpu_quota(self, runner):
        result = runner.invoke(cli, ["service", "print"])
        assert result.exit_code == 0
        assert "CPUQuota=2%" in result.output

    def test_contains_exec_start(self, runner):
        result = runner.invoke(cli, ["service", "print"])
        assert result.exit_code == 0
        assert "ExecStart=" in result.output
        assert "procdiag" in result.output.lower() or "cli" in result.output

    def test_system_flag_changes_target(self, runner):
        result = runner.invoke(cli, ["service", "print", "--system"])
        assert result.exit_code == 0
        assert "multi-user.target" in result.output

    def test_user_unit_default_target(self, runner):
        result = runner.invoke(cli, ["service", "print"])
        assert result.exit_code == 0
        assert "default.target" in result.output

    def test_with_config_and_db(self, runner):
        result = runner.invoke(
            cli,
            [
                "service",
                "print",
                "--config",
                "/etc/procdiag/config.py",
                "--db",
                "/var/lib/procdiag/data.db",
            ],
        )
        assert result.exit_code == 0
        assert "--config" in result.output
        assert "--db" in result.output


# ---------------------------------------------------------------------------
# service install
# ---------------------------------------------------------------------------


class TestServiceInstall:
    def test_file_written(self, runner, tmp_path, monkeypatch):
        """install must write the unit file to HOME/.config/systemd/user/."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # Patch Path.home() via monkeypatching the service module
        import procdiag.service as svc_mod

        original_install = svc_mod.install_unit

        def patched_install(**kwargs):
            kwargs["home_override"] = tmp_path
            return original_install(**kwargs)

        monkeypatch.setattr(svc_mod, "install_unit", patched_install)

        result = runner.invoke(cli, ["service", "install"])
        assert result.exit_code == 0

        unit_path = (
            tmp_path / ".config" / "systemd" / "user" / "procdiag-record.service"
        )
        assert unit_path.exists(), f"ユニットファイルが存在しません: {unit_path}"
        content = unit_path.read_text()
        assert "CPUQuota=2%" in content

    def test_systemctl_not_called(self, runner, tmp_path, monkeypatch):
        """install must NOT execute systemctl."""
        import procdiag.service as svc_mod

        called_with: list[str] = []
        original_install = svc_mod.install_unit

        def patched_install(**kwargs):
            kwargs["home_override"] = tmp_path
            return original_install(**kwargs)

        monkeypatch.setattr(svc_mod, "install_unit", patched_install)

        # Patch subprocess to detect any systemctl calls.
        import subprocess

        original_run = subprocess.run

        def fake_run(args, **kwargs):
            called_with.extend(args if isinstance(args, list) else [args])
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(cli, ["service", "install"])
        assert result.exit_code == 0
        assert "systemctl" not in called_with, "systemctl が実行されました"

    def test_enable_commands_printed(self, runner, tmp_path, monkeypatch):
        import procdiag.service as svc_mod

        original_install = svc_mod.install_unit

        def patched_install(**kwargs):
            kwargs["home_override"] = tmp_path
            return original_install(**kwargs)

        monkeypatch.setattr(svc_mod, "install_unit", patched_install)

        result = runner.invoke(cli, ["service", "install"])
        assert result.exit_code == 0
        assert "systemctl --user daemon-reload" in result.output
        assert "enable" in result.output


# ---------------------------------------------------------------------------
# export_rows unit tests (direct function tests)
# ---------------------------------------------------------------------------


class TestExportRows:
    def test_csv_output(self, db_with_rollup):
        from procdiag.export import export_rows

        buf = io.StringIO()
        with Storage(db_with_rollup, read_only=True) as store:
            count = export_rows(store, table="sys_rollup_1m", fmt="csv", out=buf)

        assert count == 3
        buf.seek(0)
        reader = csv.reader(buf)
        rows = list(reader)
        assert rows[0][0] == "ts_min"
        assert len(rows) == 4  # 1 header + 3 data

    def test_json_output(self, db_with_rollup):
        from procdiag.export import export_rows

        buf = io.StringIO()
        with Storage(db_with_rollup, read_only=True) as store:
            count = export_rows(store, table="sys_rollup_1m", fmt="json", out=buf)

        assert count == 3
        buf.seek(0)
        data = json.loads(buf.getvalue())
        assert isinstance(data, list)
        assert len(data) == 3
        assert "ts_min" in data[0]
        assert "cpu_avg" in data[0]

    def test_unknown_table_raises(self, db_with_rollup):
        from procdiag.export import export_rows

        buf = io.StringIO()
        with Storage(db_with_rollup, read_only=True) as store:
            with pytest.raises(ValueError, match="不明なテーブル"):
                export_rows(store, table="no_such_table", fmt="csv", out=buf)

    def test_unknown_format_raises(self, db_with_rollup):
        from procdiag.export import export_rows

        buf = io.StringIO()
        with Storage(db_with_rollup, read_only=True) as store:
            with pytest.raises(ValueError, match="不明なフォーマット"):
                export_rows(store, table="sys_rollup_1m", fmt="xml", out=buf)


# ---------------------------------------------------------------------------
# check --format html — charts must render (issue #1 §1-1)
# ---------------------------------------------------------------------------


class TestCheckHtmlCharts:
    """`check --format html` must build charts from the live DB connection.

    Regression guard for the bug where ``build_charts(store)`` ran after the
    ``with Storage(...)`` block had already closed the connection, so every
    chart silently rendered as 'データなし'.
    """

    def test_check_html_renders_chart_data(self, runner, db_with_data):
        result = runner.invoke(
            cli, ["check", "--db", str(db_with_data), "--format", "html"]
        )
        assert result.exit_code == 0, result.output
        html = result.output
        # A populated series renders an SVG <polyline>; an empty one renders
        # the 'データなし: <label>' placeholder.  The bug produced the latter.
        assert "<polyline" in html
        assert "データなし: CPU使用率" not in html

    def test_check_html_matches_report_charts(self, runner, db_with_data, tmp_path):
        out = tmp_path / "report.html"
        res_report = runner.invoke(
            cli, ["report", "--db", str(db_with_data), "--out", str(out)]
        )
        assert res_report.exit_code == 0, res_report.output
        report_html = out.read_text(encoding="utf-8")

        res_check = runner.invoke(
            cli, ["check", "--db", str(db_with_data), "--format", "html"]
        )
        assert res_check.exit_code == 0, res_check.output

        # Both paths must draw charts; previously only `report` did.
        assert "<polyline" in report_html
        assert "<polyline" in res_check.output


# ---------------------------------------------------------------------------
# check --exit-zero-on-info (issue #6 §5-3)
# ---------------------------------------------------------------------------


class TestCheckExitZeroOnInfo:
    def _info_only_db(self, tmp_path) -> Path:
        from synth import add_dir_series, add_sys_baseline

        _mb = 1024 * 1024
        _gb = 1024 * _mb
        db = tmp_path / "info.db"
        with Storage(db) as store:
            add_sys_baseline(store, hours=72.0)
            # 150 MB/day is in the INFO band (100–200 MB/day) for DISK-GROWTH-01.
            add_dir_series(
                store,
                "/var/log/grow",
                start=10 * _gb,
                daily_growth=150 * _mb,
                days=3.0,
            )
        return db

    def test_info_only_exits_2_by_default(self, runner, tmp_path):
        db = self._info_only_db(tmp_path)
        result = runner.invoke(cli, ["check", "--db", str(db)])
        assert result.exit_code == 2, result.output

    def test_exit_zero_on_info_flag_returns_0(self, runner, tmp_path):
        db = self._info_only_db(tmp_path)
        result = runner.invoke(cli, ["check", "--db", str(db), "--exit-zero-on-info"])
        assert result.exit_code == 0, result.output

    def test_flag_shown_in_help(self, runner):
        result = runner.invoke(cli, ["check", "--help"])
        assert "--exit-zero-on-info" in result.output


# ---------------------------------------------------------------------------
# check on an empty DB — must not look healthy (issue v0.3 §1-1 / A-01)
# ---------------------------------------------------------------------------


class TestCheckNoData:
    def test_empty_db_exits_3_and_warns(self, runner, tmp_path):
        db = tmp_path / "empty.db"
        with Storage(db):
            pass  # create schema only — zero samples
        result = runner.invoke(cli, ["check", "--db", str(db)])
        assert result.exit_code == 3, result.output
        assert "観測データ" in result.output
        assert "問題は見つかりませんでした" not in result.output

    def test_empty_db_json_has_no_data_true(self, runner, tmp_path):
        db = tmp_path / "empty2.db"
        with Storage(db):
            pass
        result = runner.invoke(cli, ["check", "--db", str(db), "--format", "json"])
        assert result.exit_code == 3, result.output
        data = json.loads(result.output)
        assert data["no_data"] is True
        assert data["healthy"] is True


# ---------------------------------------------------------------------------
# CLI cluster improvements — issue v0.3 §1-2 / 1-6 / 1-8 / 1-9
# ---------------------------------------------------------------------------


class TestCliClusterImprovements:
    def test_check_db_missing_exits_4(self, runner, tmp_path):
        """DB-missing is a precondition error (exit 4), distinct from 1."""
        result = runner.invoke(cli, ["check", "--db", str(tmp_path / "nope.db")])
        assert result.exit_code == 4
        assert "DB ファイル" in result.output

    def test_check_quiet_suppresses_message(self, runner, db_with_data, tmp_path):
        out = tmp_path / "r.txt"
        result = runner.invoke(
            cli,
            ["check", "--db", str(db_with_data), "--output", str(out), "--quiet"],
        )
        assert result.exit_code in (0, 2)
        assert "書き出しました" not in result.output

    def test_check_quiet_via_env(self, runner, db_with_data, tmp_path):
        out = tmp_path / "r2.txt"
        result = runner.invoke(
            cli,
            ["check", "--db", str(db_with_data), "--output", str(out)],
            env={"PROCDIAG_QUIET": "1"},
        )
        assert "書き出しました" not in result.output

    def test_doctor_ascii_flag(self, runner):
        result = runner.invoke(cli, ["doctor", "--ascii"])
        assert result.exit_code in (0, 2)
        assert ("[OK]" in result.output) or ("[WARN]" in result.output)
        assert "✔" not in result.output
        assert "⚠" not in result.output

    def test_status_empty_db_warns(self, runner, tmp_path):
        db = tmp_path / "empty.db"
        with Storage(db):
            pass
        result = runner.invoke(cli, ["status", "--db", str(db)])
        assert result.exit_code == 0
        assert "まだサンプルがありません" in result.output
