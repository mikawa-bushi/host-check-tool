"""Tests for per-rule threshold overrides (config.rules.overrides)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from synth import (
    add_cpu_saturation_samples,
    add_proc_series,
    add_sys_baseline,
    make_db,
)

from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Rule, configure_rule, validate_rule_overrides
from procdiag.diagnosis import Engine
from procdiag.diagnosis.cpu import CpuSaturationRule
from procdiag.diagnosis.io_disk import DiskJournalRule


def _cfg(overrides: dict, **rules) -> ProcdiagConfig:
    return ProcdiagConfig(
        rules=RulesConfig(min_observation_hours=0.0, overrides=overrides, **rules)
    )


class TestTunables:
    def test_numeric_class_attributes_are_tunable(self):
        tunables = CpuSaturationRule.tunables()
        assert tunables["psi_threshold"] == 20.0
        assert tunables["min_consecutive_minutes"] == 15
        assert "min_observation_hours" in tunables  # inherited from Rule
        assert "rule_id" not in tunables and "default_severity" not in tunables

    def test_configure_applies_and_coerces(self):
        rule = CpuSaturationRule()
        rule.configure(
            _cfg(
                {
                    "CPU-SATURATION-01": {
                        "psi_threshold": 35,
                        "min_consecutive_minutes": 30.0,
                    }
                }
            )
        )
        assert rule.psi_threshold == 35.0 and isinstance(rule.psi_threshold, float)
        assert rule.min_consecutive_minutes == 30
        assert isinstance(rule.min_consecutive_minutes, int)
        assert CpuSaturationRule.psi_threshold == 20.0  # class default untouched

    def test_configure_rejects_unknown_and_fractional_int(self):
        with pytest.raises(ValueError, match="調整可能なしきい値ではありません"):
            CpuSaturationRule().configure(_cfg({"CPU-SATURATION-01": {"bogus": 1}}))
        with pytest.raises(ValueError, match="整数"):
            CpuSaturationRule().configure(
                _cfg({"CPU-SATURATION-01": {"min_consecutive_minutes": 2.5}})
            )

    def test_explicit_override_beats_dedicated_setting(self):
        rule = DiskJournalRule()
        rule.configure(
            _cfg(
                {"DISK-JOURNAL-01": {"journal_size_warn_gb": 9}},
                journal_size_warn_gb=2.0,
            )
        )
        assert rule.journal_size_warn_gb == 9.0

    def test_configure_rule_reports_error_and_keeps_defaults(self):
        rule, error = configure_rule(
            CpuSaturationRule, _cfg({"CPU-SATURATION-01": {"bogus": 1}})
        )
        assert error is not None and "bogus" in error
        assert rule.psi_threshold == CpuSaturationRule.psi_threshold
        rule, error = configure_rule(
            CpuSaturationRule, _cfg({"CPU-SATURATION-01": {"psi_threshold": 35}})
        )
        assert error is None and rule.psi_threshold == 35.0

    @pytest.mark.parametrize("where", ["init", "configure"])
    def test_a_rule_that_sets_its_own_min_hours_keeps_it(self, where):
        class OwnHoursRule(Rule):
            rule_id = "X-OWN-HOURS-01"

            def __init__(self):
                if where == "init":
                    self.min_observation_hours = 2.0

            def configure(self, config):
                if where == "configure":
                    self.min_observation_hours = 2.0
                super().configure(config)

        rule, error = configure_rule(OwnHoursRule, ProcdiagConfig())
        assert error is None and rule.min_observation_hours == 2.0

    def test_validate_reports_every_problem(self):
        import procdiag.diagnosis  # noqa: F401  (fills the registry)

        problems = validate_rule_overrides(
            {"NO-SUCH-01": {"x": 1}, "CPU-SATURATION-01": {"psi_thresh": 30}}
        )
        assert len(problems) == 2
        assert "NO-SUCH-01" in problems[0] and "psi_thresh" in problems[1]


class TestEngineAndCli:
    @pytest.mark.parametrize(
        "overrides, ran",
        [({}, False), ({"MEM-LEAK-01": {"min_observation_hours": 6.0}}, True)],
    )
    def test_min_hours_override_equal_to_the_default_wins(
        self, tmp_path, overrides, ran
    ):
        store = make_db(tmp_path, hours=8)
        add_sys_baseline(store, hours=8)
        add_proc_series(
            store, "myapp", rss_start=100 * 2**20, rss_slope_per_h=0.0, hours=8
        )
        config = ProcdiagConfig(
            rules=RulesConfig(min_observation_hours=60.0, overrides=overrides)
        )
        diag = Engine(config).run(store, only=["MEM-LEAK-01"])
        store.close()
        assert ("MEM-LEAK-01" in diag.evaluated_rules) is ran
        assert bool(diag.skipped_rules) is not ran

    def _saturated_db(self, tmp_path):
        store = make_db(tmp_path, hours=1.0)
        add_cpu_saturation_samples(store, psi_cpu_high=35.0, duration_minutes=20)
        store.conn.commit()
        return store

    def test_override_changes_detection(self, tmp_path):
        store = self._saturated_db(tmp_path)
        default = Engine(_cfg({})).run(store, only=["CPU-SATURATION-01"])
        raised = Engine(_cfg({"CPU-SATURATION-01": {"psi_threshold": 50}})).run(
            store, only=["CPU-SATURATION-01"]
        )
        store.close()
        assert [f.rule_id for f in default.findings] == ["CPU-SATURATION-01"]
        assert raised.findings == []

    def test_engine_falls_back_to_defaults_on_bad_override(self, tmp_path):
        store = self._saturated_db(tmp_path)
        diag = Engine(_cfg({"CPU-SATURATION-01": {"bogus": 1}})).run(
            store, only=["CPU-SATURATION-01"]
        )
        store.close()
        assert [f.rule_id for f in diag.findings] == ["CPU-SATURATION-01"]
        assert any("設定の適用に失敗" in r for r in diag.reasoning)

    def test_cli_rejects_typo_at_startup(self, tmp_path):
        store = self._saturated_db(tmp_path)
        store.close()
        cfg = tmp_path / "config.py"
        cfg.write_text(
            "from procdiag.config import ProcdiagConfig, RulesConfig\n"
            f"config = ProcdiagConfig(db_path={str(tmp_path / 'procdiag.db')!r},\n"
            "    rules=RulesConfig(overrides={'CPU-SATURATION-01': {'psi_thresh': 30}}))\n",
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["check", "--config", str(cfg)])
        assert result.exit_code == 4
        assert "psi_thresh" in result.output

    def test_explain_lists_tunables(self):
        result = CliRunner().invoke(cli, ["explain", "CPU-SATURATION-01"])
        assert result.exit_code == 0
        assert "調整可能なしきい値" in result.output
        assert "psi_threshold (20.0)" in result.output

    def test_chart_threshold_follows_override(self, tmp_path):
        from procdiag.report import build_charts

        store = make_db(tmp_path, hours=1.0)
        charts = build_charts(
            store, config=_cfg({"CPU-SATURATION-01": {"psi_threshold": 42}})
        )
        store.close()
        assert charts.psi_cpu_threshold == 42.0
        assert charts.core_pct_threshold == 95.0

    def test_chart_threshold_ignores_bad_override(self, tmp_path):
        """Library callers that skip CLI validation still get a report."""
        from procdiag.report import build_charts

        store = make_db(tmp_path, hours=1.0)
        charts = build_charts(store, config=_cfg({"CPU-SATURATION-01": {"bogus": 1}}))
        store.close()
        assert charts.psi_cpu_threshold == CpuSaturationRule.psi_threshold
