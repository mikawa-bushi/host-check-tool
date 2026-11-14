"""Samplers record what the host did, not a sum that counts it twice or a 0 for
what they could not read.

Watch directories that cannot be read are unknown (NULL) and the rules that
need them are not evaluated; disk I/O bytes are counted once on LVM, dm-crypt,
md and loop stacks; bind-mounted files are not file systems; and a directory
walk stays on the file system it started on.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from synth import add_sys_baseline, make_db
from test_recorder import FakeClock, _AdvancingSleep

from procdiag.config import ProcdiagConfig, RulesConfig, SamplingConfig, WatchDir
from procdiag.core import CoverageGap
from procdiag.core.sampler import RunContext
from procdiag.diagnosis import Engine
from procdiag.recorder import Recorder
from procdiag.samplers.cgroup import CgroupSampler
from procdiag.samplers.disk import DiskSampler
from procdiag.samplers.system import SystemSampler
from procdiag.storage import TABLE_COLUMNS, Storage

_FIXTURES = Path(__file__).parent / "fixtures"
_DIR_RULES = ["DISK-GROWTH-01", "DISK-JOURNAL-01"]
_MiB = 1024 * 1024


def _scan(*dirs: Path) -> dict[str, tuple]:
    """``{path: (size_bytes, is_estimate)}`` of one watch-dir scan."""
    cfg = ProcdiagConfig(
        sampling=SamplingConfig(watch_dirs=[WatchDir(path=str(d)) for d in dirs])
    )
    cols = TABLE_COLUMNS["dir_samples"]
    rows = [
        dict(zip(cols, r))
        for b in DiskSampler().collect_dirs(RunContext(cfg))
        for r in b.rows
    ]
    return {r["path"]: (r["size_bytes"], r["is_estimate"]) for r in rows}


class TestUnreadableWatchDir:
    def test_missing_dir_is_unknown_and_an_empty_dir_is_zero(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "a.bin").write_bytes(b"x" * 1000)
        empty = tmp_path / "empty"
        empty.mkdir()
        not_mounted_yet = tmp_path / "not-mounted-yet"

        sizes = _scan(data, empty, not_mounted_yet)

        assert sizes[str(data)] == (1000, 0)
        assert sizes[str(empty)] == (0, 0)
        assert sizes[str(not_mounted_yet)] == (None, 0)

    def test_a_file_is_not_a_directory_size(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_bytes(b"x" * 10)
        assert _scan(log)[str(log)] == (None, 0)

    @pytest.mark.skipif(os.getuid() == 0, reason="root can read any directory")
    def test_permission_denied_dir_is_unknown(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "a.bin").write_bytes(b"x" * 1000)
        locked.chmod(0o000)
        try:
            sizes = _scan(locked)
        finally:
            locked.chmod(0o700)
        assert sizes[str(locked)] == (None, 0)

    def test_the_scan_names_the_unreadable_dir_once_per_condition(self, tmp_path):
        missing = tmp_path / "gone"
        cfg = ProcdiagConfig(
            sampling=SamplingConfig(watch_dirs=[WatchDir(path=str(missing))])
        )
        ctx, sampler = RunContext(cfg), DiskSampler()

        sampler.collect_dirs(ctx)
        sampler.collect_dirs(ctx)

        assert len(ctx.notes) == 1
        assert str(missing) in ctx.notes[0]


def _scan_into(store: Storage, *dirs: Path) -> None:
    """Store what the real disk sampler records for *dirs*, as the recorder would."""
    cfg = ProcdiagConfig(
        sampling=SamplingConfig(watch_dirs=[WatchDir(path=str(d)) for d in dirs])
    )
    inside_the_window = time.time() - 1800
    for batch in DiskSampler().collect_dirs(RunContext(cfg)):
        store.buffer(batch.table, [(inside_the_window, *r[1:]) for r in batch.rows])
    store.flush(reason="test")


def _diagnose_dir_rules(store: Storage):
    cfg = ProcdiagConfig(rules=RulesConfig(min_observation_hours=0.0))
    return Engine(cfg).run(store, only=_DIR_RULES)


class TestUnreadableWatchDirIsNotNoGrowth:
    def test_unreadable_only_watch_dir_leaves_the_rules_not_evaluated(self, tmp_path):
        store = make_db(tmp_path, hours=3)
        add_sys_baseline(store, hours=3)
        _scan_into(store, tmp_path / "not-mounted-yet")

        diag = _diagnose_dir_rules(store)
        store.close()

        assert diag.evaluated_rules == []
        assert sorted(r.rule_id for r in diag.uncovered_rules) == _DIR_RULES
        assert {r.reason for r in diag.uncovered_rules} == {CoverageGap.NOT_RECORDED}

    def test_a_readable_watch_dir_keeps_the_rules_evaluated(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "a.bin").write_bytes(b"x" * 1000)
        store = make_db(tmp_path, hours=3)
        add_sys_baseline(store, hours=3)
        _scan_into(store, data, tmp_path / "typo")

        diag = _diagnose_dir_rules(store)
        store.close()

        assert sorted(diag.evaluated_rules) == _DIR_RULES
        assert diag.uncovered_rules == []

    def test_a_host_without_watch_dirs_keeps_the_rules_evaluated(self, tmp_path):
        store = make_db(tmp_path, hours=3)
        add_sys_baseline(store, hours=3)

        diag = _diagnose_dir_rules(store)
        store.close()

        assert sorted(diag.evaluated_rules) == _DIR_RULES
        assert diag.uncovered_rules == []

    def test_check_names_the_unreadable_dir(self, tmp_path):
        db = tmp_path / "rec.db"
        missing = tmp_path / "not-mounted-yet"
        cfg = ProcdiagConfig(
            db_path=str(db),
            sampling=SamplingConfig(
                system_interval=0.1,
                cgroup_interval=0.3,
                process_interval=0.5,
                disk_interval=0.5,
                dir_scan_interval=5.0,
                watch_dirs=[WatchDir(path=str(missing))],
            ),
        )
        clock = FakeClock(0.0)
        rec = Recorder(
            cfg,
            db_path=db,
            ctx=RunContext(cfg, proc_root=_FIXTURES / "proc_tree"),
            samplers={"system": SystemSampler(), "disk": DiskSampler()},
            duration=1.0,
            clock=clock,
            sleep=_AdvancingSleep(clock),
        )
        assert rec.run() == 0

        with Storage(db, read_only=True) as store:
            diag = Engine(cfg).run(store, only=["CPU-SATURATION-01"])

        assert str(missing) in "\n".join(diag.quality.notes)


def _fake_sysfs(root: Path) -> Path:
    """sda (partitions sda1 and sda3) with LVM's dm-0 on sda3 and an attached loop0."""
    block = root / "block"
    for name in ("sda", "sda/sda1", "sda/sda3", "dm-0/slaves", "loop0/loop"):
        (block / name).mkdir(parents=True)
    (block / "sda" / "slaves").mkdir()
    (block / "dm-0" / "slaves" / "sda3").touch()
    by_number = root / "dev" / "block"
    by_number.mkdir(parents=True)
    for number, name in (("8:0", "sda"), ("252:0", "dm-0"), ("7:0", "loop0")):
        (by_number / number).symlink_to(f"../../block/{name}")
    return root


