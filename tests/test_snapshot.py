"""Tests for ``procdiag snapshot`` (procdiag/snapshot.py)."""

from __future__ import annotations

import json
from collections import namedtuple

import pytest
from click.testing import CliRunner

import procdiag.snapshot as snap
from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.kernlog import KernelEvent

_Part = namedtuple("_Part", "device mountpoint fstype opts")
_Usage = namedtuple("_Usage", "total used free percent")


def _statuses(items):
    return {i.name: i.status for i in items}


def _pressure_root(tmp_path, resource: str, avg60: float):
    d = tmp_path / "pressure"
    d.mkdir(exist_ok=True)
    (d / resource).write_text(
        f"some avg10=0.00 avg60={avg60:.2f} avg300=0.00 total=1\n"
        f"full avg10=0.00 avg60={avg60:.2f} avg300=0.00 total=1\n"
    )
    return tmp_path


class TestChecks:
    def test_disks_capacity_inodes_and_read_only_root(self, monkeypatch):
        parts = [
            _Part("/dev/sda1", "/", "ext4", "ro,relatime"),
            _Part("/dev/sdb1", "/data", "xfs", "rw"),
            _Part("tmpfs", "/run", "tmpfs", "rw"),  # pseudo-fs: ignored
        ]
        usage = {"/": _Usage(100, 96, 4, 96.0), "/data": _Usage(100, 50, 50, 50.0)}
        inodes = {"/": (1000, 100), "/data": (1000, 920)}
        monkeypatch.setattr(snap.psutil, "disk_partitions", lambda all=False: parts)
        monkeypatch.setattr(snap.psutil, "disk_usage", lambda mp: usage[mp])
        import procdiag.samplers.disk as disk_mod

        monkeypatch.setattr(disk_mod, "_inode_usage", lambda mp: inodes[mp])
        st = _statuses(snap._check_disks(ProcdiagConfig()))
        assert st == {
            "disk:/": "critical",
            "inode:/": "ok",
            "ro:/": "critical",
            "disk:/data": "ok",
            "inode:/data": "warning",
        }

    def test_memory_levels(self, monkeypatch, tmp_path):
        vm = namedtuple("vm", "total available")
        sw = namedtuple("sw", "total used percent")
        monkeypatch.setattr(snap.psutil, "virtual_memory", lambda: vm(100, 3))
        monkeypatch.setattr(snap.psutil, "swap_memory", lambda: sw(100, 85, 85.0))
        st = _statuses(snap._check_memory(ProcdiagConfig(), tmp_path))
        assert st == {"memory": "critical", "swap": "warning"}

    def test_cpu_psi_threshold_follows_overrides(self, tmp_path):
        root = _pressure_root(tmp_path, "cpu", 35.0)
        (item,) = snap._check_cpu(ProcdiagConfig(), [50.0, 50.0], root)
        assert item.status == "warning" and "35.0%" in item.detail
        relaxed = ProcdiagConfig(
            rules=RulesConfig(overrides={"CPU-SATURATION-01": {"psi_threshold": 50}})
        )
        (item,) = snap._check_cpu(relaxed, [50.0, 50.0], root)
        assert item.status == "ok"

    def test_fd_usage_and_unlimited_maximum(self, tmp_path):
        fs = tmp_path / "sys" / "fs"
        fs.mkdir(parents=True)
        (fs / "file-nr").write_text("850\t0\t1000\n")
        assert snap._check_fds(tmp_path)[0].status == "warning"
        (fs / "file-nr").write_text(f"850\t0\t{2**63 - 1}\n")
        (item,) = snap._check_fds(tmp_path)
        assert item.status == "ok" and "上限なし" in item.detail

    def test_processes_zombies_and_top_lists(self):
        procs = [
            {"pid": i, "name": f"p{i}", "status": "zombie", "cpu": 0.0, "rss": 0}
            for i in range(60)
        ] + [
            {"pid": 999, "name": "hog", "status": "running", "cpu": 95.0, "rss": 2**30}
        ]
        items = {i.name: i for i in snap._check_processes(procs)}
        assert items["zombies"].status == "warning"
        assert items["top_cpu"].detail.startswith("hog(pid 999) 95%")
        assert items["top_rss"].detail.startswith("hog(pid 999) 1.0GB")

    def test_kernel_log_severity_and_unreadable(self, monkeypatch):
        monkeypatch.setattr(
            snap.kernlog,
            "read_events",
            lambda since: ([KernelEvent(1.0, "io_error", "I/O error")] * 2, None),
        )
        (item,) = snap._check_kernel_log()
        assert item.status == "critical" and "2件" in item.detail
        monkeypatch.setattr(snap.kernlog, "read_events", lambda since: (None, None))
        assert snap._check_kernel_log()[0].status == "info"

    def test_services_failed_and_restart_counts(self, monkeypatch):
        monkeypatch.setattr(snap.systemd, "available", lambda: True)
        monkeypatch.setattr(snap.systemd, "failed_units", lambda: ["db.service"])
        monkeypatch.setattr(snap.systemd, "list_service_units", lambda: ["a", "b"])
        monkeypatch.setattr(
            snap.systemd,
            "show_units",
            lambda units: {"a": {"NRestarts": "5"}, "b": {"NRestarts": "0"}},
        )
        st = _statuses(snap._check_services(ProcdiagConfig()))
        assert st == {"failed_units": "warning", "restarted_units": "warning"}
        monkeypatch.setattr(
            snap.systemd, "show_units", lambda units: {"a": {"NRestarts": "1"}}
        )
        assert (
            _statuses(snap._check_services(ProcdiagConfig()))["restarted_units"]
            == "info"
        )

    def test_failing_check_degrades_to_info(self, monkeypatch):
        def boom():
            raise RuntimeError("no timedatectl")

        monkeypatch.setattr(snap.systemd, "ntp_synchronized", boom)
        (item,) = snap._check_time_sync()
        assert item.status == "info" and "no timedatectl" in item.detail


class TestRunAndCli:
    def test_real_host_snapshot(self, tmp_path):
        cfg = ProcdiagConfig(db_path=str(tmp_path / "none.db"))
        items = snap.run_snapshot(cfg, sample_sec=0.1)
        names = {i.name for i in items}
        assert {"cpu", "memory", "zombies", "recording"} <= names
        assert all(i.status in ("ok", "warning", "critical", "info") for i in items)
        assert snap.exit_code(items) in (0, 1)

    @pytest.mark.parametrize("args", [["--format", "json"], ["--ascii", "--no-color"]])
    def test_cli(self, tmp_path, monkeypatch, args):
        monkeypatch.chdir(tmp_path)  # no ./config.py → defaults
        monkeypatch.setattr(
            snap,
            "run_snapshot",
            lambda cfg: [
                snap.SnapshotItem("CPU", "cpu", "CPU", "warning", "高い", "確認")
            ],
        )
        result = CliRunner().invoke(cli, ["snapshot", *args])
        assert result.exit_code == 1
        if "--format" in args:
            data = json.loads(result.output)
            assert data["summary"]["warning"] == 1
        else:
            assert "[WARN] CPU: 高い" in result.output and "→ 確認" in result.output
