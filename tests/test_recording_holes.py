from __future__ import annotations

import math
import time
from datetime import datetime

import pytest
from click.testing import CliRunner
from synth import add_sys_baseline, make_db

from procdiag.cli import cli
from procdiag.config import ProcdiagConfig
from procdiag.diagnosis import Engine

_MB = 1024 * 1024
_HOUR = 3600.0
_LEAK_RULES = ["MEM-LEAK-01", "FD-LEAK-01", "THREAD-LEAK-01"]


def _local(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _register(store, name: str, ts: float) -> int:
    proc_id = store.proc_id_for(
        name=name, cmdline_hash=f"hash_{name}", cmdline_sample=f"/opt/{name}", ts=ts
    )
    store.conn.commit()
    return proc_id


def _record(
    store,
    proc_id: int,
    start: float,
    minutes: int,
    *,
    rss_mb: float,
    slope_mb_h: float = 0.0,
    fds: int = 48,
    threads: int = 24,
) -> None:
    """*minutes* of system and process rows from *start*, RSS wobbling by 1 MiB."""
    add_sys_baseline(store, hours=minutes / 60, start_ts=start)
    rows = []
    for m in range(minutes):
        rss = (rss_mb + slope_mb_h * m / 60 + math.sin(m / 7)) * _MB
        rows.append(
            (start + m * 60, proc_id, 10.0, 11.0, int(rss), int(rss), fds, threads)
        )
    store.buffer("proc_rollup_1m", rows)
    store.flush(reason="synth")


def _minute_now() -> float:
    return time.time() // 60 * 60


class TestRecordingHoles:
    def test_two_one_hour_recordings_two_days_apart_are_two_hours_observed(
        self, tmp_path
    ):
        now = _minute_now()
        first, second = now - 49 * _HOUR, now - _HOUR
        store = make_db(tmp_path, hours=49)
        proc_id = _register(store, "plc-gateway", first)
        _record(store, proc_id, first, 60, rss_mb=200)
        _record(store, proc_id, second, 60, rss_mb=260)
        diag = Engine(ProcdiagConfig()).run(store)
        store.close()

        skipped = {r.rule_id: r for r in diag.skipped_rules}
        assert "MEM-LEAK-01" in skipped
        assert "MEM-LEAK-01" not in diag.evaluated_rules
        assert skipped["MEM-PRESSURE-01"].remaining_hours == pytest.approx(
            24 - 118 / 60, abs=0.01
        )
        assert not diag.findings
        assert diag.quality.span_hours == pytest.approx(118 / 60, abs=0.01)
        assert diag.quality.gap_seconds == pytest.approx(second - first - 59 * 60)
        hole = f"{_local(first + 59 * 60)} 〜 {_local(second)}"
        assert any(hole in note for note in diag.quality.notes), diag.quality.notes

    def test_a_level_change_across_an_overnight_hole_is_not_a_leak(self, tmp_path):
        now = _minute_now()
        day1, day2 = now - 33 * _HOUR, now - 9 * _HOUR
        store = make_db(tmp_path, hours=33)
        proc_id = _register(store, "plc-gateway", day1)
        _record(store, proc_id, day1, 9 * 60, rss_mb=200, fds=48, threads=24)
        _record(store, proc_id, day2, 9 * 60, rss_mb=230, fds=64, threads=40)
        diag = Engine(ProcdiagConfig()).run(store, only=_LEAK_RULES)
        store.close()

        assert sorted(diag.evaluated_rules) == sorted(_LEAK_RULES)
        assert diag.findings == []

    def test_a_leak_that_restarts_each_morning_is_found_inside_the_days(self, tmp_path):
        now = _minute_now()
        day1, day2 = now - 33 * _HOUR, now - 9 * _HOUR
        store = make_db(tmp_path, hours=33)
        proc_id = _register(store, "plc-gateway", day1)
        _record(store, proc_id, day1, 9 * 60, rss_mb=200, slope_mb_h=20)
        _record(store, proc_id, day2, 9 * 60, rss_mb=200, slope_mb_h=20)
        diag = Engine(ProcdiagConfig()).run(store, only=["MEM-LEAK-01"])
        store.close()

        (leak,) = diag.findings
        assert leak.evidence["slope_mb_per_h"] == pytest.approx(20, rel=0.02)
        assert leak.evidence["span_hours"] == pytest.approx(2 * 539 / 60)
        assert "18.0h観測" in leak.summary

    def test_a_suspend_inside_a_recording_counts_once_in_the_gap_total(self, tmp_path):
        now = _minute_now()
        start, resume = now - 8 * _HOUR, now - 4 * _HOUR
        store = make_db(tmp_path, hours=8)
        proc_id = _register(store, "plc-gateway", start)
        _record(store, proc_id, start, 120, rss_mb=200)
        _record(store, proc_id, resume, 240, rss_mb=200)
        suspended = resume - (start + 119 * 60)
        store.conn.execute(
            "INSERT INTO ops_events (ts, kind, detail) VALUES (?, 'gap', ?)",
            (resume - 0.5, f"{suspended - 1:.1f}s ジョブt1_system"),
        )
        store.conn.execute(
            "INSERT INTO ops_events (ts, kind, detail) VALUES (?, 'gap', ?)",
            (now - _HOUR, "4.0s ジョブt1_system"),
        )
        store.conn.commit()
        diag = Engine(ProcdiagConfig()).run(store)
        store.close()

        assert diag.quality.gap_seconds == pytest.approx(suspended + 4.0)
        assert diag.quality.span_hours == pytest.approx(358 / 60)

    def test_compare_does_not_resolve_on_two_hours_spread_over_a_day(self, tmp_path):
        now = _minute_now()
        at = now - 25 * _HOUR
        before = make_db(tmp_path / "a", hours=24)
        add_sys_baseline(before, hours=24, start_ts=at - 24 * _HOUR)
        before.buffer(
            "service_events",
            [
                (at - h * _HOUR, "plc-gateway.service", "restart", 1, "exit-code")
                for h in (22, 14, 6)
            ],
        )
        before.flush(reason="synth")
        before.close()
        after = make_db(tmp_path / "b", hours=25)
        add_sys_baseline(after, hours=1, start_ts=at)
        add_sys_baseline(after, hours=1, start_ts=now - _HOUR)
        after.close()

        result = CliRunner().invoke(
            cli, ["compare", str(before.path), str(after.path), "--no-color"]
        )
        assert result.exit_code == 3, result.output
        rows = result.stdout.split("結果:")[0]
        assert "解消" not in rows
        assert "修正後の期間 2.0 時間は修正前の 24.0 時間より短い" in rows


class TestClockSteppedBack:
    def test_rows_stamped_after_now_are_left_out_and_the_leak_is_found(self, tmp_path):
        now = _minute_now()
        boot = now - 7 * _HOUR
        store = make_db(tmp_path, hours=7)
        proc_id = _register(store, "app", boot)
        _record(store, proc_id, boot + 9 * _HOUR, 3, rss_mb=50)
        _record(store, proc_id, boot + 180, 7 * 60 - 3, rss_mb=60, slope_mb_h=30)
        store.conn.execute(
            "INSERT INTO ops_events (ts, kind, detail) VALUES (?, 'clock_skew', ?)",
            (boot + 240, f"rollup watermark {boot + 9 * _HOUR + 120:.0f} > 現在分"),
        )
        store.conn.commit()
        diag = Engine(ProcdiagConfig()).run(store, only=["MEM-LEAK-01"])
        store.close()

        assert diag.window.end_ts <= time.time()
        (leak,) = diag.findings
        assert leak.evidence["slope_mb_per_h"] == pytest.approx(30, rel=0.02)
        notes = "\n".join(diag.quality.notes)
        assert _local(boot + 9 * _HOUR) in notes
        assert _local(boot + 240) in notes

    def test_the_service_starts_after_the_clock_is_synchronized(self):
        result = CliRunner().invoke(cli, ["service", "print"])
        assert result.exit_code == 0, result.output
        unit = result.output.split("[Service]")[0]
        assert "After=local-fs.target time-sync.target" in unit
        assert "Wants=local-fs.target\n" in unit
