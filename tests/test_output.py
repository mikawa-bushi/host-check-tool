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
