"""A DB placed on a disk that /etc/fstab lists but that is not mounted yet."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from procdiag import mounts
from procdiag.cli import cli
from procdiag.config import ProcdiagConfig
from procdiag.doctor import _check_db_disk
from procdiag.mounts import unmounted_disk


def _esc(path: Path) -> str:
    return str(path).replace(" ", "\\040")


@pytest.fixture()
def tables(tmp_path, monkeypatch):
    """Point procdiag at a fake fstab and mount table: tables(listed, mounted)."""

    def write(
        listed: list[Path],
        mounted: list[Path],
        extra: str = "",
        mounted_extra: str = "",
    ) -> None:
        fstab = tmp_path / "fstab"
        fstab.write_text(
            "# /etc/fstab: static file system information.\n"
            "UUID=root / ext4 errors=remount-ro 0 1\n"
            + "".join(f"/dev/sdb1 {_esc(p)} ext4 defaults,nofail 0 2\n" for p in listed)
            + "UUID=swap none swap sw 0 0\n"
            + extra
        )
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text(
            "".join(
                f"{n} 1 8:{n} / {_esc(p)} rw,relatime shared:{n} - ext4 /dev/sd{n} rw\n"
                for n, p in enumerate([Path("/"), *mounted], start=20)
            )
            + mounted_extra
        )
        monkeypatch.setattr(mounts, "FSTAB", str(fstab))
        monkeypatch.setattr(mounts, "MOUNTINFO", str(mountinfo))

    return write


@pytest.fixture()
def usb(tmp_path) -> Path:
    point = tmp_path / "usb"
    point.mkdir()
    return point


class TestUnmountedDisk:
    def test_db_in_a_folder_under_an_unmounted_disk(self, tables, usb):
        tables([usb], [])
        assert unmounted_disk(usb / "procdiag" / "procdiag.db") == str(usb)

    def test_db_directly_in_an_unmounted_mount_point(self, tables, usb):
        tables([usb], [])
        assert unmounted_disk(usb / "procdiag.db") == str(usb)

    def test_mounted_disk_is_fine(self, tables, usb):
        tables([usb], [usb])
        assert unmounted_disk(usb / "procdiag" / "procdiag.db") is None

    def test_a_commented_out_entry_does_not_count(self, tables, usb):
        tables([], [], extra=f"#/dev/sdb1 {usb} ext4 defaults,nofail 0 2\n")
        assert unmounted_disk(usb / "procdiag.db") is None
        tables([], [], extra=f"  # /dev/sdb1 {usb} ext4 defaults 0 2\n")
        assert unmounted_disk(usb / "procdiag.db") is None

    def test_mount_point_written_with_extra_slashes(self, tables, usb):
        tables([], [], extra=f"/dev/sdb1 /{usb}/ ext4 defaults,nofail 0 2\n")
        assert unmounted_disk(usb / "procdiag.db") == str(usb)
        tables([], [usb], extra=f"/dev/sdb1 /{usb}/ ext4 defaults,nofail 0 2\n")
        assert unmounted_disk(usb / "procdiag.db") is None

    def test_other_mount_points_are_never_looked_up(self, tables, tmp_path, usb):
        # A look-up of a hung network mount blocks; a NUL makes any look-up fail.
        broken = "/mnt/nas\\000share"
        tables(
            [usb],
            [],
            extra=f"server:/share {broken} nfs4 hard 0 0\n",
            mounted_extra=f"90 1 0:90 / {broken} rw - nfs4 server:/share rw\n",
        )
        assert unmounted_disk(usb / "procdiag.db") == str(usb)
        assert unmounted_disk(tmp_path / "data" / "procdiag.db") is None

    def test_db_outside_any_listed_disk(self, tables, tmp_path, usb):
        tables([usb], [])
        assert unmounted_disk(tmp_path / "data" / "procdiag.db") is None

    def test_mount_point_with_a_space(self, tables, tmp_path):
        point = tmp_path / "my usb"
        point.mkdir()
        tables([point], [])
        assert unmounted_disk(point / "procdiag.db") == str(point)
        tables([point], [point])
        assert unmounted_disk(point / "procdiag.db") is None

    def test_disk_nested_in_a_mounted_disk(self, tables, tmp_path):
        outer = tmp_path / "mnt"
        inner = outer / "usb"
        inner.mkdir(parents=True)
        tables([outer, inner], [outer])
        assert unmounted_disk(inner / "procdiag.db") == str(inner)
        assert unmounted_disk(outer / "other" / "procdiag.db") is None

    def test_path_through_a_symlink(self, tables, tmp_path, usb):
        link = tmp_path / "link"
        link.symlink_to(usb)
        tables([usb], [])
        assert unmounted_disk(link / "procdiag" / "procdiag.db") == str(usb)

    @pytest.mark.parametrize("missing", ["FSTAB", "MOUNTINFO"])
    def test_unreadable_table_does_not_block(
        self, tables, monkeypatch, tmp_path, usb, missing
    ):
        tables([usb], [])
        monkeypatch.setattr(mounts, missing, str(tmp_path / "absent"))
        assert unmounted_disk(usb / "procdiag.db") is None


class TestRecordWaitsForTheDisk:
    def test_record_does_not_write_to_the_system_disk(self, tables, usb):
        tables([usb], [])
        db = usb / "procdiag" / "procdiag.db"
        result = CliRunner().invoke(
            cli, ["record", "--db", str(db), "--duration", "2s"]
        )
        assert result.exit_code == 1, result.output
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 1 and lines[0].startswith("エラー:"), result.output
        assert str(usb) in lines[0] and "マウントされていません" in lines[0]
        assert not (usb / "procdiag").exists()

    def test_record_creates_a_missing_folder_on_a_mounted_disk(self, tables, usb):
        tables([usb], [usb])
        db = usb / "procdiag" / "procdiag.db"
        result = CliRunner().invoke(
            cli, ["record", "--db", str(db), "--duration", "1s"]
        )
        assert result.exit_code == 0, result.output
        assert db.exists()

    def test_doctor_reports_the_unmounted_disk(self, tables, usb):
        tables([usb], [])
        cfg = ProcdiagConfig(db_path=str(usb / "procdiag" / "procdiag.db"))
        result = _check_db_disk(cfg)
        assert result.status == "degraded"
        assert str(usb) in result.detail and "マウントされていません" in result.detail
