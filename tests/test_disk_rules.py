"""Regression tests for the disk rules (DISK-FULL-01, DISK-INODE-01,
FS-READONLY-01, DISK-GROWTH-01, DISK-JOURNAL-01) and the column that keeps
the space other users can write (schema 6)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import namedtuple

import pytest
from click.testing import CliRunner
from synth import add_dir_series, add_sys_baseline, disk_row, make_db

import procdiag.samplers.disk as disk_mod
import procdiag.snapshot as snap
from procdiag import storage
from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Severity
from procdiag.core.sampler import RunContext
from procdiag.diagnosis import Engine
from procdiag.report import build_charts, render_html, render_json, render_terminal
from procdiag.samplers.disk import DiskSampler
from procdiag.storage import TABLE_COLUMNS, Storage

_GiB = 1024**3
_MiB = 1024**2
_KiB = 1024
_HOUR = 3600.0

_Part = namedtuple("_Part", "device mountpoint fstype opts")
_Usage = namedtuple("_Usage", "total used free percent")


def _recording(tmp_path, hours: float):
    """A DB with *hours* of system samples ending now; returns it and its start."""
    start = time.time() - hours * _HOUR - 120
    store = make_db(tmp_path, hours=hours)
    add_sys_baseline(store, hours=hours + 1 / 60, start_ts=start)
    return store, start


def _disk(store, rows):
    store.buffer("disk_samples", [disk_row(**row) for row in rows])
    store.flush(reason="test")


def _dirs(store, rows):
    store.buffer("dir_samples", rows)
    store.flush(reason="test")


def _run(store, *rule_ids, overrides=None):
    config = ProcdiagConfig(rules=RulesConfig(overrides=overrides or {}))
    return Engine(config).run(store, only=list(rule_ids))


def _found(diag, rule_id):
    return [f for f in diag.findings if f.rule_id == rule_id]


def _uncovered(diag):
    return {r.rule_id: r.missing for r in diag.uncovered_rules}


def _flat(start, minutes, **columns):
    return [{"ts": start + m * 60, **columns} for m in range(minutes + 1)]


def _filling(start, hours, *, gb_per_hour, first_gb=50):
    """A 100 GiB disk that fills at *gb_per_hour*, sampled every minute."""
    rows = []
    for m in range(int(hours * 60) + 1):
        used = int((first_gb + gb_per_hour * m / 60) * _GiB)
        rows.append(
            {
                "ts": start + m * 60,
                "mountpoint": "/",
                "total": 100 * _GiB,
                "used": used,
                "avail": 100 * _GiB - used,
                "read_only": 0,
            }
        )
    return rows


class TestFullDisk:
    def test_a_disk_full_for_other_users_is_critical_though_it_no_longer_grows(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 3)
        _disk(
            store,
            _flat(
                start,
                180,
                mountpoint="/",
                total=100 * _GiB,
                used=95 * _GiB,
                avail=0,
                read_only=0,
            ),
        )

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.severity is Severity.CRITICAL
        assert f.subject == "/"
        assert "100.0%" in f.summary

    def test_a_disk_filled_to_the_last_block_is_critical(self, tmp_path):
        store, start = _recording(tmp_path, 3)
        _disk(
            store,
            _flat(start, 180, mountpoint="/", total=100 * _GiB, used=100 * _GiB),
        )

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.severity is Severity.CRITICAL

    def test_a_nearly_full_disk_that_no_longer_grows_is_a_warning(self, tmp_path):
        store, start = _recording(tmp_path, 3)
        used = int(87.4 * _GiB)  # 92% of the 95 GiB other users can fill
        _disk(
            store,
            _flat(
                start,
                180,
                mountpoint="/",
                total=100 * _GiB,
                used=used,
                avail=95 * _GiB - used,
                read_only=0,
            ),
        )

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.severity is Severity.WARNING
        assert "92.0%" in f.summary

    def test_days_left_count_only_the_space_other_users_can_write(self, tmp_path):
        store, start = _recording(tmp_path, 24)
        rows = []
        for m in range(0, 24 * 60 + 1, 10):
            used = int(80 * _GiB + 2 * _GiB * m / (24 * 60))
            rows.append(
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": used,
                    "avail": 95 * _GiB - used,
                    "read_only": 0,
                }
            )
        _disk(store, rows)

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.severity is Severity.CRITICAL
        assert f.evidence["days_left"] == pytest.approx(13 / 2, rel=0.02)

    def test_a_full_read_only_mount_is_not_reported(self, tmp_path):
        store, start = _recording(tmp_path, 3)
        _disk(
            store,
            _flat(
                start,
                180,
                mountpoint="/media/cdrom",
                total=4 * _GiB,
                used=4 * _GiB,
                avail=0,
                read_only=1,
            ),
        )

        assert _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01") == []

    def test_a_full_disk_is_reported_on_a_ten_minute_recording(self, tmp_path):
        store, start = _recording(tmp_path, 10 / 60)
        _disk(
            store,
            _flat(
                start,
                10,
                mountpoint="/",
                total=100 * _GiB,
                used=95 * _GiB,
                avail=0,
                read_only=0,
            ),
        )

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.severity is Severity.CRITICAL

    def test_a_recorded_full_disk_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            disk_mod.psutil,
            "disk_partitions",
            lambda all=False: [_Part("/dev/sda1", "/", "ext4", "rw,relatime")],
        )
        monkeypatch.setattr(
            disk_mod.psutil,
            "disk_usage",
            lambda mp: _Usage(100 * _GiB, 95 * _GiB, 0, 100.0),
        )
        monkeypatch.setattr(disk_mod, "_inode_usage", lambda mp: (None, None))
        (batch,) = DiskSampler().collect(RunContext(ProcdiagConfig()))
        (row,) = batch.rows
        recorded = dict(zip(TABLE_COLUMNS["disk_samples"], row))
        store, start = _recording(tmp_path, 3)
        store.buffer(
            "disk_samples",
            [
                tuple({**recorded, "ts": start + m * 60}[c] for c in recorded)
                for m in range(181)
            ],
        )
        store.flush(reason="test")

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.severity is Severity.CRITICAL

    def test_the_report_chart_shows_the_usage_the_rule_judges(self, tmp_path):
        store, start = _recording(tmp_path, 1)
        _disk(
            store,
            _flat(
                start,
                60,
                mountpoint="/",
                total=100 * _GiB,
                used=95 * _GiB,
                avail=0,
                read_only=0,
            ),
        )

        charts = build_charts(store)

        assert {pct for _, pct in charts.mounts["/"]} == {100.0}


class TestGrowthShown:
    def test_the_growth_shown_is_the_rate_behind_days_left_after_a_cleanup(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 48)
        rows = []
        for m in range(0, 48 * 60 + 1, 10):
            used = 92 * _GiB if m < 60 else 60 * _GiB + 10 * _GiB * (m - 60) / 1440
            rows.append(
                {"ts": start + m * 60, "mountpoint": "/", "total": 100 * _GiB}
                | {"used": int(used)}
            )
        _disk(store, rows)

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")
        ev = f.evidence
        growth = ev["daily_growth_bytes"]

        assert growth > 0
        assert ev["days_left"] * growth == pytest.approx(
            ev["total_bytes"] - ev["used_bytes"], rel=1e-6
        )
        assert f"{growth / _GiB:.2f} GB/日" in f.summary

    def test_the_growth_shown_is_that_of_the_last_seven_days(self, tmp_path):
        store, start = _recording(tmp_path, 30 * 24)
        rows = []
        for h in range(30 * 24 + 1):
            grown = max(0, h - 23 * 24) * 4 * _GiB / 24
            rows.append(
                {
                    "ts": start + h * _HOUR,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": int(50 * _GiB + grown),
                }
            )
        _disk(store, rows)

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.evidence["daily_growth_bytes"] == pytest.approx(4 * _GiB, rel=0.05)


class TestFoldedGrowth:
    def test_the_growing_directories_are_named_in_every_report(self, tmp_path):
        store, start = _recording(tmp_path, 48)
        _disk(
            store,
            [
                {
                    "ts": start + h * _HOUR,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": int(70 * _GiB + 6 * _GiB * h / 24),
                }
                for h in range(49)
            ],
        )
        for path in ("/var/log/plc-app", "/opt/vision/images"):
            add_dir_series(
                store,
                path,
                start=1 * _GiB,
                daily_growth=3 * _GiB,
                days=2.0,
                start_ts=start,
            )

        diag = _run(store, "DISK-FULL-01", "DISK-GROWTH-01")

        assert [f.rule_id for f in diag.findings] == ["DISK-FULL-01"]
        for out in (
            render_terminal(diag, color=False),
            render_json(diag),
            render_html(diag),
        ):
            assert "/var/log/plc-app" in out
            assert "/opt/vision/images" in out

    def test_the_json_keeps_each_folded_finding(self, tmp_path):
        store, start = _recording(tmp_path, 48)
        _disk(
            store,
            [
                {
                    "ts": start + h * _HOUR,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": int(70 * _GiB + 6 * _GiB * h / 24),
                }
                for h in range(49)
            ],
        )
        add_dir_series(
            store,
            "/var/log/plc-app",
            start=1 * _GiB,
            daily_growth=3 * _GiB,
            days=2.0,
            start_ts=start,
        )

        (full,) = json.loads(
            render_json(_run(store, "DISK-FULL-01", "DISK-GROWTH-01"))
        )["findings"]

        assert full["related"] == ["DISK-GROWTH-01"]
        (growth,) = full["folded"]
        assert growth["rule_id"] == "DISK-GROWTH-01"
        assert growth["evidence"]["path"] == "/var/log/plc-app"


class TestTruncatedScans:
    def test_a_scan_cut_short_is_not_read_as_growth(self, tmp_path):
        store, start = _recording(tmp_path, 24)
        _dirs(
            store,
            [(start, "/srv/archive", 3 * _GiB, 1)]
            + [(start + h * _HOUR, "/srv/archive", 40 * _GiB, 0) for h in range(1, 25)],
        )

        diag = _run(store, "DISK-GROWTH-01")

        assert _found(diag, "DISK-GROWTH-01") == []
        assert "DISK-GROWTH-01" in diag.evaluated_rules

    def test_growth_is_measured_between_complete_scans(self, tmp_path):
        store, start = _recording(tmp_path, 24)
        _dirs(
            store,
            [
                (start + h * _HOUR, "/srv/archive", 10 * _GiB + h * _GiB // 24, 0)
                for h in range(24)
            ]
            + [(start + 24 * _HOUR, "/srv/archive", 5 * _GiB, 1)],
        )

        (f,) = _found(_run(store, "DISK-GROWTH-01"), "DISK-GROWTH-01")

        assert f.evidence["daily_growth_mb"] == pytest.approx(1024, rel=0.05)

    def test_a_directory_never_scanned_to_the_end_is_not_evaluated(self, tmp_path):
        store, start = _recording(tmp_path, 24)
        _dirs(
            store,
            [
                (start + h * _HOUR, "/srv/archive", (3 + h % 5) * _GiB, 1)
                for h in range(25)
            ],
        )

        diag = _run(store, "DISK-GROWTH-01")

        assert "DISK-GROWTH-01" in _uncovered(diag)
        assert "DISK-GROWTH-01" not in diag.evaluated_rules

    def test_a_directory_without_two_complete_scans_is_named(self, tmp_path):
        store, start = _recording(tmp_path, 24)
        _dirs(
            store,
            [
                (start + h * _HOUR, "/srv/grow", (1 + h // 4) * _GiB, 0)
                for h in range(25)
            ]
            + [
                (start + h * _HOUR, "/srv/archive", (20 + 2 * h) * _GiB, 1)
                for h in range(25)
            ]
            + [
                (
                    start + h * _HOUR,
                    "/srv/nearly",
                    (20 + 2 * h) * _GiB,
                    0 if h == 7 else 1,
                )
                for h in range(25)
            ]
            + [
                (start + h * _HOUR, "/srv/mixed", 5 * _GiB, 1 if h == 3 else 0)
                for h in range(25)
            ],
        )

        diag = _run(store, "DISK-GROWTH-01")

        assert [f.subject for f in _found(diag, "DISK-GROWTH-01")] == ["/srv/grow"]
        (note,) = [n for n in diag.quality.notes if "DISK-GROWTH-01" in n]
        assert "完了した走査が 2 回に満たず増分を判定できません" in note
        assert "/srv/archive" in note and "/srv/nearly" in note
        assert "/srv/grow" not in note and "/srv/mixed" not in note


class TestShortTrends:
    def test_one_copy_in_a_one_hour_recording_is_not_a_forecast(self, tmp_path):
        store, start = _recording(tmp_path, 1)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": (40 + (3 if m >= 30 else 0)) * _GiB,
                    "inodes_total": 1_000_000,
                    "inodes_used": 400_000 + (20_000 if m >= 30 else 0),
                    "read_only": 0,
                }
                for m in range(61)
            ],
        )

        diag = _run(store, "DISK-FULL-01", "DISK-INODE-01")

        assert diag.findings == []
        assert sorted(diag.evaluated_rules) == ["DISK-FULL-01", "DISK-INODE-01"]

    def test_three_minutes_of_inode_growth_is_not_a_forecast(self, tmp_path):
        store, start = _recording(tmp_path, 3 / 60)
        _disk(
            store,
            [
                {
                    "ts": start + 60 * k,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": 40 * _GiB,
                    "inodes_total": 1_000_000,
                    "inodes_used": 400_000 + 10_000 * k,
                    "read_only": 0,
                }
                for k in range(3)
            ],
        )

        assert _found(_run(store, "DISK-INODE-01"), "DISK-INODE-01") == []

    def test_a_forecast_needs_six_hours_of_the_mount(self, tmp_path):
        store, start = _recording(tmp_path, 5)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": int(80 * _GiB + 10 * _GiB * m / 1440),
                }
                for m in range(5 * 60 + 1)
            ],
        )

        assert _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01") == []

    def test_steady_growth_over_six_hours_is_forecast(self, tmp_path):
        store, start = _recording(tmp_path, 6)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": int(80 * _GiB + 10 * _GiB * m / 1440),
                }
                for m in range(6 * 60 + 1)
            ],
        )

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.severity is Severity.CRITICAL
        assert f.evidence["days_left"] == pytest.approx(1.75, rel=0.02)

    @staticmethod
    def _with_usb(tmp_path, usb):
        store, start = _recording(tmp_path, 24)
        rows = _flat(start, 24 * 60, mountpoint="/", total=100 * _GiB, used=40 * _GiB)
        rows += [
            {
                "ts": start + m * 60,
                "mountpoint": "/media/usb",
                "total": total * _GiB,
                "used": used * _GiB,
                "read_only": read_only,
            }
            for m, total, used, read_only in usb
        ]
        _disk(store, rows)
        return store

    def test_a_stick_mounted_minutes_before_the_end_is_not_forecast(self, tmp_path):
        store = self._with_usb(
            tmp_path, [(24 * 60 - 3 + k, 32, 1 + k, 0) for k in range(4)]
        )

        assert _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01") == []

    def test_a_stick_plugged_in_for_minutes_is_not_forecast(self, tmp_path):
        store = self._with_usb(tmp_path, [(600 + k, 32, 1 + k, 0) for k in range(4)])

        assert _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01") == []

    def test_another_stick_at_the_same_mount_point_is_not_growth(self, tmp_path):
        store = self._with_usb(
            tmp_path,
            [(m, 8, 1, 0) for m in range(120, 541)]
            + [(m, 64, 30, 0) for m in range(720, 1201)],
        )

        assert _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01") == []

    def test_a_card_swapped_for_a_write_protected_one_is_not_a_remount(self, tmp_path):
        store = self._with_usb(
            tmp_path,
            [(m, 16, 2, 0) for m in range(60, 91)]
            + [(m, 4, 1, 1) for m in range(300, 331)],
        )

        assert _found(_run(store, "FS-READONLY-01"), "FS-READONLY-01") == []

    def test_a_stick_plugged_in_for_an_hour_is_named_with_the_hours_it_needs(
        self, tmp_path
    ):
        store = self._with_usb(tmp_path, [(600 + k, 32, 1, 0) for k in range(61)])

        diag = _run(store, "DISK-FULL-01")

        assert _pending(diag) == {("DISK-FULL-01", "/media/usb"): pytest.approx(5.0)}

    def test_another_stick_at_the_same_mount_point_is_not_inode_growth(self, tmp_path):
        store, start = _recording(tmp_path, 24)
        small = {"total": 8 * _GiB, "inodes_total": 500_000, "inodes_used": 10_000}
        large = {"total": 64 * _GiB, "inodes_total": 4_000_000}
        _disk(
            store,
            [
                {"ts": start + m * 60, "mountpoint": "/media/usb", "used": _GiB}
                | small
                | {"read_only": 0}
                for m in range(120, 541)
            ]
            + [
                {"ts": start + m * 60, "mountpoint": "/media/usb", "used": _GiB}
                | large
                | {"inodes_used": 2_000_000, "read_only": 0}
                for m in range(720, 1201)
            ],
        )

        assert _found(_run(store, "DISK-INODE-01"), "DISK-INODE-01") == []


def _pending(diag):
    """What each rule ran without judging yet: ``{(rule, mount): hours to wait}``."""
    return {
        (r.rule_id, r.subject): r.remaining_hours for r in diag.skipped_rules if r.part
    }


def _check(db, *args, only="DISK-FULL-01", code=0):
    result = CliRunner().invoke(
        cli, ["check", "--db", str(db), "--only", only, "--no-color", *args]
    )
    assert result.exit_code == code, result.output
    return result.output


def _inode_rows(start, hours):
    return [
        {
            "ts": start + m * 60,
            "mountpoint": "/",
            "total": 100 * _GiB,
            "used": 40 * _GiB,
            "inodes_total": 1_000_000,
            "inodes_used": 400_000 + 50_000 * m // 60,
            "read_only": 0,
        }
        for m in range(int(hours * 60) + 1)
    ]


class TestForecastNotJudged:
    @pytest.mark.parametrize("hours, wait", [(2, 4.0), (5, 1.0), (5.9, 0.1)])
    def test_a_fast_filling_disk_in_a_short_recording_is_not_fully_evaluated(
        self, tmp_path, hours, wait
    ):
        store, start = _recording(tmp_path, hours)
        _disk(store, _filling(start, hours, gb_per_hour=5))

        diag = _run(store, "DISK-FULL-01")

        assert "DISK-FULL-01" in diag.evaluated_rules
        assert not diag.fully_evaluated
        assert _pending(diag) == {("DISK-FULL-01", "/"): pytest.approx(wait, abs=0.05)}

    def test_the_usage_level_is_judged_while_the_forecast_waits(self, tmp_path):
        store, start = _recording(tmp_path, 3)
        used = int(92 * _GiB)
        _disk(
            store,
            _flat(
                start,
                180,
                mountpoint="/",
                total=100 * _GiB,
                used=used,
                avail=100 * _GiB - used,
                read_only=0,
            ),
        )

        diag = _run(store, "DISK-FULL-01")

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.severity is Severity.WARNING
        assert _pending(diag) == {("DISK-FULL-01", "/"): pytest.approx(3.0, abs=0.05)}

    def test_nothing_waits_once_the_mount_is_recorded_for_six_hours(self, tmp_path):
        store, start = _recording(tmp_path, 6)
        _disk(store, _filling(start, 6, gb_per_hour=0.01))

        diag = _run(store, "DISK-FULL-01")

        assert diag.skipped_rules == []
        assert diag.fully_evaluated

    def test_no_inode_forecast_waits_once_the_mount_is_recorded_for_six_hours(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 6)
        _disk(store, _inode_rows(start, 6))

        diag = _run(store, "DISK-INODE-01")

        assert diag.skipped_rules == []
        assert diag.fully_evaluated

    def test_an_inode_forecast_that_waits_is_not_fully_evaluated_either(self, tmp_path):
        store, start = _recording(tmp_path, 3)
        _disk(store, _inode_rows(start, 3))

        diag = _run(store, "DISK-INODE-01")

        assert "DISK-INODE-01" in diag.evaluated_rules
        assert _pending(diag) == {("DISK-INODE-01", "/"): pytest.approx(3.0, abs=0.05)}

    def test_only_the_mount_recorded_for_less_than_six_hours_waits(self, tmp_path):
        store = TestShortTrends._with_usb(
            tmp_path, [(600 + k, 32, 1, 0) for k in range(61)]
        )

        diag = _run(store, "DISK-FULL-01")

        assert set(_pending(diag)) == {("DISK-FULL-01", "/media/usb")}

    def test_the_hours_to_wait_follow_the_configured_min_trend_hours(self, tmp_path):
        store, start = _recording(tmp_path, 1)
        _disk(store, _filling(start, 1, gb_per_hour=5) + _inode_rows(start, 1))

        diag = _run(
            store,
            "DISK-FULL-01",
            "DISK-INODE-01",
            overrides={
                "DISK-FULL-01": {"min_trend_hours": 2.0},
                "DISK-INODE-01": {"min_trend_hours": 3.0},
            },
        )

        assert _pending(diag) == {
            ("DISK-FULL-01", "/"): pytest.approx(1.0, abs=0.05),
            ("DISK-INODE-01", "/"): pytest.approx(2.0, abs=0.05),
        }
        required = {r.rule_id: r.required_hours for r in diag.skipped_rules}
        assert required == {"DISK-FULL-01": 2.0, "DISK-INODE-01": 3.0}

    def test_a_lowered_min_trend_hours_judges_the_forecast_sooner(self, tmp_path):
        store, start = _recording(tmp_path, 3)
        _disk(store, _filling(start, 3, gb_per_hour=5))

        diag = _run(
            store, "DISK-FULL-01", overrides={"DISK-FULL-01": {"min_trend_hours": 2.0}}
        )

        assert diag.skipped_rules == []
        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is not None

    def test_check_text_does_not_call_it_a_plain_no_problem(self, tmp_path):
        store, start = _recording(tmp_path, 5)
        _disk(store, _filling(start, 5, gb_per_hour=5))
        store.close()

        out = _check(store.path)

        lines = [line for line in out.splitlines() if line.strip()]
        assert lines[0] == "診断: 評価できた範囲では問題は見つかりませんでした。"
        assert lines[1] == (
            "⚠ 評価範囲: 1 ルールを評価 / 一部が未判定 1 件"
            " (あと最長 1.0 時間の記録で判定できます)"
        )
        (waiting,) = [line for line in lines if line.startswith("  未評価:")]
        assert "DISK-FULL-01 (残り日数の予測: / あと 1.0h)" in waiting

    def test_check_text_names_every_mount_that_waits(self, tmp_path):
        store = TestShortTrends._with_usb(
            tmp_path, [(600 + k, 32, 1, 0) for k in range(61)]
        )
        first = min(r["ts"] for r in store.rows("disk_samples"))
        _disk(
            store,
            [
                {
                    "ts": first + (600 + k) * 60,
                    "mountpoint": "/mnt/data",
                    "total": 64 * _GiB,
                    "used": 8 * _GiB,
                    "read_only": 0,
                }
                for k in range(61)
            ],
        )
        store.close()

        out = _check(store.path)

        (waiting,) = [line for line in out.splitlines() if line.startswith("  未評価:")]
        assert "/media/usb あと 5.0h" in waiting and "/mnt/data あと 5.0h" in waiting
        assert "/ あと" not in waiting

    def test_check_json_carries_what_waits_in_skipped_rules(self, tmp_path):
        store, start = _recording(tmp_path, 5)
        _disk(store, _filling(start, 5, gb_per_hour=5))
        store.close()

        data = json.loads(_check(store.path, "--format", "json"))

        (entry,) = data["skipped_rules"]
        assert entry["rule_id"] == "DISK-FULL-01"
        assert (entry["part"], entry["subject"]) == ("残り日数の予測", "/")
        assert entry["required_hours"] == 6.0
        assert entry["remaining_hours"] == pytest.approx(1.0, abs=0.05)
        assert data["evaluated_rules"] == ["DISK-FULL-01"]

    def test_check_json_marks_a_whole_skipped_rule_without_a_part(self, tmp_path):
        store, start = _recording(tmp_path, 2)
        _disk(store, _filling(start, 2, gb_per_hour=1))
        store.close()

        data = json.loads(_check(store.path, "--format", "json", only="MEM-LEAK-01"))

        (entry,) = data["skipped_rules"]
        assert entry["rule_id"] == "MEM-LEAK-01"
        assert (entry["part"], entry["subject"]) == (None, None)

    def test_check_html_is_not_the_green_badge_alone(self, tmp_path):
        store, start = _recording(tmp_path, 5)
        _disk(store, _filling(start, 5, gb_per_hour=5))
        store.close()

        out = _check(store.path, "--format", "html")

        assert '<span class="badge-ok">' not in out
        assert '<span class="badge-warn">問題なし (一部未評価)</span>' in out
        assert "評価できた範囲では問題は見つかりませんでした" in out
        assert "あと 1.0 時間" in out and "残り日数の予測" in out

    def test_a_warning_and_a_pending_forecast_are_both_shown(self, tmp_path):
        store, start = _recording(tmp_path, 3)
        used = int(92 * _GiB)
        _disk(
            store,
            _flat(
                start,
                180,
                mountpoint="/",
                total=100 * _GiB,
                used=used,
                avail=100 * _GiB - used,
                read_only=0,
            ),
        )
        store.close()

        out = _check(store.path, code=1)

        assert "[DISK-FULL-01]" in out
        assert "DISK-FULL-01 (残り日数の予測: / あと 3.0h)" in out

    def test_the_inode_forecast_that_waits_is_named_in_the_check(self, tmp_path):
        store, start = _recording(tmp_path, 3)
        _disk(store, _inode_rows(start, 3))
        store.close()

        out = _check(store.path, only="DISK-INODE-01")

        assert "DISK-INODE-01 (残り日数の予測: / あと 3.0h)" in out
        assert "ルールすべてを評価" not in out


def _resized(start, hours, *, flat_hours):
    """A mount that was 100 GiB for *flat_hours*, then 200 GiB (another filesystem)."""
    rows = []
    for m in range(int(hours * 60) + 1):
        total = 100 * _GiB if m <= flat_hours * 60 else 200 * _GiB
        rows.append(
            {
                "ts": start + m * 60,
                "mountpoint": "/",
                "total": total,
                "used": 40 * _GiB,
                "avail": total - 40 * _GiB,
                "read_only": 0,
            }
        )
    return rows


def _compare(before, after, *args):
    result = CliRunner().invoke(
        cli,
        ["compare", str(before), str(after), "--only", "DISK-FULL-01", *args],
    )
    return result


def _one_disk_db(directory, hours, rows):
    directory.mkdir(parents=True, exist_ok=True)
    store, start = _recording(directory, hours)
    _disk(store, rows(start))
    store.close()
    return store.path


class TestCompareForecastNotJudged:
    def test_a_fix_is_not_resolved_when_the_after_forecast_waits(self, tmp_path):
        before = _one_disk_db(
            tmp_path / "b", 24, lambda s: _filling(s, 24, gb_per_hour=1, first_gb=20)
        )
        after = _one_disk_db(
            tmp_path / "a", 24, lambda s: _resized(s, 24, flat_hours=23)
        )

        result = _compare(before, after, "--no-color")

        assert result.exit_code == 3, result.output
        rows = result.output.split("結果:")[0]
        assert "？ 判定不能  [DISK-FULL-01]" in rows
        assert "解消" not in rows
        data = json.loads(_compare(before, after, "--format", "json").output)
        (row,) = data["rows"]
        assert (row["verdict"], row["after"]["why"]) == ("not_evaluable", "too_short")

    def test_a_problem_is_not_new_when_the_before_forecast_waits(self, tmp_path):
        before = _one_disk_db(
            tmp_path / "b", 24, lambda s: _resized(s, 24, flat_hours=23)
        )
        after = _one_disk_db(
            tmp_path / "a", 24, lambda s: _filling(s, 24, gb_per_hour=1, first_gb=20)
        )

        result = _compare(before, after, "--no-color")

        assert result.exit_code == 1, result.output
        rows = result.output.split("結果:")[0]
        assert "新規" not in rows
        data = json.loads(_compare(before, after, "--format", "json").output)
        (row,) = data["rows"]
        assert (row["verdict"], row["before"]["why"]) == ("not_evaluable", "too_short")

    def test_two_short_periods_that_both_fill_fast_are_not_no_problem(self, tmp_path):
        def filling(start):
            return _filling(start, 5, gb_per_hour=5)

        before = _one_disk_db(tmp_path / "b", 5, filling)
        after = _one_disk_db(tmp_path / "a", 5, filling)

        result = _compare(before, after, "--no-color")

        assert result.exit_code == 0, result.output
        assert (
            "評価できた範囲では、どちらの期間にも問題は検出されませんでした"
            in result.output
        )
        for side in ("修正前", "修正後"):
            section = result.output.split(f"{side}の未評価:")[1].splitlines()[1]
            assert (
                "DISK-FULL-01" in section and "残り日数の予測: / あと 1.0h" in section
            )
        data = json.loads(_compare(before, after, "--format", "json").output)
        assert data["rows"] == []
        for side in ("before", "after"):
            (entry,) = data[side]["skipped_rules"]
            assert (entry["rule_id"], entry["subject"]) == ("DISK-FULL-01", "/")

    def test_a_mount_judged_in_both_periods_is_resolved_while_another_waits(
        self, tmp_path
    ):
        before = _one_disk_db(
            tmp_path / "b", 24, lambda s: _filling(s, 24, gb_per_hour=1, first_gb=20)
        )

        def after_rows(start):
            root = _flat(
                start,
                24 * 60,
                mountpoint="/",
                total=100 * _GiB,
                used=30 * _GiB,
                avail=70 * _GiB,
                read_only=0,
            )
            stick = [
                {
                    "ts": start + (23 * 60 + k) * 60,
                    "mountpoint": "/media/usb",
                    "total": 32 * _GiB,
                    "used": _GiB,
                    "read_only": 0,
                }
                for k in range(61)
            ]
            return root + stick

        after = _one_disk_db(tmp_path / "a", 24, after_rows)

        result = _compare(before, after, "--no-color")

        assert result.exit_code == 0, result.output
        assert "✔ 解消  [DISK-FULL-01]" in result.output
        data = json.loads(_compare(before, after, "--format", "json").output)
        assert [(r["subject"], r["verdict"]) for r in data["rows"]] == [
            ("/", "resolved")
        ]
        assert [r["subject"] for r in data["after"]["skipped_rules"]] == ["/media/usb"]

    def test_a_fix_is_resolved_once_both_forecasts_are_judged(self, tmp_path):
        before = _one_disk_db(
            tmp_path / "b", 24, lambda s: _filling(s, 24, gb_per_hour=1, first_gb=20)
        )
        after = _one_disk_db(
            tmp_path / "a",
            24,
            lambda s: _flat(
                s,
                24 * 60,
                mountpoint="/",
                total=100 * _GiB,
                used=30 * _GiB,
                avail=70 * _GiB,
                read_only=0,
            ),
        )

        result = _compare(before, after, "--no-color")

        assert result.exit_code == 0, result.output
        assert "✔ 解消  [DISK-FULL-01]" in result.output


class TestLevelWording:
    @staticmethod
    def _at(tmp_path, pct):
        store, start = _recording(tmp_path, 3)
        used = int(100 * _GiB * pct / 100)
        _disk(
            store,
            _flat(
                start,
                180,
                mountpoint="/",
                total=100 * _GiB,
                used=used,
                avail=100 * _GiB - used,
                read_only=0,
            ),
        )
        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")
        return f

    def test_a_warning_just_under_the_critical_line_is_not_called_urgent(
        self, tmp_path
    ):
        f = self._at(tmp_path, 94.96)

        assert f.severity is Severity.WARNING
        assert "緊急対応が必要です" not in f.proposal

    def test_a_critical_disk_is_called_urgent(self, tmp_path):
        f = self._at(tmp_path, 95.0)

        assert f.severity is Severity.CRITICAL
        assert "緊急対応が必要です" in f.proposal


class TestJournalCoverage:
    def test_the_journal_is_not_evaluated_when_only_var_log_is_watched(self, tmp_path):
        store, start = _recording(tmp_path, 2)
        _dirs(
            store,
            [(start + h * _HOUR, "/var/log", 12 * _GiB, 0) for h in range(3)],
        )

        diag = _run(store, "DISK-JOURNAL-01")

        assert "DISK-JOURNAL-01" not in diag.evaluated_rules
        assert any("/var/log/journal" in m for m in _uncovered(diag)["DISK-JOURNAL-01"])

    def test_an_unreadable_journal_directory_is_not_evaluated(self, tmp_path):
        store, start = _recording(tmp_path, 2)
        _dirs(
            store,
            [(start + h * _HOUR, "/var/log", 12 * _GiB, 0) for h in range(3)]
            + [(start + h * _HOUR, "/var/log/journal", None, 0) for h in range(3)],
        )

        diag = _run(store, "DISK-JOURNAL-01")

        assert "DISK-JOURNAL-01" in _uncovered(diag)

    def test_a_measured_journal_is_evaluated(self, tmp_path):
        store, start = _recording(tmp_path, 2)
        _dirs(
            store,
            [(start + h * _HOUR, "/var/log", 12 * _GiB, 0) for h in range(3)]
            + [(start + h * _HOUR, "/var/log/journal", 1 * _GiB, 0) for h in range(3)],
        )

        diag = _run(store, "DISK-JOURNAL-01")

        assert diag.evaluated_rules == ["DISK-JOURNAL-01"]
        assert diag.findings == []


class TestGrowthProposalNumbers:
    def test_days_are_rounded_and_a_threshold_under_1_mb_is_kept(self, tmp_path):
        store, start = _recording(tmp_path, 13)
        _dirs(
            store,
            [
                (start + h * _HOUR, "/opt/app/data", 5 * _GiB + h * 300 * _MiB, 0)
                for h in range(14)
            ],
        )

        (f,) = _found(
            _run(
                store,
                "DISK-GROWTH-01",
                overrides={"DISK-GROWTH-01": {"threshold_bytes_per_day": 512 * _KiB}},
            ),
            "DISK-GROWTH-01",
        )

        assert "閾値 0.5 MB/日" in f.summary
        assert "0.5 日間" in f.proposal
        assert "閾値 0.5 MB/日" in f.proposal
        assert "0.5416" not in f.proposal


def _schema5_db(path, start, *, used):
    """A DB as procdiag wrote it at schema 5: disk_samples without avail."""
    with Storage(path) as store:
        add_sys_baseline(store, hours=3 + 1 / 60, start_ts=start)
    conn = sqlite3.connect(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(disk_samples)")}
    if "avail" in columns:
        conn.execute("ALTER TABLE disk_samples DROP COLUMN avail")
    conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    conn.executemany(
        "INSERT INTO disk_samples (ts, mountpoint, total, used, read_only)"
        " VALUES (?, '/', ?, ?, 0)",
        [(start + m * 60, 100 * _GiB, used) for m in range(181)],
    )
    conn.commit()
    conn.close()


def _version_and_columns(path):
    conn = sqlite3.connect(path)
    (version,) = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(disk_samples)")}
    conn.close()
    return version, columns


class TestSchema6:
    def test_a_write_open_adds_the_column_and_keeps_the_rows(self, tmp_path):
        path = tmp_path / "v5.db"
        _schema5_db(path, time.time() - 4 * _HOUR, used=40 * _GiB)

        with Storage(path) as store:
            rows = store.rows("disk_samples")

        version, columns = _version_and_columns(path)
        assert version == "6"
        assert "avail" in columns
        assert len(rows) == 181
        assert {r["used"] for r in rows} == {40 * _GiB}
        assert {r["avail"] for r in rows} == {None}

    def test_a_read_only_open_of_schema_5_reports_a_full_disk(self, tmp_path):
        path = tmp_path / "v5.db"
        _schema5_db(path, time.time() - 4 * _HOUR, used=100 * _GiB)

        with Storage(path, read_only=True) as store:
            diag = _run(store, "DISK-FULL-01")

        assert [f.severity for f in diag.findings] == [Severity.CRITICAL]
        assert diag.errored_rules == []
        assert _version_and_columns(path)[0] == "5"

    def test_a_migration_cut_short_leaves_the_database_at_schema_5(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "v5.db"
        _schema5_db(path, time.time() - 4 * _HOUR, used=40 * _GiB)

        def killed(conn):
            raise sqlite3.OperationalError("killed")

        monkeypatch.setitem(
            storage._MIGRATIONS, 6, [*storage._MIGRATIONS.get(6, []), killed]
        )
        with pytest.raises(sqlite3.OperationalError):
            Storage(path)

        version, columns = _version_and_columns(path)
        assert version == "5"
        assert "avail" not in columns

    def test_it_migrates_on_a_disk_without_room_for_a_copy(self, tmp_path, monkeypatch):
        path = tmp_path / "v5.db"
        _schema5_db(path, time.time() - 4 * _HOUR, used=40 * _GiB)
        conn = sqlite3.connect(path)
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.close()
        connect = sqlite3.connect

        def nearly_full(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.execute(f"PRAGMA max_page_count = {pages + 8}")
            return conn

        monkeypatch.setattr(sqlite3, "connect", nearly_full)
        with Storage(path):
            pass

        monkeypatch.setattr(sqlite3, "connect", connect)
        assert _version_and_columns(path) == ("6", {*TABLE_COLUMNS["disk_samples"]})


class TestSnapshotThresholds:
    def test_the_snapshot_judges_a_disk_with_the_disk_full_thresholds(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            snap.psutil,
            "disk_partitions",
            lambda all=False: [_Part("/dev/sda1", "/", "ext4", "rw")],
        )
        monkeypatch.setattr(
            snap.psutil, "disk_usage", lambda mp: _Usage(100, 60, 40, 60.0)
        )
        monkeypatch.setattr(disk_mod, "_inode_usage", lambda mp: (None, None))
        config = ProcdiagConfig(
            rules=RulesConfig(overrides={"DISK-FULL-01": {"warning_pct": 50.0}})
        )

        (item,) = snap._check_disks(config)

        assert item.status == "warning"


class TestPercentNeverShownAcrossAThreshold:
    @staticmethod
    def _filled(tmp_path, *, used_pct: float, inode_pct: float | None = None):
        inode_pct = used_pct if inode_pct is None else inode_pct
        store, start = _recording(tmp_path, 2)
        total = 100 * _GiB
        used = int(total * used_pct / 100)
        _disk(
            store,
            _flat(
                start,
                120,
                mountpoint="/",
                total=total,
                used=used,
                avail=total - used,
                inodes_total=1_000_000,
                inodes_used=int(1_000_000 * inode_pct / 100),
                read_only=0,
            ),
        )
        return store

    def test_a_disk_at_94_96_percent_is_a_warning_that_does_not_read_95_0(
        self, tmp_path
    ):
        store = self._filled(tmp_path, used_pct=94.96)
        diag = _run(store, "DISK-FULL-01")
        (f,) = _found(diag, "DISK-FULL-01")
        assert f.severity is Severity.WARNING
        assert "95.0%" not in f.summary and "94.9%" in f.summary
        assert f.evidence["used_pct"] == 94.9
        assert "使用率 94.9%" in f.proposal

    def test_a_disk_at_exactly_95_percent_is_critical_and_reads_95_0(self, tmp_path):
        store = self._filled(tmp_path, used_pct=95.0)
        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")
        assert f.severity is Severity.CRITICAL
        assert "95.0%" in f.summary and f.evidence["used_pct"] == 95.0

    def test_inodes_at_94_96_percent_do_not_read_95_0(self, tmp_path):
        store = self._filled(tmp_path, used_pct=94.96)
        (f,) = _found(_run(store, "DISK-INODE-01"), "DISK-INODE-01")
        assert f.severity is Severity.WARNING
        assert "95.0%" not in f.summary and "94.9%" in f.summary
        assert f.evidence["used_pct"] == 94.9

    @pytest.mark.parametrize(
        "bytes_pct, shown", [(94.96, "94.9"), (89.96, "89.9"), (95.0, "95.0")]
    )
    def test_the_byte_usage_beside_an_inode_finding_is_cut_the_same_way(
        self, tmp_path, bytes_pct, shown
    ):
        store = self._filled(tmp_path, used_pct=bytes_pct, inode_pct=97.0)
        (f,) = _found(_run(store, "DISK-INODE-01"), "DISK-INODE-01")
        assert f.evidence["bytes_used_pct"] == float(shown)
        assert f"(容量の使用率は {shown}%)" in f.proposal

    def test_the_byte_usage_that_does_not_reach_a_level_is_still_rounded(
        self, tmp_path
    ):
        store = self._filled(tmp_path, used_pct=61.26, inode_pct=97.0)
        (f,) = _found(_run(store, "DISK-INODE-01"), "DISK-INODE-01")
        assert "(容量の使用率は 61.3%)" in f.proposal


class TestUnjudgedPartsAreInTheVerdict:
    @staticmethod
    def _cut_short_scans(tmp_path):
        store, start = _recording(tmp_path, 24)
        _dirs(
            store,
            [(start + h * _HOUR, "/srv/steady", 5 * _GiB, 0) for h in range(25)]
            + [
                (start + h * _HOUR, "/srv/archive", (20 + 2 * h) * _GiB, 1)
                for h in range(25)
            ],
        )
        return store

    @staticmethod
    def _screen(db, *args):
        result = CliRunner().invoke(
            cli, ["check", "--db", str(db), "--only", "DISK-GROWTH-01", *args]
        )
        assert result.exit_code in (0, 1, 2), result.output
        return result.output

    def test_the_engine_names_the_rule_that_left_a_part_unjudged(self, tmp_path):
        diag = _run(self._cut_short_scans(tmp_path), "DISK-GROWTH-01")
        assert diag.evaluated_rules == ["DISK-GROWTH-01"]
        assert diag.fully_evaluated is False
        assert list(diag.unjudged_parts) == ["DISK-GROWTH-01"]
        assert "/srv/archive" in " ".join(diag.unjudged_parts["DISK-GROWTH-01"])

    def test_check_text_does_not_call_it_a_plain_no_problem(self, tmp_path):
        store = self._cut_short_scans(tmp_path)
        store.close()
        lines = [line for line in self._screen(store.path).splitlines() if line.strip()]
        verdict = next(line for line in lines if line.startswith("診断:"))
        assert verdict.startswith("診断: 評価できた範囲では")
        coverage = next(line for line in lines if line.startswith("⚠ 評価範囲"))
        assert "一部を判定できていない 1 件" in coverage
        assert "DISK-GROWTH-01" in coverage

    def test_check_json_and_html_say_so_too(self, tmp_path):
        store = self._cut_short_scans(tmp_path)
        store.close()
        data = json.loads(self._screen(store.path, "--format", "json"))
        assert data["unjudged_rules"] == ["DISK-GROWTH-01"]
        html = self._screen(store.path, "--format", "html")
        assert "問題なし (一部未評価)" in html

    def test_a_directory_with_two_complete_scans_leaves_nothing_unjudged(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 24)
        _dirs(
            store,
            [
                (start + h * _HOUR, "/srv/grow", (1 + h // 4) * _GiB, 0)
                for h in range(25)
            ],
        )
        diag = _run(store, "DISK-GROWTH-01")
        assert diag.unjudged_parts == {}
        assert diag.fully_evaluated is True


class TestDirectoryGrowthNeedsTwelveHours:
    @staticmethod
    def _growing(tmp_path, hours, scans):
        store, start = _recording(tmp_path, hours)
        _dirs(
            store,
            [
                (start + h * _HOUR, "/srv/images", (1 + h) * _GiB, 0)
                for h in range(scans)
            ],
        )
        return _run(store, "DISK-GROWTH-01")

    def test_a_recording_of_eight_hours_is_not_evaluated_not_healthy(self, tmp_path):
        diag = self._growing(tmp_path, 8, 9)

        assert diag.findings == []
        assert diag.evaluated_rules == []
        (waits,) = diag.skipped_rules
        assert waits.rule_id == "DISK-GROWTH-01"
        assert waits.remaining_hours == pytest.approx(4.0, abs=0.1)

    def test_scans_spanning_eleven_hours_wait_for_the_twelfth(self, tmp_path):
        diag = self._growing(tmp_path, 12, 12)

        assert diag.findings == []
        (waits,) = diag.skipped_rules
        assert waits.remaining_hours == pytest.approx(1.0, abs=0.1)

    def test_scans_spanning_twelve_hours_are_judged(self, tmp_path):
        diag = self._growing(tmp_path, 12, 13)

        (f,) = _found(diag, "DISK-GROWTH-01")
        assert diag.skipped_rules == []
        assert f.evidence["daily_growth_mb"] == pytest.approx(24 * 1024, rel=0.01)

    def test_one_scan_in_a_recording_of_eight_hours_waits_for_twelve(self, tmp_path):
        diag = self._growing(tmp_path, 8, 1)

        assert diag.evaluated_rules == []
        (waits,) = diag.skipped_rules
        assert waits.remaining_hours == pytest.approx(4.0, abs=0.1)

    def test_check_says_how_long_to_record_instead_of_no_problem(self, tmp_path):
        store, start = _recording(tmp_path, 8)
        _dirs(
            store,
            [(start + h * _HOUR, "/srv/images", (1 + h) * _GiB, 0) for h in range(9)],
        )
        store.close()

        out = _check(store.path, only="DISK-GROWTH-01")

        assert "DISK-GROWTH-01" in out
        assert "観測時間不足" in out
        assert "問題は見つかりませんでした" not in out


def _steady(
    start, hours, *, mountpoint="/media/usb", total=100, from_h=0.0, gb_h=2, shift=0
):
    """Rows each minute from *from_h* to *hours* of a mount growing *gb_h* an hour.

    *shift* GiB is added to the usage, as a stick with other contents has.
    """
    return [
        {
            "ts": start + m * 60,
            "mountpoint": mountpoint,
            "total": total * _GiB,
            "used": int((20 + shift + gb_h * m / 60) * _GiB),
            "read_only": 0,
        }
        for m in range(int(from_h * 60), int(hours * 60) + 1)
    ]


def _cut_system_rows(store, start, from_h, to_h):
    """Remove the system samples of a stretch, as a stopped recorder leaves it."""
    lo, hi = start + from_h * _HOUR, start + to_h * _HOUR
    store.conn.execute("DELETE FROM sys_samples WHERE ts BETWEEN ? AND ?", (lo, hi))
    store.conn.execute(
        "DELETE FROM sys_rollup_1m WHERE ts_min BETWEEN ? AND ?", (lo, hi)
    )
    store.conn.commit()


class TestAnotherMediumOfTheSameSize:
    def test_a_stick_of_the_same_size_after_hours_unplugged_is_not_growth(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 24)
        sticks = [(m, 32, 1, 0) for m in range(120, 541)] + [
            (m, 32, 29, 0) for m in range(720, 1201)
        ]
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/media/usb",
                    "total": total * _GiB,
                    "used": used * _GiB,
                    "read_only": read_only,
                }
                for m, total, used, read_only in sticks
            ],
        )

        diag = _run(store, "DISK-FULL-01", "DISK-INODE-01")

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.severity is Severity.WARNING
        assert f.evidence["days_left"] is None
        assert "満杯" not in f.summary.split("使用率")[-1]

    def test_the_same_stick_unplugged_for_ten_minutes_is_one_filesystem(self, tmp_path):
        store, start = _recording(tmp_path, 12)
        _disk(
            store,
            _steady(start, 5.0) + _steady(start, 12, from_h=5.0 + 10 / 60),
        )

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.evidence["days_left"] == pytest.approx(1.17, abs=0.1)

    def test_a_recorder_stop_between_samples_does_not_end_the_filesystem(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 14)
        _cut_system_rows(store, start, 8.01, 10.99)
        _disk(store, _steady(start, 8.0) + _steady(start, 14, from_h=11.0))

        diag = _run(store, "DISK-FULL-01")

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is not None
        assert diag.skipped_rules == []

    def test_a_mount_missing_while_the_recorder_ran_ends_the_filesystem(self, tmp_path):
        store, start = _recording(tmp_path, 14)
        _disk(
            store,
            _steady(start, 8.0) + _steady(start, 14, from_h=11.0, shift=30),
        )

        diag = _run(store, "DISK-FULL-01")

        assert _found(diag, "DISK-FULL-01") == []
        (waits,) = diag.skipped_rules
        assert waits.subject == "/media/usb"
        assert waits.remaining_hours == pytest.approx(3.0, abs=0.1)

    def test_a_mount_missing_for_two_minutes_is_not_a_new_filesystem(self, tmp_path):
        store, start = _recording(tmp_path, 8)
        _disk(store, _steady(start, 3.0) + _steady(start, 8, from_h=3.0 + 2 / 60))

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.evidence["days_left"] is not None

    def test_a_mount_missing_for_six_minutes_with_other_contents_is_a_new_filesystem(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 12)
        _disk(
            store,
            _steady(start, 7.0) + _steady(start, 12, from_h=7.0 + 6 / 60, shift=30),
        )

        diag = _run(store, "DISK-FULL-01")

        assert _found(diag, "DISK-FULL-01") == []
        assert _pending(diag) == {
            ("DISK-FULL-01", "/media/usb"): pytest.approx(1.1, abs=0.05)
        }

    @pytest.mark.parametrize("gap_minutes", [6, 10, 30, 60])
    def test_a_gap_in_the_rows_of_a_mount_that_grows_on_is_no_new_filesystem(
        self, tmp_path, gap_minutes
    ):
        store, start = _recording(tmp_path, 12)
        _disk(
            store,
            _steady(start, 10.0, total=200)
            + _steady(start, 12, total=200, from_h=10.0 + gap_minutes / 60),
        )

        diag = _run(store, "DISK-FULL-01")

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] == pytest.approx(3.4, abs=0.4)
        assert diag.skipped_rules == []

    def test_three_gaps_of_six_minutes_in_twelve_hours_still_give_a_forecast(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 12)
        rows = [
            row
            for row in _steady(start, 12, total=200)
            if not any(
                g * _HOUR <= row["ts"] - start < (g + 0.1) * _HOUR for g in (2, 5, 8)
            )
        ]
        _disk(store, rows)

        diag = _run(store, "DISK-FULL-01")

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is not None
        assert diag.skipped_rules == []

    def test_a_gap_with_a_total_that_changes_by_more_than_a_percent_is_a_new_filesystem(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 12)
        _disk(
            store,
            _steady(start, 7.0) + _steady(start, 12, from_h=7.0 + 6 / 60, total=97),
        )

        diag = _run(store, "DISK-FULL-01")

        assert _found(diag, "DISK-FULL-01") == []
        assert _pending(diag) == {
            ("DISK-FULL-01", "/media/usb"): pytest.approx(1.1, abs=0.05)
        }

    def test_a_gap_with_a_total_that_drifts_by_less_than_a_percent_is_no_new_one(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 12)
        rows = _steady(start, 7.0, total=500) + _steady(
            start, 12, from_h=7.0 + 6 / 60, total=498
        )
        _disk(store, rows)

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.evidence["days_left"] is not None

    def test_a_sparse_mount_is_judged_by_its_own_steps(self, tmp_path):
        store, start = _recording(tmp_path, 24)
        _disk(
            store,
            [
                {
                    "ts": start + h * _HOUR,
                    "mountpoint": "/media/usb",
                    "total": 100 * _GiB,
                    "used": (20 + 2 * h) * _GiB,
                    "read_only": 0,
                }
                for h in range(0, 9)
                if h != 5
            ],
        )

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.evidence["days_left"] is not None


class TestFilesystemWhoseSizeDrifts:
    @staticmethod
    def _tank(tmp_path, wobble):
        store, start = _recording(tmp_path, 24)
        rows = []
        for m in range(24 * 60 + 1):
            total = int(500 * _GiB * (1 + wobble * ((m % 7) - 3)))
            rows.append(
                {
                    "ts": start + m * 60,
                    "mountpoint": "/tank",
                    "total": total,
                    "used": int(100 * _GiB + 2 * _GiB * m / 60),
                    "read_only": 0,
                }
            )
        _disk(store, rows)
        return _run(store, "DISK-FULL-01")

    def test_a_total_drifting_by_a_few_percent_still_gets_a_forecast(self, tmp_path):
        (f,) = _found(self._tank(tmp_path, 0.004), "DISK-FULL-01")

        assert f.evidence["days_left"] == pytest.approx(7.4, abs=0.5)

    def test_a_total_changing_by_ten_percent_is_another_filesystem(self, tmp_path):
        diag = self._tank(tmp_path, 0.02)

        assert _found(diag, "DISK-FULL-01") == []
        assert _pending(diag) != {}


class TestOneFilesystemUnderSeveralMountPoints:
    @staticmethod
    def _bound(tmp_path, *, used_other, inodes_other=700_000):
        store, start = _recording(tmp_path, 3)
        rows = []
        for m in range(181):
            rows.append(
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": 95 * _GiB,
                    "inodes_total": 1_000_000,
                    "inodes_used": 920_000,
                    "read_only": 0,
                }
            )
            rows.append(
                {
                    "ts": start + m * 60,
                    "mountpoint": "/srv/data",
                    "total": 100 * _GiB,
                    "used": used_other,
                    "inodes_total": 1_000_000,
                    "inodes_used": inodes_other,
                    "read_only": 0,
                }
            )
        _disk(store, rows)
        return _run(store, "DISK-FULL-01", "DISK-INODE-01")

    def test_a_bind_mount_is_one_finding_naming_the_other_mount_point(self, tmp_path):
        diag = self._bound(
            tmp_path, used_other=95 * _GiB + 3 * _MiB, inodes_other=920_000
        )

        (full,) = _found(diag, "DISK-FULL-01")
        (inodes,) = _found(diag, "DISK-INODE-01")
        for f in (full, inodes):
            assert f.subject == "/"
            assert f.evidence["also_mounted_at"] == ["/srv/data"]
            assert "同じファイルシステム: /srv/data" in f.summary

    def test_a_filesystem_of_the_same_size_with_other_contents_is_its_own_finding(
        self, tmp_path
    ):
        diag = self._bound(tmp_path, used_other=92 * _GiB, inodes_other=910_000)

        assert sorted(f.subject for f in _found(diag, "DISK-FULL-01")) == [
            "/",
            "/srv/data",
        ]
        assert sorted(f.subject for f in _found(diag, "DISK-INODE-01")) == [
            "/",
            "/srv/data",
        ]


class TestBindMountsThatDifferInWhatTheyShow:
    @staticmethod
    def _mounts(tmp_path, *mounts, hours=3, inodes_used=700_000):
        """*mounts* are ``(path, read_only, used GiB, avail GiB)`` of a 100 GiB disk."""
        store, start = _recording(tmp_path, hours)
        rows = []
        for m in range(hours * 60 + 1):
            for path, read_only, used, avail in mounts:
                rows.append(
                    {
                        "ts": start + m * 60,
                        "mountpoint": path,
                        "total": 100 * _GiB,
                        "used": int(used * _GiB),
                        "avail": int(avail * _GiB),
                        "inodes_total": 1_000_000,
                        "inodes_used": inodes_used,
                        "read_only": read_only,
                    }
                )
        _disk(store, rows)
        return _run(store, "DISK-FULL-01", "DISK-INODE-01")

    def test_a_full_disk_is_reported_when_a_read_only_bind_mount_has_the_shorter_path(
        self, tmp_path
    ):
        diag = self._mounts(tmp_path, ("/mnt/data", 0, 97, 3), ("/mnt/a", 1, 97, 3))

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.severity is Severity.CRITICAL
        assert f.subject == "/mnt/data"
        assert f.evidence["also_mounted_at"] == ["/mnt/a"]
        assert "97.0%" in f.summary

    def test_a_full_disk_is_reported_when_a_read_only_bind_mount_has_the_longer_path(
        self, tmp_path
    ):
        diag = self._mounts(
            tmp_path, ("/mnt/data", 0, 97, 3), ("/mnt/data-ro", 1, 97, 3)
        )

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.subject == "/mnt/data"
        assert f.evidence["also_mounted_at"] == ["/mnt/data-ro"]

    def test_the_check_exits_with_the_finding_not_with_no_problem(self, tmp_path):
        store, start = _recording(tmp_path, 3)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": path,
                    "total": 100 * _GiB,
                    "used": 97 * _GiB,
                    "avail": 3 * _GiB,
                    "read_only": read_only,
                }
                for m in range(181)
                for path, read_only in (("/mnt/data", 0), ("/mnt/a", 1))
            ],
        )
        store.close()

        out = _check(store.path, code=1)

        assert "/mnt/data" in out and "97.0%" in out

    def test_a_read_only_filesystem_that_is_full_is_not_a_full_disk(self, tmp_path):
        diag = self._mounts(tmp_path, ("/mnt/a", 1, 97, 3), ("/mnt/b", 1, 97, 3))

        assert _found(diag, "DISK-FULL-01") == []

    def test_one_inode_finding_for_a_filesystem_whose_shorter_mount_is_read_only(
        self, tmp_path
    ):
        diag = self._mounts(
            tmp_path,
            ("/mnt/data", 0, 40, 60),
            ("/mnt/a", 1, 40, 60),
            inodes_used=960_000,
        )

        (f,) = _found(diag, "DISK-INODE-01")
        assert f.subject == "/mnt/data"
        assert f.evidence["also_mounted_at"] == ["/mnt/a"]

    def test_the_same_size_and_usage_with_other_reserved_space_is_another_filesystem(
        self, tmp_path
    ):
        diag = self._mounts(tmp_path, ("/data1", 0, 96, 4), ("/data2", 0, 96, 0))

        found = {f.subject: f for f in _found(diag, "DISK-FULL-01")}
        assert sorted(found) == ["/data1", "/data2"]
        assert found["/data2"].evidence["used_pct"] == 100.0
        assert found["/data1"].evidence["also_mounted_at"] == []

    def test_the_other_reserve_decides_whichever_path_is_the_shorter(self, tmp_path):
        diag = self._mounts(tmp_path, ("/d1", 0, 96, 0), ("/data2", 0, 96, 4))

        assert sorted(f.subject for f in _found(diag, "DISK-FULL-01")) == [
            "/d1",
            "/data2",
        ]

    def test_free_space_that_differs_by_a_write_between_two_reads_is_the_same(
        self, tmp_path
    ):
        diag = self._mounts(
            tmp_path,
            ("/data", 0, 97, 3),
            ("/srv/data", 0, 97 + 3 / 1024, 3 - 3 / 1024),
        )

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["also_mounted_at"] == ["/srv/data"]


class TestAGrowthThatIsOneStep:
    @staticmethod
    def _copy(tmp_path, hours, copies):
        """A 100 GiB disk at 40 GiB; each of *copies* (hour, GiB) lands at once."""
        store, start = _recording(tmp_path, hours)
        rows = []
        for m in range(int(hours * 60) + 1):
            used = 40 + sum(gb for h, gb in copies if m >= h * 60)
            rows.append(
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": used * _GiB,
                    "read_only": 0,
                }
            )
        _disk(store, rows)
        return _run(store, "DISK-FULL-01")

    def test_one_large_copy_in_a_recording_just_over_six_hours_is_no_forecast(
        self, tmp_path
    ):
        diag = self._copy(tmp_path, 6.5, [(3.0, 20)])

        assert _found(diag, "DISK-FULL-01") == []
        assert diag.evaluated_rules == ["DISK-FULL-01"]

    def test_the_verdict_says_the_forecast_was_not_judged(self, tmp_path):
        diag = self._copy(tmp_path, 6.5, [(3.0, 20)])

        assert diag.fully_evaluated is False
        (note,) = diag.unjudged_parts["DISK-FULL-01"]
        assert "一度の大きな増加" in note and "/" in note
        assert "コピー" not in note and "展開" not in note

    def test_check_text_does_not_call_it_a_plain_no_problem(self, tmp_path):
        store, start = _recording(tmp_path, 6.5)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": (60 if m >= 180 else 40) * _GiB,
                    "read_only": 0,
                }
                for m in range(6 * 60 + 31)
            ],
        )
        store.close()

        out = _check(store.path)

        assert "診断: 評価できた範囲では" in out
        assert "一度の大きな増加" in out

    def test_a_filesystem_filling_steadily_is_still_forecast(self, tmp_path):
        diag = self._copy(tmp_path, 6.5, [(h / 2, 2) for h in range(1, 13)])

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is not None
        assert diag.fully_evaluated is True

    def test_copies_that_keep_coming_are_a_trend(self, tmp_path):
        diag = self._copy(tmp_path, 6.5, [(1.0, 6), (3.5, 6), (5.5, 6)])

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is not None

    def test_a_copy_that_is_most_of_the_growth_is_one_step_whatever_else_grows(
        self, tmp_path
    ):
        diag = self._copy(
            tmp_path, 6.5, [(3.0, 20)] + [(h / 2, 1) for h in range(1, 6)]
        )

        assert _found(diag, "DISK-FULL-01") == []
        assert "DISK-FULL-01" in diag.unjudged_parts

    def test_a_copy_that_is_about_half_the_growth_is_still_a_trend(self, tmp_path):
        diag = self._copy(
            tmp_path, 6.5, [(3.0, 8)] + [(h / 2, 1.0) for h in range(1, 13)]
        )

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is not None

    def test_a_copy_that_takes_forty_minutes_is_one_step(self, tmp_path):
        store, start = _recording(tmp_path, 6.5)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": int((40 + 20 * min(max(m - 180, 0), 40) / 40) * _GiB),
                    "read_only": 0,
                }
                for m in range(6 * 60 + 31)
            ],
        )

        diag = _run(store, "DISK-FULL-01")

        assert _found(diag, "DISK-FULL-01") == []

    def test_growth_over_half_the_recording_is_not_one_step(self, tmp_path):
        store, start = _recording(tmp_path, 6.5)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": int((40 + 20 * min(max(m - 60, 0), 200) / 200) * _GiB),
                    "read_only": 0,
                }
                for m in range(6 * 60 + 31)
            ],
        )

        (f,) = _found(_run(store, "DISK-FULL-01"), "DISK-FULL-01")

        assert f.evidence["days_left"] is not None

    def test_a_copy_does_not_hide_a_disk_that_is_full(self, tmp_path):
        diag = self._copy(tmp_path, 6.5, [(3.0, 56)])

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is None
        assert f.severity is Severity.CRITICAL

    def test_one_large_run_of_new_inodes_is_no_forecast_either(self, tmp_path):
        store, start = _recording(tmp_path, 6.5)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 100 * _GiB,
                    "used": 40 * _GiB,
                    "inodes_total": 1_000_000,
                    "inodes_used": 400_000 + (300_000 if m >= 180 else 0),
                    "read_only": 0,
                }
                for m in range(6 * 60 + 31)
            ],
        )

        diag = _run(store, "DISK-INODE-01")

        assert _found(diag, "DISK-INODE-01") == []
        (note,) = diag.unjudged_parts["DISK-INODE-01"]
        assert "inode" in note


class TestGrowthThatGoesOnIsNotOneStep:
    @staticmethod
    def _used(tmp_path, hours, used_gib, *, total_gib=200):
        """A *total_gib* disk whose usage is ``used_gib(hour)``, sampled each minute."""
        store, start = _recording(tmp_path, hours)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/data",
                    "total": total_gib * _GiB,
                    "used": int(used_gib(m / 60) * _GiB),
                    "read_only": 0,
                }
                for m in range(int(hours * 60) + 1)
            ],
        )
        return _run(store, "DISK-FULL-01")

    @pytest.mark.parametrize("last_hours", [1, 2, 3, 4])
    def test_a_disk_that_started_filling_fast_in_the_last_hours_is_forecast(
        self, tmp_path, last_hours
    ):
        diag = self._used(
            tmp_path,
            24,
            lambda h: 100 + 10 * max(0.0, h - (24 - last_hours)),
        )

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is not None
        assert f.evidence["days_left"] < 30
        assert "DISK-FULL-01" not in diag.unjudged_parts

    def test_the_runaway_of_the_last_four_hours_is_critical_with_its_days_left(
        self, tmp_path
    ):
        diag = self._used(tmp_path, 24, lambda h: 100 + 10 * max(0.0, h - 20))

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.severity is Severity.CRITICAL
        assert f.evidence["days_left"] == pytest.approx(3.4, abs=0.3)

    def test_growth_that_goes_on_after_a_large_step_is_a_trend(self, tmp_path):
        diag = self._used(
            tmp_path,
            6.5,
            lambda h: 40 + (20 if h >= 3 else 0) + 4 * max(0.0, h - 3),
            total_gib=100,
        )

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is not None
        assert "DISK-FULL-01" not in diag.unjudged_parts

    def test_a_step_that_was_followed_by_a_rise_of_a_tenth_is_still_one_step(
        self, tmp_path
    ):
        diag = self._used(
            tmp_path,
            6.5,
            lambda h: 40 + (20 if h >= 3 else 0) + 0.4 * max(0.0, h - 3),
            total_gib=100,
        )

        assert _found(diag, "DISK-FULL-01") == []
        assert "DISK-FULL-01" in diag.unjudged_parts

    @pytest.mark.parametrize("offset", [0, 6, 12, 18])
    def test_a_log_rotation_sawtooth_keeps_its_forecast(self, tmp_path, offset):
        def used(hour):
            t = hour + offset
            day = int(t // 24)
            return 60 + day * 2 + (t % 24) / 24 * 10

        diag = self._used(tmp_path, 72, used, total_gib=100)

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.evidence["days_left"] is not None
        assert "DISK-FULL-01" not in diag.unjudged_parts

    def test_a_copy_well_before_the_end_of_a_day_is_still_one_step(self, tmp_path):
        diag = self._used(
            tmp_path,
            24,
            lambda h: 100 + (40 if h >= 8 else 0),
        )

        assert _found(diag, "DISK-FULL-01") == []
        assert "DISK-FULL-01" in diag.unjudged_parts

    def test_inodes_that_keep_being_used_up_to_the_last_samples_are_forecast(
        self, tmp_path
    ):
        store, start = _recording(tmp_path, 24)
        _disk(
            store,
            [
                {
                    "ts": start + m * 60,
                    "mountpoint": "/",
                    "total": 200 * _GiB,
                    "used": 40 * _GiB,
                    "inodes_total": 1_000_000,
                    "inodes_used": 500_000 + int(60_000 * max(0.0, m / 60 - 20)),
                    "read_only": 0,
                }
                for m in range(24 * 60 + 1)
            ],
        )

        diag = _run(store, "DISK-INODE-01")

        (f,) = _found(diag, "DISK-INODE-01")
        assert f.evidence["days_left"] is not None
        assert "DISK-INODE-01" not in diag.unjudged_parts


class TestRowsWithoutUsedBytes:
    def test_a_sample_that_lost_its_used_bytes_is_not_the_latest_usage(self, tmp_path):
        store, start = _recording(tmp_path, 8)
        rows = _flat(start, 360, mountpoint="/", total=100 * _GiB, used=96 * _GiB)
        rows.append({"ts": start + 361 * 60, "mountpoint": "/", "total": 100 * _GiB})
        _disk(store, rows)

        diag = _run(store, "DISK-FULL-01")

        (f,) = _found(diag, "DISK-FULL-01")
        assert f.severity is Severity.CRITICAL
        assert f.evidence["used_pct"] == 96.0
