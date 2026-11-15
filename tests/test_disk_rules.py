"""Regression tests for the disk rules (DISK-FULL-01, DISK-INODE-01,
FS-READONLY-01, DISK-GROWTH-01, DISK-JOURNAL-01) and the column that keeps
the space other users can write (schema 6)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import namedtuple

import pytest
from synth import add_dir_series, add_sys_baseline, disk_row, make_db

import procdiag.samplers.disk as disk_mod
import procdiag.snapshot as snap
from procdiag import storage
from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Severity
from procdiag.core.sampler import RunContext
from procdiag.diagnosis import Engine
from procdiag.report import render_html, render_json, render_terminal
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
