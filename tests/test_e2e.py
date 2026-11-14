"""End-to-end integration tests for procdiag.

Exercises the full ``init → record → check`` pipeline using the real
``procdiag`` entry point (subprocess) so that signal handling, process
lifecycle, and DB I/O are all exercised in a realistic way.

All tests are tagged ``@pytest.mark.e2e``.  They require the real ``.venv``
environment, write to ``tmp_path``, and may take 10-15 seconds each because
``record`` actually runs for a few seconds collecting real system metrics.

Test cases
----------
1. ``test_full_pipeline`` — init → record 8 s → status → check (terminal,
   then ``-o report.html`` / ``-o report.json``).
   Assertions: sys_samples > 0, check exits 0-2, report.html/report.json
   generated with no external references.

2. ``test_sigterm_shutdown`` — record launched, SIGTERM after 2 s → exit 0,
   ops_events contains a "stopped" row.

3. ``test_self_cpu_under_budget`` — after an 8-second record run the
   steady-state self_cpu (startup transient excluded) must be below 10 %.

4. ``test_quick_runs_all_three_steps`` — ``quick --duration 2s`` prints the
   three step headings and leaves procdiag.db and report.html in ``--out``.

5. ``test_quick_ctrl_c_during_recording_still_diagnoses`` — SIGINT during the
   recording ends it early; the diagnosis and report.html still follow.

6. ``test_record_keeps_kernel_errors_logged_since_its_last_read`` and
   ``test_record_resumes_the_kernel_log_after_a_restart_and_a_reboot`` — with a
   stand-in ``journalctl``, KERNEL-ERROR-01 counts each kernel error once,
   including the ones logged after the last read and while record was stopped.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROCDIAG = str(Path(sys.executable).parent / "procdiag")


def _write_config(config_path: Path, db_path: Path) -> None:
    """Write a minimal config.py with fast intervals for E2E tests."""
    config_path.write_text(
        textwrap.dedent(f"""\
            from procdiag.config import (
                ProcdiagConfig, SamplingConfig, TriggerConfig,
                BudgetConfig, RetentionConfig, RulesConfig, ReportConfig,
            )

            config = ProcdiagConfig(
                db_path={str(db_path)!r},
                sampling=SamplingConfig(
                    system_interval=0.5,
                    cgroup_interval=1.0,
                    process_interval=2.0,
                    disk_interval=5.0,
                    dir_scan_interval=3600.0,
                    watch_dirs=[],
                    cmdline_normalize=True,
                ),
                trigger=TriggerConfig(
                    psi_cpu_some10=20.0,
                    psi_mem_some10=5.0,
                    psi_mem_full10=1.0,
                    psi_io_some10=30.0,
                    spike_cooldown_sec=30,
                    spike_storm_threshold=10,
                ),
                budget=BudgetConfig(
                    overhead_budget_pct=5.0,
                    throttle_factor=1.5,
                    throttle_max_interval=60.0,
                ),
                retention=RetentionConfig(raw="48h", rollup="90d"),
                rules=RulesConfig(min_observation_hours=0.001),
                report=ReportConfig(lang="ja"),
            )
            """),
        encoding="utf-8",
    )


def _run(
    args: list[str],
    *,
    timeout: int = 60,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Run a procdiag sub-command and return the CompletedProcess."""
    return subprocess.run(
        [_PROCDIAG, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _count_rows(db_path: Path, table: str) -> int:
    """Return the row count of *table* in the SQLite DB at *db_path*."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _ops_events(db_path: Path) -> list[str]:
    """Return all 'kind' values from ops_events."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT kind FROM ops_events").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _self_cpu_avg(db_path: Path, skip_first_sec: float = 2.0) -> Optional[float]:
    """Return steady-state average self_cpu from sys_samples.

    The first samples measure the startup transient (registry warm-up over a
    sub-second wall window) and can read 50-150 % on a busy box; the overhead
    budget is a steady-state concept, so the warm-up window is excluded —
    same convention as ``bin/load_test.py``.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT self_cpu FROM sys_samples WHERE self_cpu IS NOT NULL "
            "AND ts >= (SELECT MIN(ts) FROM sys_samples) + ? ORDER BY ts",
            (skip_first_sec,),
        ).fetchall()
        vals = [r[0] for r in rows]
        return sum(vals) / len(vals) if vals else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 1: full pipeline
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_full_pipeline(tmp_path: Path) -> None:
    """init → record 8 s → status → check → check -o report.html / report.json.

    Verifies:
    - sys_samples has rows after recording
    - check exits with code 0, 1, or 2 (Finding presence is environment-dependent)
    - report.html and report.json are generated
    - report.html contains no external (http/https/cdn) references
    """
    config_path = tmp_path / "config.py"
    db_path = tmp_path / "data" / "procdiag.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _write_config(config_path, db_path)

    # --- record ---------------------------------------------------------------
    result = _run(
        [
            "record",
            "--duration",
            "8s",
            "--config",
            str(config_path),
            "--db",
            str(db_path),
        ],
        timeout=30,
    )
    assert result.returncode == 0, (
        f"record failed (code {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # --- verify DB contents ---------------------------------------------------
    n_sys = _count_rows(db_path, "sys_samples")
    assert (
        n_sys > 0
    ), f"sys_samples is empty after 8-second record run (got {n_sys} rows)"

    # --- status ---------------------------------------------------------------
    st = _run(["status", "--db", str(db_path)], timeout=15)
    assert st.returncode == 0, f"status failed: {st.stderr}"
    # Status output should mention a sample count.
    assert (
        "sys=" in st.stdout or "sys_samples" in st.stdout
    ), f"status output unexpected:\n{st.stdout}"

    # --- check ----------------------------------------------------------------
    ck = _run(
        ["check", "--db", str(db_path), "--config", str(config_path)],
        timeout=30,
    )
    assert ck.returncode in (
        0,
        1,
        2,
    ), f"check returned unexpected code {ck.returncode}:\n{ck.stderr}"

    # --- check -o report.html / report.json -----------------------------------
    html_out = tmp_path / "report.html"
    json_out = tmp_path / "report.json"
    for out in (html_out, json_out):
        rp = _run(
            ["check", "--db", str(db_path), "--config", str(config_path)]
            + ["-o", str(out)],
            timeout=30,
        )
        assert rp.returncode in (0, 1, 2), f"check -o {out.name} failed: {rp.stderr}"
    assert html_out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "findings" in json.loads(json_out.read_text(encoding="utf-8"))

    # report.html must contain no external URL references (no CDN, no http://)
    html_text = html_out.read_text(encoding="utf-8")
    external_refs = re.findall(r'(?:src|href)\s*=\s*["\']https?://', html_text)
    assert not external_refs, (
        f"report.html contains external references (violates closed-network rule): "
        f"{external_refs}"
    )


# ---------------------------------------------------------------------------
# Test 2: SIGTERM triggers graceful shutdown
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_sigterm_shutdown(tmp_path: Path) -> None:
    """Launch record, send SIGTERM after 2 s, assert exit 0 and stopped event.

    This exercises signal handling which is not reachable via CliRunner.
    """
    config_path = tmp_path / "config.py"
    db_path = tmp_path / "data" / "procdiag.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _write_config(config_path, db_path)

    proc = subprocess.Popen(
        [
            _PROCDIAG,
            "record",
            "--config",
            str(config_path),
            "--db",
            str(db_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Allow at least one full system sample tick (interval=0.2 s → wait 2 s).
    time.sleep(2.0)

    # Send SIGTERM and wait for clean exit.
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("procdiag record did not exit within 10 s after SIGTERM")

    assert proc.returncode == 0, (
        f"Expected exit code 0 after SIGTERM, got {proc.returncode}.\n"
        f"stderr: {proc.stderr.read() if proc.stderr else ''}"
    )

    # ops_events must contain a "stopped" row.
    events = _ops_events(db_path)
    assert "stopped" in events, (
        f"ops_events does not contain 'stopped' after SIGTERM.\n" f"events: {events}"
    )


# ---------------------------------------------------------------------------
# Test 3: self-CPU overhead stays below relaxed threshold
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_self_cpu_under_budget(tmp_path: Path) -> None:
    """Steady-state self_cpu (startup excluded) must stay < 10 % over 8 s.

    The design target is 1 % at steady state; the relaxed guard keeps this a
    wiring check that stays green on a busy CI machine.
    """
    config_path = tmp_path / "config.py"
    db_path = tmp_path / "data" / "procdiag.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _write_config(config_path, db_path)

    result = _run(
        [
            "record",
            "--duration",
            "8s",
            "--config",
            str(config_path),
            "--db",
            str(db_path),
        ],
        timeout=30,
    )
    assert result.returncode == 0, f"record failed: {result.stderr}"

    avg = _self_cpu_avg(db_path)
    assert (
        avg is not None
    ), "self_cpu column is all NULL — SelfMeter not wired to SystemSampler"
    # Relaxed threshold for short E2E runs: design target is 1% at steady state,
    # but startup transients and scheduler overhead on faster intervals can push
    # short runs higher.  10% is still meaningful as a wiring check.
    assert avg < 10.0, (
        f"procdiag self-CPU overhead too high: avg={avg:.2f}% >= 10%.\n"
        "Check SelfMeter wiring and sampling intervals."
    )


# ---------------------------------------------------------------------------
# quick: snapshot, a short record and the diagnosis in one command
# ---------------------------------------------------------------------------

_QUICK_HEADINGS = ("[1/3]", "[2/3]", "[3/3]")


def _assert_quick_finished(out_dir: Path, stdout: str, returncode: int) -> None:
    assert returncode in (0, 1, 2), f"quick exited {returncode}:\n{stdout}"
    positions = [stdout.find(h) for h in _QUICK_HEADINGS]
    assert -1 not in positions and positions == sorted(positions), stdout
    assert _count_rows(out_dir / "procdiag.db", "sys_samples") > 0
    report = (out_dir / "report.html").read_text(encoding="utf-8")
    assert report.startswith("<!doctype html>")


@pytest.mark.e2e
def test_quick_runs_all_three_steps(tmp_path: Path) -> None:
    out_dir = tmp_path / "q"
    config_path = tmp_path / "config.py"
    _write_config(config_path, tmp_path / "unused.db")
    result = _run(
        ["quick", "--duration", "2s", "--out", str(out_dir), "--no-color"]
        + ["--config", str(config_path)],
        timeout=60,
    )
    _assert_quick_finished(out_dir, result.stdout, result.returncode)
    assert str(out_dir / "report.html") in result.stdout


@pytest.mark.e2e
def test_quick_ctrl_c_during_recording_still_diagnoses(tmp_path: Path) -> None:
    """SIGINT while recording ends the recording early, not the command."""
    out_dir = tmp_path / "q"
    config_path = tmp_path / "config.py"
    _write_config(config_path, tmp_path / "unused.db")
    proc = subprocess.Popen(
        [_PROCDIAG, "quick", "--duration", "10m", "--out", str(out_dir), "--no-color"]
        + ["--config", str(config_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    seen = []
    assert proc.stdout is not None
    for line in proc.stdout:
        seen.append(line)
        if line.startswith("[2/3]"):
            break
    # The recorder installs its handler after setting up the samplers.
    time.sleep(2.0)
    proc.send_signal(signal.SIGINT)
    try:
        rest, _err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("quick did not finish within 60 s after SIGINT")

    _assert_quick_finished(out_dir, "".join(seen) + rest, proc.returncode)


# ---------------------------------------------------------------------------
# Test 6: kernel errors reach KERNEL-ERROR-01 across stops and reboots
# ---------------------------------------------------------------------------

# A stand-in journalctl.  $FAKE_JOURNAL holds one JSON object per kernel line
# (boot, ts, message) and $FAKE_BOOT names the running boot.  As in systemd,
# "-k" shows only the running boot.  A cursor is the line number.  Each call is
# appended to $FAKE_JOURNAL.calls after its output, so a test can wait for a
# read to finish.
_FAKE_JOURNALCTL = """\
import json, os, sys

args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1] if name in args else None


