"""``atomic_output`` leaves a complete file or nothing."""

from __future__ import annotations

import os
import stat

import pytest

from procdiag.output import atomic_output


def test_the_file_appears_when_the_block_ends(tmp_path):
    path = tmp_path / "report.html"
    with atomic_output(path, encoding="utf-8") as fh:
        fh.write("全部")
        assert not path.exists()
    assert path.read_text(encoding="utf-8") == "全部"
    assert [p.name for p in tmp_path.iterdir()] == ["report.html"]


def test_a_failed_block_leaves_nothing_and_keeps_the_old_file(tmp_path):
    path = tmp_path / "report.html"
    path.write_text("old")
    with pytest.raises(OSError):
        with atomic_output(path) as fh:
            fh.write("half")
            raise OSError(28, "No space left on device")
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["report.html"]


def test_a_failed_block_creates_no_file(tmp_path):
    path = tmp_path / "report.html"
    with pytest.raises(RuntimeError):
        with atomic_output(path) as fh:
            fh.write("half")
            raise RuntimeError
    assert list(tmp_path.iterdir()) == []


def test_the_mode_of_the_replaced_file_is_kept(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("old")
    path.chmod(0o600)
    with atomic_output(path) as fh:
        fh.write("new")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_symlink_is_written_through(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("old")
    link = tmp_path / "latest.txt"
    link.symlink_to(real)
    with atomic_output(link) as fh:
        fh.write("new")
    assert link.is_symlink()
    assert real.read_text() == "new"


def test_a_device_is_opened_as_it_is():
    with atomic_output(os.devnull) as fh:
        fh.write("discarded")
    assert stat.S_ISCHR(os.stat(os.devnull).st_mode)


def test_a_name_near_the_file_name_limit_can_be_written(tmp_path):
    path = tmp_path / ("r" * 250 + ".txt")
    with atomic_output(path, encoding="utf-8") as fh:
        fh.write("全部")
    assert path.read_text(encoding="utf-8") == "全部"
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


def test_a_hard_linked_file_is_written_through(tmp_path):
    real = tmp_path / "report.txt"
    real.write_text("old")
    link = tmp_path / "latest.txt"
    os.link(real, link)
    with atomic_output(real) as fh:
        fh.write("new")
    assert link.read_text() == "new"
    assert os.stat(real).st_ino == os.stat(link).st_ino


def test_the_data_is_synced_before_the_file_replaces_the_old_one(tmp_path, monkeypatch):
    path = tmp_path / "report.txt"
    path.write_text("old")
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1]
    )
    with atomic_output(path) as fh:
        fh.write("new")
    assert events == ["fsync", "replace"]
    assert path.read_text() == "new"
