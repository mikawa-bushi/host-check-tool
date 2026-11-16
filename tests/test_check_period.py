from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner
from synth import add_proc_series, add_sys_baseline, make_db

from procdiag.cli import cli

_HOUR = 3600.0
_MB = 1024 * 1024
_LEAK_RULES = {"MEM-LEAK-01", "FD-LEAK-01", "THREAD-LEAK-01", "ZOMBIE-01"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


@pytest.fixture()
def recording(tmp_path):
    start = time.time() - 30 * _HOUR
    store = make_db(tmp_path, hours=30)
    add_sys_baseline(store, hours=30, start_ts=start)
    store.conn.execute("DELETE FROM sys_samples WHERE ts < ?", (start + 28 * _HOUR,))
    store.conn.commit()
    store.close()
    return store.path, start


def _check(db, *args):
    result = CliRunner().invoke(cli, ["check", "--db", str(db), *args])
    assert result.exit_code in (0, 1, 2), result.output
    return result


def _check_json(db, *args):
    return json.loads(_check(db, "--format", "json", *args).stdout)


class TestDefaultPeriod:
    def test_diagnoses_the_raw_hours_and_points_at_older_records(self, recording):
        db, start = recording
        report = _check_json(db)
        span = _ts(report["window"]["end"]) - _ts(report["window"]["start"])
        assert span == pytest.approx(2 * _HOUR, abs=120)
        older = [n for n in report["quality"]["notes"] if "--since" in n]
        assert len(older) == 1
        assert datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M") in older[0]

    def test_text_output_shows_when_the_period_starts_and_ends(self, recording):
        db, _ = recording
        line = next(
            l for l in _check(db, "--no-color").output.splitlines() if "観測期間" in l
        )
        assert "〜" in line


class TestChosenPeriod:
    def test_since_reaches_the_rollup_and_names_the_raw_rules(self, recording):
        db, start = recording
        report = _check_json(db, "--since", _iso(start))
        span = _ts(report["window"]["end"]) - _ts(report["window"]["start"])
        assert span == pytest.approx(30 * _HOUR, abs=120)
        notes = report["quality"]["notes"]
        assert not [n for n in notes if "--since" in n]
        partial = [n for n in notes if "生データがあるのは" in n]
        assert len(partial) == 1 and "CPU-SATURATION-01" in partial[0]

    def test_until_excludes_its_own_instant(self, recording):
        db, start = recording
        until = start + 10 * _HOUR
        report = _check_json(db, "--since", _iso(start), "--until", _iso(until))
        assert _ts(report["window"]["end"]) < until

    def test_the_html_report_covers_the_same_period(self, recording, tmp_path):
        db, start = recording
        out = tmp_path / "r.html"
        result = CliRunner().invoke(
            cli, ["check", "--db", str(db), "--since", _iso(start), "-o", str(out)]
        )
        assert result.exit_code in (0, 1, 2), result.output
        assert "<dt>観測期間</dt><dd>30.0 時間" in out.read_text(encoding="utf-8")

    def test_a_period_without_records_exits_3(self, recording):
        db, _ = recording
        result = CliRunner().invoke(
            cli, ["check", "--db", str(db), "--since", _iso(time.time() + _HOUR)]
        )
        assert result.exit_code == 3, result.output
        assert "記録がありません" in result.output

    @pytest.mark.parametrize("suffix", [".json", ".html", ".txt"])
    def test_a_period_without_records_still_writes_the_report(
        self, recording, tmp_path, suffix
    ):
        db, _ = recording
        out = tmp_path / f"report{suffix}"
        out.write_text("yesterday's report")
        result = CliRunner().invoke(
            cli,
            [
                "check",
                "--db",
                str(db),
                "--since",
                _iso(time.time() + _HOUR),
                "-o",
                str(out),
            ],
        )
        assert result.exit_code == 3, result.output
        text = out.read_text(encoding="utf-8")
        assert "yesterday" not in text
        if suffix == ".json":
            report = json.loads(text)
            assert report["no_data"] is True
            assert report["healthy"] is False
            assert report["window"] is None
            assert any(
                "指定した期間に記録がありません" in n
                for n in report["quality"]["notes"]
            )
        else:
            assert "観測データがありません" in text
            assert "指定した期間に記録がありません" in text

    def test_a_period_without_records_prints_json_on_stdout(self, recording):
        db, _ = recording
        result = CliRunner().invoke(
            cli,
            [
                "check",
                "--db",
                str(db),
                "--since",
                _iso(time.time() + _HOUR),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 3, result.output
        assert json.loads(result.stdout)["no_data"] is True

    def test_an_unreadable_time_exits_4(self, recording):
        db, _ = recording
        result = CliRunner().invoke(cli, ["check", "--db", str(db), "--since", "昨日"])
        assert result.exit_code == 4, result.output


@pytest.fixture()
def leaking_recording(tmp_path):
    start = time.time() - 30 * _HOUR
    store = make_db(tmp_path, hours=30)
    add_sys_baseline(store, hours=30, start_ts=start)
    add_proc_series(
        store,
        "leaky",
        rss_start=200 * _MB,
        rss_slope_per_h=50 * _MB,
        hours=30,
        start_ts=start,
    )
    store.close()
    return store.path, start


def _skipped(report):
    return {r["rule_id"] for r in report["skipped_rules"]}


class TestPeriodLength:
    def test_a_period_as_long_as_the_leak_rules_need_evaluates_them(
        self, leaking_recording
    ):
        db, start = leaking_recording
        report = _check_json(
            db, "--since", _iso(start), "--until", _iso(start + 6 * _HOUR)
        )
        assert not _LEAK_RULES & _skipped(report)
        leaks = [
            f["subject"] for f in report["findings"] if f["rule_id"] == "MEM-LEAK-01"
        ]
        assert leaks == ["leaky"]

    def test_a_day_long_period_is_long_enough_for_memory_pressure(
        self, leaking_recording
    ):
        db, start = leaking_recording
        report = _check_json(
            db, "--since", _iso(start), "--until", _iso(start + 24 * _HOUR)
        )
        skipped = {r["rule_id"]: r for r in report["skipped_rules"]}
        assert (
            "MEM-PRESSURE-01" in report["evaluated_rules"]
            or skipped["MEM-PRESSURE-01"]["required_hours"] > 24
        )

    def test_a_period_minutes_short_still_skips_the_leak_rules(self, leaking_recording):
        db, start = leaking_recording
        until = start + 6 * _HOUR - 3 * 60
        report = _check_json(db, "--since", _iso(start), "--until", _iso(until))
        assert "MEM-LEAK-01" in _skipped(report)

    @pytest.mark.parametrize("minutes_short", [2.4, 2.9])
    def test_a_wait_of_a_few_minutes_is_never_shown_as_zero_hours(
        self, leaking_recording, minutes_short
    ):
        db, start = leaking_recording
        until = start + 6 * _HOUR - minutes_short * 60
        args = ["--since", _iso(start), "--until", _iso(until)]
        report = _check_json(db, *args)
        waits = [r["remaining_hours"] for r in report["skipped_rules"]]
        assert waits and all(w >= 0.1 for w in waits)
        screen = _check(db, *args, "--no-color").output
        assert "あと 0.0h" not in screen and "あと 0.0 時間" not in screen
        assert "MEM-LEAK-01 (あと 0.1h)" in screen


def test_an_empty_db_has_no_window(tmp_path):
    store = make_db(tmp_path, hours=1)
    store.close()
    result = CliRunner().invoke(
        cli, ["check", "--db", str(store.path), "--format", "json"]
    )
    assert result.exit_code == 3
    assert json.loads(result.stdout)["window"] is None


@pytest.fixture()
def holey_recording(tmp_path):
    """57 h from first to last row: 12 h and 15 h recorded, raw only for 21 h of it."""
    now = time.time()
    start = now - 57 * _HOUR
    store = make_db(tmp_path, hours=57)
    add_sys_baseline(store, hours=12, start_ts=start)
    add_sys_baseline(store, hours=15, start_ts=start + 42 * _HOUR)
    store.conn.execute("DELETE FROM sys_samples WHERE ts < ?", (start + 6 * _HOUR,))
    store.conn.commit()
    store.close()
    return store.path


class TestPeriodNotesCountRecordedHours:
    def test_the_raw_data_note_does_not_count_the_gap_as_raw_data(
        self, holey_recording
    ):
        report = _check_json(holey_recording, "--since", "2020-01-01")
        assert report["quality"]["span_hours"] == pytest.approx(27.0, abs=0.1)
        (note,) = [n for n in report["quality"]["notes"] if "生データがあるのは" in n]
        assert "期間 27.0 時間のうち生データがあるのは 21.0 時間です" in note
