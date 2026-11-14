from __future__ import annotations

import json
import math
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


def _rows_section(stdout: str) -> str:
    return stdout.split("結果:")[0]


def _intermittent_faults(directory, *, hours: float, start: float, at: float):
    """A recording with 3 service restarts and 2 disk I/O errors in the day before *at*."""
    store = make_db(directory, hours=hours)
    add_sys_baseline(store, hours=hours, start_ts=start)
    store.buffer(
        "service_events",
        [
            (at - h * _HOUR, "plc-gateway.service", "restart", 1, "exit-code")
            for h in (22, 14, 6)
            if at - h * _HOUR >= start
        ],
    )
    store.buffer(
        "kernel_events",
        [
            (at - h * _HOUR, "io_error", "blk_update_request: I/O error, dev sda")
            for h in (20, 8)
            if at - h * _HOUR >= start
        ],
    )
    store.flush(reason="synth")
    path = store.path
    store.close()
    return path


class TestShortAfterPeriod:
    @pytest.fixture
    def one_db(self, tmp_path):
        now = time.time() // 60 * 60
        at = now - 1 * _HOUR
        return (
            _intermittent_faults(tmp_path, hours=25, start=at - 24 * _HOUR, at=at),
            at,
        )

    @pytest.mark.parametrize("mode", ["--at", "--before/--after"])
    def test_no_fault_in_one_hour_is_not_resolved_after_a_day_with_three(
        self, one_db, mode
    ):
        db, at = one_db
        split = {
            "--at": ["--at", _iso(at)],
            "--before/--after": [
                "--before",
                f"{_iso(at - 24 * _HOUR)}..{_iso(at)}",
                "--after",
                f"{_iso(at)}..",
            ],
        }[mode]
        result = _run("--db", db, *split)
        assert result.exit_code == 3, result.output
        rows = _rows_section(result.stdout)
        assert "解消" not in rows
        assert "？ 判定不能" in rows and "[SERVICE-RESTART-01]" in rows
        assert "修正後の期間 1.0 時間は修正前の 24.0 時間より短い" in rows
        data = json.loads(_run("--db", db, *split, "--format", "json").stdout)
        verdicts = {r["rule_id"]: (r["verdict"], r["why"]) for r in data["rows"]}
        assert verdicts == {
            "SERVICE-RESTART-01": ("not_evaluable", "too_short"),
            "KERNEL-ERROR-01": ("not_evaluable", "too_short"),
        }

    def test_two_dbs_with_a_one_hour_after_db_are_not_resolved(self, tmp_path):
        now = time.time() // 60 * 60
        at = now - 1 * _HOUR
        before = _intermittent_faults(
            tmp_path / "a", hours=24, start=at - 24 * _HOUR, at=at
        )
        after_store = make_db(tmp_path / "b", hours=1)
        add_sys_baseline(after_store, hours=1, start_ts=at)
        after = after_store.path
        after_store.close()
        result = _run(before, after)
        assert result.exit_code == 3, result.output
        assert "解消" not in _rows_section(result.stdout)

    def test_a_fault_free_after_period_as_long_as_before_resolves(self, tmp_path):
        now = time.time() // 60 * 60
        at = now - 24 * _HOUR
        db = _intermittent_faults(tmp_path, hours=48, start=at - 24 * _HOUR, at=at)
        result = _run("--db", db, "--at", _iso(at))
        assert result.exit_code == 0, result.output
        assert "✔ 解消  [SERVICE-RESTART-01]" in result.stdout
        assert "修正前に検出された問題はすべて解消しました" in result.stdout

    def test_a_fault_after_a_shorter_before_period_is_not_called_new(self, tmp_path):
        now = time.time() // 60 * 60
        at = now - 24 * _HOUR
        before_store = make_db(tmp_path / "a", hours=2)
        add_sys_baseline(before_store, hours=2, start_ts=at - 2 * _HOUR)
        before = before_store.path
        before_store.close()
        after = _intermittent_faults(tmp_path / "b", hours=24, start=at, at=now)
        result = _run(before, after)
        assert result.exit_code == 1, result.output
        rows = _rows_section(result.stdout)
        assert "新規" not in rows
        assert "修正前の期間 2.0 時間は修正後の 24.0 時間より短い" in rows


