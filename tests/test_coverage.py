from __future__ import annotations

import json
import sqlite3
import time

from click.testing import CliRunner
from synth import add_proc_series, add_sys_baseline, make_db
from test_rules_v4 import _make_v3_db

from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import CoverageGap, Window
from procdiag.diagnosis import Engine
from procdiag.report import render_json, render_terminal
from procdiag.storage import Storage

_MB = 1024 * 1024
_HOUR = 3600.0
_RAW_RULES = {
    "CPU-SATURATION-01",
    "IO-BOTTLENECK-01",
    "SWAP-THRASH-01",
    "OOM-KILL-01",
    "ZOMBIE-01",
}


def _cfg(min_hours: float = 6.0) -> ProcdiagConfig:
    return ProcdiagConfig(rules=RulesConfig(min_observation_hours=min_hours))


def _rollup_only_db(tmp_path):
    store = make_db(tmp_path, hours=12)
    add_sys_baseline(store, hours=12)
    add_proc_series(
        store, "leaky", rss_start=200 * _MB, rss_slope_per_h=5 * _MB, hours=12
    )
    store.conn.execute("DELETE FROM sys_samples")
    store.conn.commit()
    path = store.path
    store.close()
    return path


def _v2_db(tmp_path):
    db = tmp_path / "v2.db"
    _make_v3_db(db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE thermal_samples")
    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    return db


class TestUncoveredRules:
    def test_rollup_only_db_marks_raw_rules_uncovered(self, tmp_path):
        with Storage(_rollup_only_db(tmp_path), read_only=True) as store:
            diag = Engine(_cfg()).run(store)
        uncovered = {r.rule_id: r for r in diag.uncovered_rules}
        assert _RAW_RULES <= set(uncovered)
        assert uncovered["CPU-SATURATION-01"].reason is CoverageGap.NOT_RECORDED
        assert uncovered["CPU-SATURATION-01"].missing == ("sys_samples",)
        assert "proc_samples" in uncovered["OOM-KILL-01"].missing
        assert not _RAW_RULES & set(diag.evaluated_rules)
        assert "MEM-LEAK-01" in diag.evaluated_rules
        assert [f.rule_id for f in diag.findings] == ["MEM-LEAK-01"]
        assert not diag.fully_evaluated

    def test_older_schema_reports_new_rules_uncovered_not_clear(self, tmp_path):
        new_rules = [
            "SERVICE-RESTART-01",
            "KERNEL-ERROR-01",
            "DISK-INODE-01",
            "FS-READONLY-01",
            "UNCLEAN-SHUTDOWN-01",
            "THERMAL-01",
        ]
        with Storage(_v2_db(tmp_path), read_only=True) as store:
            diag = Engine(_cfg(0.0)).run(store, only=new_rules)
        assert diag.evaluated_rules == []
        assert diag.errored_rules == []
        uncovered = {r.rule_id: r for r in diag.uncovered_rules}
        assert set(uncovered) == set(new_rules)
        assert all(r.reason is CoverageGap.SCHEMA_MISSING for r in uncovered.values())
        assert uncovered["DISK-INODE-01"].missing == (
            "disk_samples.inodes_total",
            "disk_samples.inodes_used",
        )
        assert uncovered["THERMAL-01"].missing == ("thermal_samples",)
        assert uncovered["SERVICE-RESTART-01"].missing == ("service_events",)

    def test_sampler_never_collected_on_this_host_is_not_a_gap(self, tmp_path):
        store = make_db(tmp_path, hours=2)
        add_sys_baseline(store, hours=2)
        diag = Engine(_cfg(0.0)).run(store, only=["THERMAL-01", "DISK-JOURNAL-01"])
        store.close()
        assert sorted(diag.evaluated_rules) == ["DISK-JOURNAL-01", "THERMAL-01"]
        assert diag.uncovered_rules == []

    def test_optional_sampler_with_rows_elsewhere_is_a_gap(self, tmp_path):
        store = make_db(tmp_path, hours=2)
        add_sys_baseline(store, hours=2)
        store.buffer("thermal_samples", [(time.time() - 30 * _HOUR, 50.0, 1.0, 2.0)])
        store.flush()
        diag = Engine(_cfg(0.0)).run(store, only=["THERMAL-01"])
        store.close()
        assert [r.rule_id for r in diag.uncovered_rules] == ["THERMAL-01"]

    def test_outputs_list_uncovered_rules(self, tmp_path):
        with Storage(_rollup_only_db(tmp_path), read_only=True) as store:
            diag = Engine(_cfg()).run(store)
        text = render_terminal(diag, color=False)
        assert "記録データが無く未評価" in text
        assert "CPU-SATURATION-01 (期間内に sys_samples の記録がありません)" in text
        data = json.loads(render_json(diag))
        listed = {r["rule_id"]: r for r in data["uncovered_rules"]}
        assert listed["SWAP-THRASH-01"] == {
            "rule_id": "SWAP-THRASH-01",
            "title": "スワップスラッシング",
            "reason": "not_recorded",
            "missing": ["sys_samples"],
        }


class TestCheckExitCodes:
    def test_uncovered_rules_do_not_change_the_exit_code(self, tmp_path):
        db = _rollup_only_db(tmp_path)
        result = CliRunner().invoke(cli, ["check", "--db", str(db), "--format", "json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert _RAW_RULES <= {r["rule_id"] for r in data["uncovered_rules"]}

    def test_old_schema_db_stays_healthy(self, tmp_path):
        result = CliRunner().invoke(cli, ["check", "--db", str(_v2_db(tmp_path))])
        assert result.exit_code == 0, result.output
        assert "DB の版が古く thermal_samples がありません" in result.output


class TestExplicitWindow:
    def test_window_is_used_verbatim(self, tmp_path):
        store = make_db(tmp_path, hours=24)
        add_sys_baseline(store, hours=24)
        now = time.time()
        window = Window(now - 20 * _HOUR, now - 10 * _HOUR)
        diag = Engine(_cfg()).run(store, window=window)
        store.close()
        assert diag.quality.span_hours == 10.0
        assert diag.no_data is False

    def test_window_without_system_samples_is_no_data(self, tmp_path):
        store = make_db(tmp_path, hours=2)
        add_sys_baseline(store, hours=2)
        now = time.time()
        diag = Engine(_cfg()).run(
            store, window=Window(now - 50 * _HOUR, now - 40 * _HOUR)
        )
        store.close()
        assert diag.no_data is True

    def test_a_sample_on_the_window_end_counts_as_data(self, tmp_path):
        store = make_db(tmp_path, hours=2)
        start = time.time() - 2 * _HOUR
        add_sys_baseline(store, hours=2, start_ts=start)
        diag = Engine(_cfg()).run(store, window=Window(start - 600.0, start))
        store.close()
        assert diag.no_data is False

    def test_gap_and_throttle_counts_cover_only_the_window(self, tmp_path):
        store = make_db(tmp_path, hours=24)
        add_sys_baseline(store, hours=24)
        now = time.time()
        for ts, kind, detail in [
            (now - 22 * _HOUR, "gap", "10.0s ジョブt1_system"),
            (now - 15 * _HOUR, "gap", "2.5s ジョブt1_system"),
            (now - 22 * _HOUR, "throttle_down", ""),
            (now - 15 * _HOUR, "throttle_down", ""),
        ]:
            store.conn.execute(
                "INSERT INTO ops_events (ts, kind, detail) VALUES (?, ?, ?)",
                (ts, kind, detail),
            )
        store.conn.commit()
        diag = Engine(_cfg()).run(store, window=Window(now - 20 * _HOUR, now))
        store.close()
        assert diag.quality.gap_seconds == 2.5
        assert diag.quality.throttle_events == 1

    def test_required_hours_is_the_longest_configured_minimum(self):
        engine = Engine(_cfg(6.0))
        assert engine.required_hours() == 24.0
        assert engine.required_hours(skip=["MEM-PRESSURE-01"]) == 6.0
        assert engine.required_hours(only=["SWAP-THRASH-01"]) == 0.0
