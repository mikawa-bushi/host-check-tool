"""A report written with ``-o`` carries no terminal colour, even from a terminal."""

from __future__ import annotations

import os
import pty
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from synth import add_proc_series, add_sys_baseline, make_db

_PROCDIAG = str(Path(sys.executable).parent / "procdiag")
_HOUR = 3600.0
_MB = 1024 * 1024
ESC = b"\x1b"

pytestmark = pytest.mark.skipif(
    not hasattr(pty, "openpty"), reason="needs a pseudo terminal"
)


def _on_terminal(args: list[str]) -> tuple[int, bytes]:
    """Run procdiag with a pseudo terminal as stdout; return (exit code, screen)."""
    master, slave = pty.openpty()
    env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
    try:
        proc = subprocess.run(
            [_PROCDIAG, *args],
            stdout=slave,
            stderr=subprocess.PIPE,
            env=env,
            timeout=60,
        )
        os.close(slave)
        slave = -1
        screen = b""
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            screen += chunk
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)
    return proc.returncode, screen


@pytest.fixture()
def leaking_db(tmp_path):
    start = time.time() - 30 * _HOUR
    store = make_db(tmp_path, hours=30)
    add_sys_baseline(store, hours=30, start_ts=start)
    add_proc_series(
        store,
        "leaky",
        rss_start=200 * _MB,
        rss_slope_per_h=50 * _MB,
        hours=30,
        start_ts=start,
    )
    store.close()
    return store.path


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_screen_is_coloured_on_a_terminal(leaking_db):
    code, screen = _on_terminal(["check", "--db", str(leaking_db)])
    assert code == 1
    assert ESC in screen


def test_check_output_file_has_no_colour_codes(leaking_db, tmp_path):
    out = tmp_path / "report.txt"
    code, screen = _on_terminal(["check", "--db", str(leaking_db), "-o", str(out)])
    assert code == 1
    assert ESC not in out.read_bytes()
    assert "MEM-LEAK-01" in out.read_text(encoding="utf-8")
    assert ESC not in screen


def test_compare_output_file_has_no_colour_codes(tmp_path):
    now = time.time()
    at = now - 12 * _HOUR
    store = make_db(tmp_path, hours=24)
    add_sys_baseline(store, hours=24, start_ts=now - 24 * _HOUR)
    for rss, slope, t0 in ((200, 38, now - 24 * _HOUR), (300, 0.2, at)):
        add_proc_series(
            store,
            "myapp",
            rss_start=rss * _MB,
            rss_slope_per_h=slope * _MB,
            hours=12,
            start_ts=t0,
        )
    store.close()
    out = tmp_path / "cmp.txt"
    code, screen = _on_terminal(
        ["compare", "--db", str(store.path), "--at", _iso(at), "--output", str(out)]
    )
    assert code == 0, screen
    assert "MEM-LEAK-01" in out.read_text(encoding="utf-8")
    assert ESC not in out.read_bytes()