path = os.environ["FAKE_JOURNAL"]
with open(path, encoding="utf-8") as f:
    shown = list(enumerate(map(json.loads, f), 1))
if "-k" in args:
    shown = [(i, e) for i, e in shown if e["boot"] == os.environ["FAKE_BOOT"]]
if opt("--after-cursor"):
    after = int(opt("--after-cursor").split("i=")[1])
    shown = [(i, e) for i, e in shown if i > after]
elif opt("--since"):
    since = float(opt("--since").lstrip("@"))
    shown = [(i, e) for i, e in shown if e["ts"] >= since]
if opt("-n"):
    shown = shown[-int(opt("-n")) :]
for i, e in shown:
    print(f"{e['ts']:.6f} edge kernel: {e['message']}")
if shown and "--show-cursor" in args:
    print(f"-- cursor: s=fake;i={shown[-1][0]}")
sys.stdout.flush()
with open(path + ".calls", "a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\\n")
"""

_BOOT_LINE = "Linux version 6.8.0-fake (gcc 13.2.0) #1 SMP PREEMPT_DYNAMIC"


def _io_error(sector: int) -> str:
    return (
        f"I/O error, dev sda, sector {sector} op 0x1:(WRITE) flags 0x800 "
        "phys_seg 1 prio class 2"
    )


class _KernelJournal:
    """The stand-in journal; ``log`` appends a kernel line logged now."""

    def __init__(self, tmp_path: Path) -> None:
        self._bin = tmp_path / "bin"
        self._bin.mkdir()
        script = tmp_path / "fake_journalctl.py"
        script.write_text(_FAKE_JOURNALCTL, encoding="utf-8")
        wrapper = self._bin / "journalctl"
        wrapper.write_text(
            f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        self.path = tmp_path / "journal.jsonl"
        self.path.touch()
        self._calls = Path(f"{self.path}.calls")

    def log(self, boot: str, message: str) -> None:
        line = {"boot": boot, "ts": time.time(), "message": message}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def env(self, boot: str) -> dict:
        return {
            **os.environ,
            "PATH": f"{self._bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_JOURNAL": str(self.path),
            "FAKE_BOOT": boot,
        }

    def wait_for_a_read(self, proc: subprocess.Popen) -> None:
        """Wait until a read of the new lines (not a probe of the newest) ends."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and proc.poll() is None:
            calls = self._calls.read_text() if self._calls.exists() else ""
            if any(
                "--show-cursor" in call and "-n" not in call.split()
                for call in calls.splitlines()
            ):
                return
            time.sleep(0.05)
        pytest.fail("record never read the kernel log")