def _disk(read: int, write: int) -> SimpleNamespace:
    return SimpleNamespace(read_bytes=read, write_bytes=write)


class _Disks:
    """psutil.disk_io_counters over fixed per-device counters, summing the way
    psutil does: whole devices only, partitions excluded."""

    whole = ("sda", "dm-0", "loop0", "nvme0n1")

    def __init__(self, **counters: tuple[int, int]) -> None:
        self.counters = counters

    def bump(self, **by: tuple[int, int]) -> None:
        for name, (r, w) in by.items():
            old_r, old_w = self.counters[name]
            self.counters[name] = (old_r + r, old_w + w)

    def __call__(self, perdisk: bool = False, nowrap: bool = True):
        per = {n: _disk(*rw) for n, rw in self.counters.items()}
        if perdisk:
            return per
        whole = [c for n, c in per.items() if n in self.whole]
        return _disk(
            sum(c.read_bytes for c in whole), sum(c.write_bytes for c in whole)
        )


def _disk_delta(ctx: RunContext, disks: _Disks, monkeypatch, **bump) -> tuple[int, int]:
    monkeypatch.setattr(psutil, "disk_io_counters", disks)
    sampler = SystemSampler()
    sampler.setup(ctx)
    sampler.collect(ctx)
    disks.bump(**bump)
    row = dict(zip(TABLE_COLUMNS["sys_samples"], sampler.collect(ctx)[0].rows[0]))
    return row["disk_read_bytes"], row["disk_write_bytes"]


def _ctx(*, sys_root: Path, cgroup_root: Path = Path("/sys/fs/cgroup")) -> RunContext:
    return RunContext(
        ProcdiagConfig(),
        proc_root=_FIXTURES / "proc_tree",
        cgroup_root=cgroup_root,
        sys_root=sys_root,
    )


