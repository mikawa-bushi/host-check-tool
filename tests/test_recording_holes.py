from __future__ import annotations

import json
import math
import time
from datetime import datetime

import pytest
from click.testing import CliRunner
from synth import add_sys_baseline, make_db

from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, SamplingConfig
from procdiag.core import Confidence, Window
from procdiag.diagnosis import Engine
from procdiag.storage import make_row

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


def _sys_rows(store, start: float, end: float, step: float) -> None:
    """System rows every *step* seconds in [start, end), with their rollups."""
    stamps = [start + k * step for k in range(int((end - start) / step))]
    store.buffer(
        "sys_samples",
        [
            make_row(
                "sys_samples",
                ts=ts,
                cpu_total=5.0,
                cpu_max_core=7.0,
                mem_used=2 << 30,
                mem_available=6 << 30,
                swap_used=0,
                swap_in=0,
                swap_out=0,
                load1=0.1,
                psi_cpu_some10=0.0,
                psi_mem_some10=0.0,
                psi_mem_full10=0.0,
                psi_io_some10=0.0,
                psi_io_full10=0.0,
                disk_read_bytes=0,
                disk_write_bytes=0,
                self_cpu=0.1,
                self_rss=40 * _MB,
            )
            for ts in stamps
        ],
    )
    minutes = sorted({ts // 60 * 60 for ts in stamps})
    store.buffer(
        "sys_rollup_1m",
        [(m, 5.0, 7.0, 5.0, 7.0, 2 << 30, 2 << 30, 0.0, 0.0, 0.0, 0) for m in minutes],
    )
    store.flush(reason="synth")


def _proc_samples(store, proc_id: int, stamps, *, pss_mb=None) -> None:
    store.buffer(
        "proc_samples",
        [
            make_row(
                "proc_samples",
                ts=ts,
                proc_id=proc_id,
                pid=4242,
                cpu_pct=1.0,
                rss=200 * _MB,
                pss=None if pss_mb is None else int(pss_mb * _MB),
                num_threads=24,
                num_fds=48,
                io_read_delta=0,
                io_write_delta=0,
            )
            for ts in stamps
        ],
    )
    store.flush(reason="synth")


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
        header = result.stdout.split("時刻はこのホストのローカルタイム")[0]
        assert "  2.0 時間 (うち生データ 2.0 時間)" in header
        assert f"{_local(at + 59 * 60)} 〜 {_local(now - _HOUR)}" in header
        data = json.loads(
            CliRunner()
            .invoke(
                cli, ["compare", str(before.path), str(after.path), "--format", "json"]
            )
            .stdout
        )
        assert data["after"]["span_hours"] == pytest.approx(118 / 60)
        assert data["after"]["raw_hours"] == pytest.approx(118 / 60)

    def test_a_recording_sampled_every_six_minutes_has_no_holes(self, tmp_path):
        now = _minute_now()
        start = now - 12 * _HOUR
        store = make_db(tmp_path, hours=12)
        config = ProcdiagConfig(sampling=SamplingConfig(system_interval=360))
        store.set_meta("config", config.model_dump_json())
        _sys_rows(store, start, now, 360.0)
        proc_id = _register(store, "plc-gateway", start)
        store.buffer(
            "proc_rollup_1m",
            [
                (ts, proc_id, 1.0, 1.0, int((200 + 20 * h) * _MB), 0, 48, 24)
                for h in [k * 0.1 for k in range(120)]
                for ts in [start + h * _HOUR]
            ],
        )
        store.flush(reason="synth")
        diag = Engine(ProcdiagConfig()).run(store)
        store.close()

        assert diag.quality.span_hours == pytest.approx(12 - 0.1)
        assert diag.quality.gap_seconds == 0
        assert "MEM-LEAK-01" in diag.evaluated_rules
        assert [f.rule_id for f in diag.findings] == ["MEM-LEAK-01"]

    def test_a_leak_in_many_short_shifts_is_judged_like_the_gate(self, tmp_path):
        now = _minute_now()
        store = make_db(tmp_path, hours=36)
        shifts = [now - (12 - i) * 3 * _HOUR for i in range(12)]
        proc_id = _register(store, "plc-gateway", shifts[0])
        for start in shifts:
            _sys_rows(store, start, start + 1800, 5.0)
            store.buffer(
                "proc_rollup_1m",
                [
                    (start + m * 60, proc_id, 1.0, 1.0, int((200 + m) * _MB), 0, 48, 24)
                    for m in range(30)
                ],
            )
            store.flush(reason="synth")
        diag = Engine(ProcdiagConfig()).run(store, only=["MEM-LEAK-01"])
        store.close()

        assert diag.evaluated_rules == ["MEM-LEAK-01"]
        (leak,) = diag.findings
        assert leak.evidence["slope_mb_per_h"] == pytest.approx(60, rel=0.01)

    def test_zombies_left_over_on_another_day_are_not_a_trend(self, tmp_path):
        now = _minute_now()
        day1, day2 = now - 33 * _HOUR, now - 9 * _HOUR
        store = make_db(tmp_path, hours=33)
        proc_id = _register(store, "plc-gateway", day1)
        for start, zombies in ((day1, 2), (day2, 20)):
            _sys_rows(store, start, start + 9 * _HOUR, 60.0)
            stamps = [start + k * 600 for k in range(54)]
            _proc_samples(store, proc_id, stamps)
            store.buffer("proc_counts", [(ts, 300, zombies) for ts in stamps])
            store.flush(reason="synth")
        diag = Engine(ProcdiagConfig()).run(store, only=["ZOMBIE-01"])
        store.close()

        assert diag.evaluated_rules == ["ZOMBIE-01"]
        assert diag.findings == []

    def test_pss_stepping_across_a_hole_does_not_confirm_a_leak(self, tmp_path):
        now = _minute_now()
        day1, day2 = now - 33 * _HOUR, now - 9 * _HOUR
        store = make_db(tmp_path, hours=33)
        proc_id = _register(store, "plc-gateway", day1)
        for start, pss_mb in ((day1, 100), (day2, 140)):
            _record(store, proc_id, start, 9 * 60, rss_mb=200, slope_mb_h=20)
            _proc_samples(
                store, proc_id, [start + k * 600 for k in range(54)], pss_mb=pss_mb
            )
        diag = Engine(ProcdiagConfig()).run(store, only=["MEM-LEAK-01"])
        store.close()

        (leak,) = diag.findings
        assert leak.confidence is Confidence.LOW
        assert "pss_note" in leak.evidence

    def test_an_explicit_window_wider_than_the_records_has_holes_at_its_ends(
        self, tmp_path
    ):
        now = _minute_now()
        start = now - 8 * _HOUR
        store = make_db(tmp_path, hours=8)
        proc_id = _register(store, "plc-gateway", start)
        _record(store, proc_id, start, 8 * 60, rss_mb=200)
        last = start + (8 * 60 - 1) * 60
        diag = Engine(ProcdiagConfig()).run(
            store, window=Window(start - 2 * _HOUR, last + _HOUR)
        )
        store.close()

        assert diag.quality.span_hours == pytest.approx((last - start) / _HOUR)
        assert diag.quality.gap_seconds == pytest.approx(3 * _HOUR)


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

    def test_a_chosen_period_leaves_out_rows_stamped_after_now(self, tmp_path):
        now = _minute_now()
        boot = now - 7 * _HOUR
        store = make_db(tmp_path, hours=7)
        proc_id = _register(store, "app", boot)
        _record(store, proc_id, boot + 9 * _HOUR, 3, rss_mb=50)
        _record(store, proc_id, boot + 180, 7 * 60 - 3, rss_mb=60, slope_mb_h=30)
        store.close()
        since = datetime.fromtimestamp(boot - _HOUR).strftime("%Y-%m-%dT%H:%M")

        result = CliRunner().invoke(
            cli,
            ["check", "--db", str(store.path), "--since", since, "--format", "json"]
            + ["--only", "MEM-LEAK-01"],
        )
        data = json.loads(result.stdout)
        assert datetime.fromisoformat(data["window"]["end"]).timestamp() < now
        assert [f["rule_id"] for f in data["findings"]] == ["MEM-LEAK-01"]

    def test_a_system_unit_starts_after_the_clock_is_synchronized(self):
        result = CliRunner().invoke(cli, ["service", "print", "--system"])
        assert result.exit_code == 0, result.output
        unit = result.output.split("[Service]")[0]
        assert "After=local-fs.target time-sync.target\n" in unit
        assert "Wants=local-fs.target\n" in unit

    def test_a_user_unit_names_no_system_target(self):
        result = CliRunner().invoke(cli, ["service", "print"])
        assert result.exit_code == 0, result.output
        unit = result.output.split("[Service]")[0]
        assert "After=local-fs.target\n" in unit
        assert "time-sync" not in unit