def _python3_procs(directory, procs):
    """One DB, 48 h; *procs* maps a cmdline to (slope before, slope after) in MB/h.

    A ``None`` slope means the process is not running in that half.
    """
    now = time.time() // 60 * 60
    at = now - 24 * _HOUR
    start = at - 24 * _HOUR
    store = make_db(directory, hours=48)
    add_sys_baseline(store, hours=48, start_ts=start)
    rows = []
    for cmdline, slopes in procs.items():
        pid = store.proc_id_for(
            name="python3", cmdline_hash=cmdline, cmdline_sample=cmdline, ts=start
        )
        for i in range(48 * 60):
            ts = start + i * 60
            slope, t0 = (slopes[0], start) if ts < at else (slopes[1], at)
            if slope is not None:
                rss = int((200 + slope * (ts - t0) / _HOUR) * _MB)
                rows.append((ts, pid, 5.0, 6.0, rss, rss, 30, 8))
    store.buffer("proc_rollup_1m", rows)
    store.flush(reason="synth")
    path = store.path
    store.close()
    return path, at


class TestNameOnlyPairing:
    def test_a_stranger_with_the_same_name_does_not_resolve_a_gone_leaker(
        self, tmp_path
    ):
        db, at = _python3_procs(
            tmp_path,
            {
                "python3 /opt/collector.py": (20.0, None),
                "python3 /opt/report_tool.py": (None, 0.0),
            },
        )
        args = ["--db", db, "--at", _iso(at), "--only", "MEM-LEAK-01"]
        result = _run(*args)
        assert result.exit_code == 3, result.output
        rows = _rows_section(result.stdout)
        assert "解消" not in rows
        assert "？ 判定不能" in rows
        assert "RSS 増加率 20.0 → 0.0 MB/h" in rows
        assert "名前だけで突き合わせ" in rows
        (row,) = json.loads(_run(*args, "--format", "json").stdout)["rows"]
        assert (row["verdict"], row["why"]) == ("not_evaluable", "paired_by_name")
        assert row["identity"] == "python3 /opt/collector.py"
        assert row["after"]["state"] == "clear"

    def test_the_same_command_line_still_resolves(self, tmp_path):
        db, at = _python3_procs(tmp_path, {"python3 /opt/collector.py": (20.0, 0.0)})
        result = _run("--db", db, "--at", _iso(at), "--only", "MEM-LEAK-01")
        assert result.exit_code == 0, result.output
        assert "✔ 解消" in result.stdout and "名前だけ" not in result.stdout


def _noisy_leak(directory, *, after_slope: float, after_wobble: float):
    """MEM-LEAK-01 fires before (10 MB/h, R² ~1); after, a 2 h wave keeps R² < 0.9."""
    now = time.time() // 60 * 60
    at = now - 24 * _HOUR
    start = at - 24 * _HOUR
    store = make_db(directory, hours=48)
    add_sys_baseline(store, hours=48, start_ts=start)
    pid = store.proc_id_for(
        name="myapp", cmdline_hash="h1", cmdline_sample="/opt/myapp", ts=start
    )
    rows = []
    for i in range(48 * 60):
        ts = start + i * 60
        slope, wobble = (10.0, 10.0) if ts < at else (after_slope, after_wobble)
        t_h = (ts - (start if ts < at else at)) / _HOUR
        rss = int((300 + slope * t_h + wobble * math.sin(math.pi * t_h)) * _MB)
        rows.append((ts, pid, 5.0, 6.0, rss, rss, 50, 8))
    store.buffer("proc_rollup_1m", rows)
    store.flush(reason="synth")
    path = store.path
    store.close()
    return path, at


class TestRowsAgreeWithTheResult:
    @pytest.mark.parametrize(
        "after_slope, wobble, row_text",
        [
            (10.0, 45.0, "＝ 変化なし"),
            (14.0, 60.0, "↗ 悪化"),
            (6.0, 30.0, "↘ 改善 (しきい値以上)"),
        ],
    )
    def test_a_leak_still_far_above_the_threshold_remains_a_problem(
        self, tmp_path, after_slope, wobble, row_text
    ):
        db, at = _noisy_leak(tmp_path, after_slope=after_slope, after_wobble=wobble)
        args = ["--db", db, "--at", _iso(at), "--only", "MEM-LEAK-01"]
        result = _run(*args)
        assert result.exit_code == 1, result.output
        assert row_text in result.stdout
        assert "しきい値未満" not in result.stdout
        assert "修正後もまだ問題があります" in result.stdout.split("結果:")[1]
        (row,) = json.loads(_run(*args, "--format", "json").stdout)["rows"]
        assert row["after"]["state"] == "clear"
        assert row["after"]["evidence"]["r2"] < 0.9
        assert row["after"]["value"] > 5 * row["headline"]["threshold"]


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
