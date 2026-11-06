from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner
from synth import add_proc_series, add_sys_baseline, make_db

from procdiag.cli import cli

_MB = 1024 * 1024
_HOUR = 3600.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recording(
    directory,
    *,
    before_h: float,
    after_h: float,
    before_slope: float = 38.0,
    after_slope: float = 0.2,
    hostname: str = "synth-host",
):
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    at = now - after_h * _HOUR
    start = at - before_h * _HOUR
    store = make_db(directory, hours=before_h + after_h)
    store.set_meta("hostname", hostname)
    add_sys_baseline(store, hours=before_h + after_h, start_ts=start)
    for rss, slope, hours, t0 in (
        (200, before_slope, before_h, start),
        (300, after_slope, after_h, at),
    ):
        if hours:
            add_proc_series(
                store,
                "myapp",
                rss_start=rss * _MB,
                rss_slope_per_h=slope * _MB,
                hours=hours,
                start_ts=t0,
            )
    store.conn.execute("DELETE FROM sys_samples WHERE ts < ?", (now - 48 * _HOUR,))
    store.conn.commit()
    path = store.path
    store.close()
    return path, at


def _run(*args):
    return CliRunner().invoke(cli, ["compare", "--no-color", *map(str, args)])


@pytest.fixture(scope="module")
def story(tmp_path_factory):
    return _recording(tmp_path_factory.mktemp("story"), before_h=72, after_h=72)


class TestCanonicalStory:
    def test_split_at_the_fix_resolves_with_both_slopes(self, story):
        db, at = story
        result = _run("--db", db, "--at", _iso(at))
        assert result.exit_code == 0, result.output
        assert "✔ 解消" in result.stdout
        assert "[MEM-LEAK-01]" in result.stdout
        assert "RSS 増加率 38.0 → 0.2 MB/h" in result.stdout
        assert "修正前に検出された問題はすべて解消しました" in result.stdout

    def test_json_headline_row(self, story):
        db, at = story
        result = _run("--db", db, "--at", _iso(at), "--format", "json")
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["format"] == "procdiag-compare/1"
        assert set(data) == {
            "format",
            "verdict_counts",
            "rows",
            "before",
            "after",
            "notes",
            "generated_at",
            "procdiag_version",
        }
        (row,) = data["rows"]
        assert row["rule_id"] == "MEM-LEAK-01" and row["verdict"] == "resolved"
        assert row["before"]["state"] == "detected"
        assert row["before"]["value"] == pytest.approx(38.0, rel=0.02)
        assert row["after"]["state"] == "clear"
        assert row["after"]["value"] == pytest.approx(0.2, abs=0.05)
        assert row["headline"] == {
            "label": "RSS 増加率",
            "unit": "MB/h",
            "threshold": 1.0,
        }
        assert data["before"]["raw_hours"] == 0.0
        assert data["before"]["window"]["start"] < data["after"]["window"]["start"]

    def test_raw_rules_of_a_pruned_before_period_are_listed_unevaluated(self, story):
        db, at = story
        data = json.loads(_run("--db", db, "--at", _iso(at), "--format", "json").stdout)
        uncovered = {r["rule_id"] for r in data["before"]["uncovered_rules"]}
        assert {"CPU-SATURATION-01", "SWAP-THRASH-01"} <= uncovered
        assert "CPU-SATURATION-01" in data["after"]["evaluated_rules"]

    def test_explicit_periods_give_the_same_verdict(self, story):
        db, at = story
        result = _run(
            "--db", db, "--before", f"..{_iso(at)}", "--after", f"{_iso(at)}.."
        )
        assert result.exit_code == 0
        assert "RSS 増加率 38.0 → 0.2 MB/h" in result.stdout


class TestTwoDbs:
    def test_two_recordings_compare_whole_and_note_the_hosts(self, tmp_path):
        before, _ = _recording(
            tmp_path / "a", before_h=24, after_h=0, hostname="line-pc01"
        )
        after, _ = _recording(
            tmp_path / "b",
            before_h=0,
            after_h=24,
            after_slope=0.1,
            hostname="line-pc02",
        )
        result = _run(before, after)
        assert result.exit_code == 0, result.output
        assert (
            "注意: 記録ホストが異なります (修正前: line-pc01 / 修正後: line-pc02)"
            in (result.stdout)
        )
        assert f"[{before}]" in result.stdout and f"[{after}]" in result.stdout
        assert "✔ 解消" in result.stdout

    def test_the_same_file_twice_is_one_db_with_overlapping_periods(self, story):
        db, _ = story
        result = _run(db, db)
        assert result.exit_code == 4
        assert "重なっています" in result.stderr


class TestNotEvaluable:
    def test_too_short_after_period_is_not_evaluable_and_exits_3(self, tmp_path):
        db, at = _recording(tmp_path, before_h=24, after_h=3)
        result = _run("--db", db, "--at", _iso(at))
        assert result.exit_code == 3, result.output
        assert "？ 判定不能" in result.stdout
        assert "修正後: 記録が 3.0 時間のため未評価 (6.0 時間必要、あと 3.0 時間)" in (
            result.stdout
        )
        assert "解消" not in result.stdout.split("結果:")[0]

    def test_after_period_without_records_exits_3(self, story):
        db, _ = story
        result = _run("--db", db, "--at", _iso(time.time() + 3600))
        assert result.exit_code == 3
        assert "修正後: 期間内に記録がありません" in result.stdout


class TestUsageErrors:
    def test_overlapping_periods_exit_4(self, story):
        db, at = story
        result = _run(
            "--db",
            db,
            "--before",
            f"..{_iso(at + 3600)}",
            "--after",
            f"{_iso(at)}..",
        )
        assert result.exit_code == 4
        assert "重なっています" in result.stderr

    def test_newer_schema_db_exits_4_without_traceback(self, tmp_path):
        db, at = _recording(tmp_path, before_h=12, after_h=12)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        result = _run("--db", db, "--at", _iso(at))
        assert result.exit_code == 4
        assert isinstance(result.exception, SystemExit)
        assert str(db) in result.stderr and "スキーマ版 99" in result.stderr
        assert "Traceback" not in result.output

    @pytest.mark.parametrize(
        "args, message",
        [
            (["only-one.db"], "DB は 2 つ"),
            (["--at", "2026-10-28", "a.db", "b.db"], "--at"),
            (["--at", "not-a-time", "--db", "x.db"], "時刻の形式"),
            (["--db", "x.db", "a.db", "b.db"], "同時に指定できません"),
        ],
    )
    def test_bad_arguments_exit_4(self, args, message):
        result = _run(*args)
        assert result.exit_code == 4
        assert message in result.stderr

    def test_missing_db_exits_4(self, tmp_path):
        result = _run("--db", tmp_path / "nope.db", "--at", "2026-10-28")
        assert result.exit_code == 4
        assert "DB ファイルが見つかりません" in result.stderr
