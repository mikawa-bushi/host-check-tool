"""A command that cannot do its job says why in one line and exits 4.

Exit 4 is the 前提条件エラー code: a bad DB, a bad output path or a bad argument.
It must not read as a diagnosis (1 = problems found, 2 = info only) and must not
be a traceback, and a failed command must not leave a half-written output file.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from synth import add_sys_baseline, make_db

from procdiag.cli import cli
from procdiag.storage import Storage

needs_non_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can write to a read-only directory"
)


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def good_db(tmp_path) -> Path:
    store = make_db(tmp_path, hours=1)
    add_sys_baseline(store, hours=1)
    store.close()
    path = tmp_path / "synth.db"
    assert not Path(f"{path}-wal").exists()
    return path


def _lines(result) -> list[str]:
    return [ln for ln in result.output.splitlines() if ln.strip()]


def _assert_refused(result) -> None:
    assert result.exit_code == 4, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    lines = _lines(result)
    assert len(lines) == 1 and lines[0].startswith("エラー:"), result.output


def _make_bad_dbs(tmp_path: Path, good_db: Path) -> dict[str, Path]:
    newer = tmp_path / "newer.db"
    shutil.copy(good_db, newer)
    with sqlite3.connect(newer) as conn:
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    truncated = tmp_path / "truncated.db"
    data = good_db.read_bytes()
    truncated.write_bytes(data[: len(data) // 2])
    not_a_db = tmp_path / "not-a-db.db"
    not_a_db.write_text("config = {}\n" * 200, encoding="utf-8")
    zero = tmp_path / "zero.db"
    zero.touch()
    adir = tmp_path / "adir"
    adir.mkdir()
    return {
        "newer": newer,
        "truncated": truncated,
        "not-sqlite": not_a_db,
        "zero-byte": zero,
        "directory": adir,
    }


BAD_DB_KINDS = ["newer", "truncated", "not-sqlite", "zero-byte", "directory"]


class TestBadDb:
    @pytest.mark.parametrize("kind", BAD_DB_KINDS)
    @pytest.mark.parametrize("command", ["check", "status", "export", "bundle"])
    def test_read_commands_refuse_a_bad_db(
        self, runner, tmp_path, good_db, command, kind
    ):
        bad = _make_bad_dbs(tmp_path, good_db)[kind]
        args = [command, "--db", str(bad)]
        if command == "bundle":
            args += ["--out", str(tmp_path / "out" / "b.tar.gz")]
        result = runner.invoke(cli, args)
        _assert_refused(result)
        assert str(bad) in result.output
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize("kind", ["newer", "not-sqlite", "directory"])
    @pytest.mark.parametrize("command", ["prune", "record"])
    def test_write_commands_refuse_a_bad_db(
        self, runner, tmp_path, good_db, command, kind
    ):
        bad = _make_bad_dbs(tmp_path, good_db)[kind]
        args = [command, "--db", str(bad)]
        if command == "record":
            args += ["--duration", "2s"]
        result = runner.invoke(cli, args)
        assert result.exit_code == 4, result.output
        assert "エラー:" in result.output
        assert str(bad) in result.output

    @pytest.mark.parametrize("kind", ["truncated", "not-sqlite", "zero-byte"])
    def test_compare_refuses_a_bad_db(self, runner, tmp_path, good_db, kind):
        bad = _make_bad_dbs(tmp_path, good_db)[kind]
        result = runner.invoke(cli, ["compare", str(bad), str(good_db)])
        _assert_refused(result)

    def test_check_json_prints_nothing_on_stdout(self, runner, tmp_path, good_db):
        bad = _make_bad_dbs(tmp_path, good_db)["truncated"]
        result = runner.invoke(cli, ["check", "--db", str(bad), "--format", "json"])
        _assert_refused(result)
        assert "{" not in result.output


@needs_non_root
class TestReadOnlyDirectory:
    """A root recorder's DB sits in a directory the operator cannot write."""

    @pytest.fixture()
    def locked_db(self, tmp_path, good_db):
        site = tmp_path / "site"
        site.mkdir()
        locked = site / "procdiag.db"
        shutil.copy(good_db, locked)
        locked.chmod(0o444)
        site.chmod(0o555)
        yield locked
        site.chmod(0o755)

    def test_status_reads_the_recording(self, runner, locked_db):
        result = runner.invoke(
            cli, ["status", "--db", str(locked_db), "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        assert '"sys_samples": 60' in result.output

    def test_check_diagnoses_it(self, runner, locked_db):
        result = runner.invoke(
            cli, ["check", "--db", str(locked_db), "--format", "json"]
        )
        assert result.exit_code in (0, 1, 2), result.output
        assert json.loads(result.output)["window"] is not None

    def test_export_writes_the_rows(self, runner, locked_db):
        result = runner.invoke(
            cli, ["export", "--db", str(locked_db), "--rollup", "raw"]
        )
        assert result.exit_code == 0, result.output
        assert len(result.output.strip().splitlines()) == 61

    def test_bundle_packs_it(self, runner, locked_db, tmp_path):
        out = tmp_path / "b.tar.gz"
        result = runner.invoke(
            cli, ["bundle", "--db", str(locked_db), "--out", str(out)]
        )
        assert result.exit_code == 0, result.output
        with tarfile.open(out) as tar:
            assert any(n.endswith("/procdiag.db") for n in tar.getnames())

    def test_a_wal_still_holding_data_is_not_read_around(
        self, runner, tmp_path, good_db
    ):
        live = tmp_path / "live"
        live.mkdir()
        db = live / "procdiag.db"
        shutil.copy(good_db, db)
        writer = Storage(db)
        writer.conn.execute("DELETE FROM sys_samples")
        writer.conn.commit()
        assert Path(f"{db}-wal").stat().st_size > 0
        site = tmp_path / "copy"
        site.mkdir()
        shutil.copy(db, site / "procdiag.db")
        shutil.copy(f"{db}-wal", site / "procdiag.db-wal")
        writer.close()
        site.chmod(0o555)
        try:
            result = runner.invoke(cli, ["status", "--db", str(site / "procdiag.db")])
        finally:
            site.chmod(0o755)
        _assert_refused(result)


class TestOutputPath:
    def test_check_output_in_a_missing_directory(
        self, runner, tmp_path, good_db, monkeypatch
    ):
        monkeypatch.setattr(
            "procdiag.cli._diagnose",
            lambda *a, **k: pytest.fail("diagnosed before checking the output"),
        )
        out = tmp_path / "usb-not-mounted" / "report.html"
        result = runner.invoke(cli, ["check", "--db", str(good_db), "-o", str(out)])
        _assert_refused(result)
        assert str(out) in result.output

    def test_compare_output_in_a_missing_directory(self, runner, tmp_path, good_db):
        after = tmp_path / "after.db"
        shutil.copy(good_db, after)
        out = tmp_path / "nodir" / "c.json"
        result = runner.invoke(
            cli,
            ["compare", str(good_db), str(after), "--format", "json"]
            + ["--output", str(out)],
        )
        _assert_refused(result)
        assert str(out) in result.output

    def test_export_out_in_a_missing_directory(self, runner, tmp_path, good_db):
        out = tmp_path / "nodir" / "x.csv"
        result = runner.invoke(cli, ["export", "--db", str(good_db), "--out", str(out)])
        _assert_refused(result)
        assert not out.parent.exists()

    @needs_non_root
    def test_check_output_in_a_read_only_directory(self, runner, tmp_path, good_db):
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        try:
            result = runner.invoke(
                cli, ["check", "--db", str(good_db), "-o", str(ro / "r.json")]
            )
        finally:
            ro.chmod(0o755)
        _assert_refused(result)

    def test_bundle_out_is_an_existing_directory(self, runner, tmp_path, good_db):
        usb = tmp_path / "usb"
        usb.mkdir()
        result = runner.invoke(cli, ["bundle", "--db", str(good_db), "--out", str(usb)])
        _assert_refused(result)
        assert list(usb.iterdir()) == []

    def test_bundle_still_creates_a_missing_output_folder(
        self, runner, tmp_path, good_db
    ):
        out = tmp_path / "new" / "folder" / "b.tar.gz"
        result = runner.invoke(cli, ["bundle", "--db", str(good_db), "--out", str(out)])
        assert result.exit_code == 0, result.output
        assert out.is_file()

    @needs_non_root
    def test_bundle_out_below_a_read_only_directory(self, runner, tmp_path, good_db):
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        try:
            result = runner.invoke(
                cli,
                ["bundle", "--db", str(good_db), "--out", str(ro / "sub" / "b.tar.gz")],
            )
        finally:
            ro.chmod(0o755)
        _assert_refused(result)

    def test_a_full_disk_is_reported_not_crashed(self, runner, good_db):
        if not os.path.exists("/dev/full"):
            pytest.skip("no /dev/full")
        result = runner.invoke(
            cli, ["check", "--db", str(good_db), "--format", "json", "-o", "/dev/full"]
        )
        _assert_refused(result)

    def test_bundle_failing_midway_leaves_no_archive(
        self, runner, tmp_path, good_db, monkeypatch
    ):
        out = tmp_path / "b.tar.gz"
        out.write_bytes(b"previous bundle")

        def boom(self, *args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tarfile.TarFile, "add", boom)
        result = runner.invoke(cli, ["bundle", "--db", str(good_db), "--out", str(out)])
        _assert_refused(result)
        assert out.read_bytes() == b"previous bundle"
        assert sorted(p.name for p in tmp_path.iterdir() if "b.tar" in p.name) == [
            "b.tar.gz"
        ]


class TestOldDb:
    """A recording made by procdiag 0.3 or older has no newer tables."""

    @pytest.fixture(params=[("sessions",), ("sessions", "thermal_samples")])
    def old_db(self, request, tmp_path, good_db):
        old = tmp_path / "old.db"
        shutil.copy(good_db, old)
        with sqlite3.connect(old) as conn:
            for table in request.param:
                conn.execute(f"DROP TABLE {table}")
            conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        return old

    def test_status_reads_it(self, runner, old_db):
        result = runner.invoke(cli, ["status", "--db", str(old_db), "--format", "json"])
        assert result.exit_code == 0, result.output
        assert '"sessions": 0' in result.output

    def test_bundle_packs_it(self, runner, old_db, tmp_path):
        out = tmp_path / "b.tar.gz"
        result = runner.invoke(cli, ["bundle", "--db", str(old_db), "--out", str(out)])
        assert result.exit_code == 0, result.output
        with tarfile.open(out) as tar:
            assert any(n.endswith("/status.json") for n in tar.getnames())


class TestUsageErrors:
    @pytest.mark.parametrize(
        "args",
        [
            ["chekc"],
            ["check", "--fromat", "json"],
            ["check", "--format", "xml"],
            ["check", "--since"],
            ["check", "extra-argument"],
            ["quick", "--duraton", "5s"],
            ["compare", "--at"],
            ["compare", "--format", "html"],
            ["service", "print", "--bogus"],
            ["--bogus"],
        ],
    )
    def test_a_command_line_mistake_exits_4(self, runner, args):
        result = runner.invoke(cli, args)
        assert result.exit_code == 4, result.output
        assert "Error:" in result.output

    def test_record_duration_is_refused_like_quick(self, runner, tmp_path):
        db = tmp_path / "r.db"
        result = runner.invoke(cli, ["record", "--db", str(db), "--duration", "90"])
        assert result.exit_code == 4, result.output
        assert "エラー:" in result.output
        assert not db.exists()

    def test_help_still_exits_0(self, runner):
        assert runner.invoke(cli, ["check", "--help"]).exit_code == 0


class TestPrune:
    @pytest.mark.parametrize(
        "option, value",
        [("--raw", "2days"), ("--raw", "48"), ("--rollup", "3mo")],
    )
    def test_a_bad_retention_is_refused_before_deleting(
        self, runner, good_db, option, value
    ):
        before = (
            sqlite3.connect(good_db)
            .execute("SELECT COUNT(*) FROM sys_samples")
            .fetchone()
        )
        result = runner.invoke(cli, ["prune", "--db", str(good_db), option, value])
        _assert_refused(result)
        assert option in result.output and value in result.output
        after = (
            sqlite3.connect(good_db)
            .execute("SELECT COUNT(*) FROM sys_samples")
            .fetchone()
        )
        assert after == before


class TestInit:
    def test_init_on_a_file(self, runner, tmp_path):
        target = tmp_path / "afile"
        target.write_text("x")
        result = runner.invoke(cli, ["init", str(target)])
        _assert_refused(result)

    @needs_non_root
    def test_init_in_a_read_only_directory(self, runner, tmp_path):
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        try:
            result = runner.invoke(cli, ["init", str(ro / "site")])
        finally:
            ro.chmod(0o755)
        _assert_refused(result)

    def test_a_rerun_does_not_claim_to_create_data_again(self, runner, tmp_path):
        site = tmp_path / "site"
        first = runner.invoke(cli, ["init", str(site)])
        assert first.exit_code == 0
        assert "data/ ディレクトリを生成しました" in first.output
        second = runner.invoke(cli, ["init", str(site)])
        assert second.exit_code == 0
        assert "data/ ディレクトリを生成しました" not in second.output
        assert "data/ ディレクトリはすでに存在します" in second.output


class TestClosedPipe:
    def test_export_into_a_closed_pipe_is_silent(self, tmp_path, good_db):
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "procdiag.cli", "export", "--db", str(good_db)],
                stdout=write_end,
                stderr=subprocess.PIPE,
                cwd=tmp_path,
                timeout=60,
            )
        finally:
            os.close(write_end)
        assert proc.stderr == b""
        assert proc.returncode in (-13, 141), proc.returncode
