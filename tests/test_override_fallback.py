"""A threshold of 0 or less that hides a rule's evidence falls back to the default.

procdiag 0.6 accepted ``max_findings = 0`` and the like.  Refusing them at start-up
(exit code 4) would stop a unit that ran before an upgrade: the generated unit does
not restart on 4.  The rule runs with its default for that one threshold, the rest
of the user's overrides still apply, and ``check`` says what it did.
"""

from __future__ import annotations

import time

import pytest
from click.testing import CliRunner
from synth import (
    add_oom_scenario,
    add_proc_io_series,
    add_psi_io_series,
    add_sys_baseline,
    make_db,
)

from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import configure_rule, validate_rule_overrides
from procdiag.diagnosis import Engine
from procdiag.diagnosis.crash import OomKillRule


def _oom(store):
    add_oom_scenario(
        store, die_at_hours=2.0, total_hours=3.0, rss_bytes=int(1.5 * 1024**3)
    )


def _io_pressure(store):
    add_psi_io_series(store, hours=3, psi_io_some10=50.0)
    add_proc_io_series(store, "writer", hours=3, io_write_per_min=500 * 2**20)


def _kernel_errors(store):
    add_sys_baseline(store, hours=3)
    now = time.time()
    store.buffer(
        "kernel_events",
        [
            (now - 300 + i, "io_error", f"I/O error dev sdb sector {i}")
            for i in range(4)
        ],
    )
    store.flush()


def _count(findings):
    return len(findings)


def _writers(findings):
    return len(findings[0].evidence["top_writers"])


def _samples(findings):
    return len(findings[0].evidence["samples"])


CASES = [
    pytest.param("OOM-KILL-01", "max_findings", _oom, _count, id="max_findings"),
    pytest.param("IO-BOTTLENECK-01", "top_n", _io_pressure, _writers, id="top_n"),
    pytest.param(
        "KERNEL-ERROR-01", "max_samples", _kernel_errors, _samples, id="max_samples"
    ),
]


def _cfg(overrides: dict) -> ProcdiagConfig:
    return ProcdiagConfig(
        rules=RulesConfig(min_observation_hours=0.0, overrides=overrides)
    )


def _diagnose(tmp_path, build, overrides):
    store = make_db(tmp_path, hours=3)
    build(store)
    store.conn.commit()
    diag = Engine(_cfg(overrides)).run(store)
    store.close()
    return diag


@pytest.mark.parametrize(("rule_id", "name", "build", "size"), CASES)
@pytest.mark.parametrize("value", [0, -1])
class TestFallsBackToTheDefault:
    def test_start_up_accepts_it(self, rule_id, name, build, size, value):
        assert validate_rule_overrides({rule_id: {name: value}}) == []

    def test_the_rule_reports_as_it_does_by_default(
        self, tmp_path, rule_id, name, build, size, value
    ):
        (tmp_path / "default").mkdir()
        (tmp_path / "zero").mkdir()
        default = _diagnose(tmp_path / "default", build, {})
        zero = _diagnose(tmp_path / "zero", build, {rule_id: {name: value}})

        def mine(diag):
            return [f for f in diag.findings if f.rule_id == rule_id]

        assert size(mine(default)) > 0
        assert size(mine(zero)) == size(mine(default))
        assert rule_id not in zero.errored_rules

    def test_check_says_which_threshold_it_did_not_use(
        self, tmp_path, rule_id, name, build, size, value
    ):
        diag = _diagnose(tmp_path, build, {rule_id: {name: value}})

        (note,) = [n for n in diag.quality.notes if name in n]
        assert note.startswith(f"{rule_id}: {name} = {value} ")


def test_the_default_is_named_in_the_note(tmp_path):
    diag = _diagnose(tmp_path, _oom, {"OOM-KILL-01": {"max_findings": 0}})

    (note,) = [n for n in diag.quality.notes if "max_findings" in n]
    assert f"既定値 {OomKillRule.max_findings} " in note


def test_the_rules_other_overrides_still_apply():
    cfg = _cfg({"OOM-KILL-01": {"max_findings": 0, "gap_threshold_s": 123.0}})

    rule, reason = configure_rule(OomKillRule, cfg)

    assert rule.max_findings == OomKillRule.max_findings
    assert rule.gap_threshold_s == 123.0
    assert reason is not None and "max_findings" in reason


def test_a_positive_value_is_used_and_reports_nothing():
    rule, reason = configure_rule(
        OomKillRule, _cfg({"OOM-KILL-01": {"max_findings": 5}})
    )

    assert rule.max_findings == 5 and reason is None


def test_check_runs_through_the_cli_and_names_the_value(tmp_path):
    store = make_db(tmp_path, hours=3)
    _oom(store)
    store.close()
    config = tmp_path / "config.py"
    config.write_text(
        "from procdiag.config import ProcdiagConfig, RulesConfig\n"
        "config = ProcdiagConfig(rules=RulesConfig(min_observation_hours=0.0,\n"
        '    overrides={"OOM-KILL-01": {"max_findings": 0}}))\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["check", "--config", str(config), "--db", str(tmp_path / "synth.db")],
    )

    assert result.exit_code in (0, 1, 2), result.output
    assert "OOM-KILL-01" in result.output
    assert "max_findings = 0 " in result.output


def test_a_non_tunable_name_is_still_refused_at_start_up():
    problems = validate_rule_overrides({"OOM-KILL-01": {"bogus": 1}})

    assert problems and "bogus" in problems[0]
