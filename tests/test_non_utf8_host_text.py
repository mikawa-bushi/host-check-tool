"""Host text that is not valid UTF-8 must not stop the recording."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from procdiag.config import ProcdiagConfig, SamplingConfig
from procdiag.core.sampler import RunContext
from procdiag.recorder import Recorder

_SET_COMM = (
    "import os, time; "
    "open('/proc/self/comm', 'wb').write(bytes.fromhex(os.environ['COMM'])); "
    "print('ready', flush=True); time.sleep(60)"
)


@pytest.fixture
def spawn():
    """Start a long-running child and wait until it prints ``ready``."""
    children: list[subprocess.Popen] = []

    def _spawn(*argv: str | bytes, comm: bytes = b"") -> int:
        env = {**os.environ, "COMM": comm.hex()}
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, env=env)
        children.append(child)
        assert child.stdout is not None
        assert child.stdout.readline() == b"ready\n"
        return child.pid

    yield _spawn
    for child in children:
        child.kill()
        child.wait()
        assert child.stdout is not None
        child.stdout.close()


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


@pytest.fixture
def record():
    """Record 3 s of fake time with the real samplers; return a DB connection."""
    connections: list[sqlite3.Connection] = []

    def _record(db: Path, cgroup_root: Path | None = None) -> sqlite3.Connection:
        fake = _FakeTime()
        config = ProcdiagConfig(sampling=SamplingConfig(process_interval=1.0))
        ctx = RunContext(config, cgroup_root=cgroup_root) if cgroup_root else None
        rec = Recorder(
            config,
            db_path=db,
            duration=3.0,
            ctx=ctx,
            clock=fake.clock,
            sleep=fake.sleep,
        )
        assert rec.run() == 0
        connections.append(sqlite3.connect(db))
        return connections[-1]

    yield _record
    for con in connections:
        con.close()


def _samples_by_identity(con: sqlite3.Connection, pid: int) -> list[tuple]:
    return con.execute(
        "SELECT s.proc_id, r.name, r.cmdline_sample, COUNT(*) FROM proc_samples s "
        "JOIN proc_registry r USING (proc_id) WHERE s.pid = ? GROUP BY s.proc_id",
        (pid,),
    ).fetchall()


def test_processes_with_non_utf8_names_are_recorded_like_any_other(
    tmp_path: Path, spawn, record
):
    def set_comm(comm: bytes) -> int:
        return spawn(sys.executable, "-c", _SET_COMM, comm=comm)

    sjis_a = set_comm("測定".encode("shift_jis") + b"_tool")
    sjis_b = set_comm("測定".encode("shift_jis") + b"_tool")
    sjis_other = set_comm("試験".encode("shift_jis") + b"_tool")
    # The kernel keeps 15 bytes of a comm, which cuts this name inside "監".
    cut_utf8 = set_comm("1号機温度監視".encode())
    sjis_log = os.fsencode(tmp_path) + b"/" + "テスト.log".encode("shift_jis")
    Path(os.fsdecode(sjis_log)).write_text("ready\n")
    sjis_argv = spawn("tail", "-f", sjis_log)
    children = (sjis_a, sjis_b, sjis_other, cut_utf8, sjis_argv)

    con = record(tmp_path / "rec.db")
    ticks = con.execute("SELECT COUNT(*) FROM proc_counts").fetchone()[0]
    assert ticks >= 2
    others = con.execute(
        "SELECT COUNT(DISTINCT pid) FROM proc_samples WHERE pid NOT IN (?, ?, ?, ?, ?)",
        children,
    ).fetchone()[0]
    assert others > 0

    [(id_a, name_a, _, n_a)] = _samples_by_identity(con, sjis_a)
    [(id_b, _, _, n_b)] = _samples_by_identity(con, sjis_b)
    [(id_other, name_other, _, n_other)] = _samples_by_identity(con, sjis_other)
    assert name_a == r"\x91\xaa\x92\xe8_tool"
    assert name_other == r"\x8e\x8e\x8c\xb1_tool"
    assert id_a == id_b != id_other
    assert n_a == n_b == n_other == ticks

    [(_, name, _, n)] = _samples_by_identity(con, cut_utf8)
    assert name == r"1号機温度\xe7\x9b"
    assert n == ticks

    [(_, _, sample, n)] = _samples_by_identity(con, sjis_argv)
    assert sample == rf"tail -f {tmp_path}/\x83e\x83X\x83g.log"
    assert n == ticks

    restarted = set_comm("測定".encode("shift_jis") + b"_tool")
    con = record(tmp_path / "rec.db")
    [(id_restarted, _, _, _)] = _samples_by_identity(con, restarted)
    assert id_restarted == id_a


def test_a_mount_point_with_non_utf8_bytes_does_not_stop_writes(
    tmp_path: Path, monkeypatch, record
):
    mount = os.fsencode(tmp_path) + b"/" + "共有データ".encode("shift_jis")
    os.mkdir(mount)
    real_partitions = psutil.disk_partitions

    def with_sjis_mount(all: bool = False) -> list:
        extra = SimpleNamespace(
            device="/dev/sdb1",
            mountpoint=os.fsdecode(mount),
            fstype="ext4",
            opts="rw,relatime",
        )
        return [*real_partitions(all), extra]

    monkeypatch.setattr(psutil, "disk_partitions", with_sjis_mount)

    con = record(tmp_path / "rec.db")
    mountpoints = {r[0] for r in con.execute("SELECT mountpoint FROM disk_samples")}
    assert rf"{tmp_path}/\x8b\xa4\x97L\x83f\x81[\x83^" in mountpoints
    assert con.execute("SELECT COUNT(*) FROM sys_samples").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM proc_samples").fetchone()[0] > 0


def test_a_cgroup_unit_with_non_utf8_bytes_is_recorded(tmp_path: Path, record):
    root = tmp_path / "cgroup"
    unit = os.fsencode(root) + b"/system.slice/" + "測定".encode("shift_jis")
    os.makedirs(unit + b".service")
    (root / "cgroup.controllers").write_text("cpu memory\n")
    Path(os.fsdecode(unit + b".service/memory.current")).write_text("4096\n")

    con = record(tmp_path / "rec.db", cgroup_root=root)
    rows = con.execute(
        "SELECT r.unit_path, s.mem_current FROM cg_samples s "
        "JOIN cgroup_registry r USING (cg_id)"
    ).fetchall()
    assert rows
    assert set(rows) == {(r"system.slice/\x91\xaa\x92\xe8.service", 4096)}