class TestDiskIoIsCountedOnce:
    def test_lvm_and_loop_do_not_add_to_the_disk_below(self, tmp_path, monkeypatch):
        ctx = _ctx(sys_root=_fake_sysfs(tmp_path / "sys"))
        disks = _Disks(
            **{
                "sda": (1_000_000, 2_000_000),
                "sda1": (10, 0),
                "sda3": (900_000, 1_900_000),
                "dm-0": (890_000, 1_890_000),
                "loop0": (50_000, 0),
            }
        )

        read, write = _disk_delta(
            ctx,
            disks,
            monkeypatch,
            **{
                "sda": (4096, 8192),
                "sda3": (4096, 8192),
                "dm-0": (4096, 8192),
                "loop0": (100, 0),
            },
        )

        assert (read, write) == (4096, 8192)

    def test_a_host_without_stacked_devices_counts_every_disk(
        self, tmp_path, monkeypatch
    ):
        sys_root = _fake_sysfs(tmp_path / "sys")
        (sys_root / "block" / "nvme0n1").mkdir()
        ctx = _ctx(sys_root=sys_root)
        disks = _Disks(
            **{"sda": (1000, 2000), "nvme0n1": (500, 700), "nvme0n1p1": (400, 600)}
        )

        read, write = _disk_delta(
            ctx,
            disks,
            monkeypatch,
            **{"sda": (4096, 0), "nvme0n1": (1024, 512), "nvme0n1p1": (1024, 512)},
        )

        assert (read, write) == (4096 + 1024, 512)

    def test_without_sysfs_the_psutil_total_is_kept(self, tmp_path, monkeypatch):
        ctx = _ctx(sys_root=tmp_path / "no-sysfs")
        disks = _Disks(**{"sda": (1000, 2000), "dm-0": (900, 1900)})

        read, write = _disk_delta(
            ctx, disks, monkeypatch, **{"sda": (4096, 8192), "dm-0": (4096, 8192)}
        )

        assert (read, write) == (8192, 16384)

    def test_cgroup_io_does_not_add_the_dm_device_to_its_disk(self, tmp_path):
        cgroup = tmp_path / "cgroup"
        (cgroup / "user.slice").mkdir(parents=True)
        (cgroup / "cgroup.controllers").write_text("cpu io memory pids\n")
        io_stat = cgroup / "user.slice" / "io.stat"

        def stat(sda: int, dm: int, loop: int) -> str:
            return (
                f"8:0 rbytes={sda} wbytes={sda * 2} rios=1 wios=1 dbytes=0 dios=0\n"
                f"252:0 rbytes={dm} wbytes={dm * 2} rios=1 wios=1 dbytes=0 dios=0\n"
                f"7:0 rbytes={loop} wbytes=0 rios=1 wios=0 dbytes=0 dios=0\n"
            )

        ids: dict[str, int] = {}
        sampler = CgroupSampler(
            resolve_cg=lambda p, ts: ids.setdefault(p, len(ids) + 1)
        )
        ctx = _ctx(cgroup_root=cgroup, sys_root=_fake_sysfs(tmp_path / "sys"))
        sampler.setup(ctx)
        io_stat.write_text(stat(1_000_000, 990_000, 5000))
        sampler.collect(ctx)
        io_stat.write_text(stat(1_004_096, 994_096, 5100))

        (row,) = [
            dict(zip(TABLE_COLUMNS["cg_samples"], r))
            for r in sampler.collect(ctx)[0].rows
        ]

        assert row["io_rbytes_delta"] == 4096
        assert row["io_wbytes_delta"] == 8192


class TestBindMountedFilesAreNotFileSystems:
    def test_only_directories_are_recorded_as_mount_points(self, tmp_path, monkeypatch):
        data = tmp_path / "data"
        data.mkdir()
        hosts = tmp_path / "hosts"
        hosts.write_text("127.0.0.1 localhost\n")

        def mount(path: Path) -> SimpleNamespace:
            return SimpleNamespace(
                device="/dev/mapper/vg-lv",
                mountpoint=str(path),
                fstype="ext4",
                opts="rw,relatime",
            )

        monkeypatch.setattr(
            psutil, "disk_partitions", lambda all=False: [mount(data), mount(hosts)]
        )

        rows = DiskSampler().collect(RunContext(ProcdiagConfig()))[0].rows

        assert [r[1] for r in rows] == [str(data)]


def _shm_is_a_separate_file_system() -> bool:
    try:
        return (
            os.access("/dev/shm", os.W_OK)
            and os.stat("/dev").st_dev != os.stat("/dev/shm").st_dev
        )
    except OSError:
        return False


class TestWatchDirStaysOnItsFileSystem:
    @pytest.mark.skipif(
        not sys.platform.startswith("linux") or not _shm_is_a_separate_file_system(),
        reason="/dev/shm is not a separate file system from /dev here",
    )
    def test_a_walk_does_not_count_a_file_system_mounted_below_it(self):
        payload = Path("/dev/shm") / f"procdiag-test-{uuid.uuid4().hex}"
        payload.write_bytes(b"x" * (8 * _MiB))
        try:
            size, is_estimate = _scan(Path("/dev"))["/dev"]
        finally:
            payload.unlink()

        assert is_estimate == 0
        assert size < 8 * _MiB