def _record(config_path: Path, db_path: Path, duration: str, env: dict):
    return subprocess.Popen(
        [_PROCDIAG, "record", "--duration", duration]
        + ["--config", str(config_path), "--db", str(db_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _kernel_errors(config_path: Path, db_path: Path) -> dict:
    """``{kind: count}`` of the KERNEL-ERROR-01 findings for the DB."""
    ck = _run(
        ["check", "--db", str(db_path), "--config", str(config_path)]
        + ["--only", "KERNEL-ERROR-01", "--format", "json", "-q"],
        timeout=30,
    )
    return {
        f["evidence"]["kind"]: f["evidence"]["count"]
        for f in json.loads(ck.stdout)["findings"]
    }


@pytest.mark.e2e
def test_record_keeps_kernel_errors_logged_since_its_last_read(
    tmp_path: Path,
) -> None:
    """The kernel log is read every 5 minutes; stopping reads it once more."""
    config_path = tmp_path / "config.py"
    db_path = tmp_path / "procdiag.db"
    _write_config(config_path, db_path)
    journal = _KernelJournal(tmp_path)
    journal.log("A", _BOOT_LINE)

    proc = _record(config_path, db_path, "2s", journal.env("A"))
    journal.wait_for_a_read(proc)
    journal.log("A", _io_error(2048))
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err

    assert _kernel_errors(config_path, db_path) == {"io_error": 1}


@pytest.mark.e2e
def test_record_resumes_the_kernel_log_after_a_restart_and_a_reboot(
    tmp_path: Path,
) -> None:
    """Errors logged while record was stopped are recorded once, the tail of
    the previous boot included."""
    config_path = tmp_path / "config.py"
    db_path = tmp_path / "procdiag.db"
    _write_config(config_path, db_path)
    journal = _KernelJournal(tmp_path)
    journal.log("A", _BOOT_LINE)

    proc = _record(config_path, db_path, "2s", journal.env("A"))
    journal.wait_for_a_read(proc)
    journal.log("A", _io_error(100))
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    journal.log("A", _io_error(200))
    journal.log("B", _BOOT_LINE)
    journal.log("B", _io_error(300))
    time.sleep(1.5)
    second = _run(
        ["record", "--duration", "1s"]
        + ["--config", str(config_path), "--db", str(db_path)],
        timeout=30,
        env=journal.env("B"),
    )
    assert second.returncode == 0, second.stderr

    assert _kernel_errors(config_path, db_path) == {"io_error": 3}
