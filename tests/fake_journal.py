"""A stand-in ``journalctl`` for the kernel log, answering as the real one does.

What it copies from journalctl (measured on systemd 259): ``--show-cursor``
prints the cursor of the last line shown; ``--after-cursor`` with nothing new
echoes the cursor it was given, while ``--since`` with nothing to show prints
no cursor; a cursor journalctl cannot parse ends the read with exit 1 and
"Failed to seek to cursor: Invalid argument".  A cursor here is ``s=fake;i=<n>``
for the *n*-th line.

Faults a test can arm: the next reads from a cursor fail without saying why
(a timeout, a busy journal), every cursor is refused, the whole journal is down.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = r"""
import json, os, re, sys

args = sys.argv[1:]
root = os.environ["FAKE_JOURNAL_DIR"]


def opt(name):
    return args[args.index(name) + 1] if name in args else None


with open(root + "/calls", "a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\n")
try:
    with open(root + "/faults.json", encoding="utf-8") as f:
        faults = json.load(f)
except OSError:
    faults = {}
if faults.get("down"):
    sys.stderr.write("journalctl: injected failure\n")
    sys.exit(1)
with open(root + "/journal", encoding="utf-8") as f:
    shown = [
        (i, float(ts), message)
        for i, (ts, message) in enumerate(
            (line.rstrip("\n").split("\t", 1) for line in f if line.strip()), 1
        )
    ]
if opt("--after-cursor"):
    given = re.fullmatch(r"s=fake;i=(\d+)", opt("--after-cursor"))
    if faults.get("reject_cursors") or not given:
        sys.stderr.write("Failed to seek to cursor: Invalid argument\n")
        sys.exit(1)
    failing = faults.get("fail_cursor_reads", 0)
    if failing:
        if failing > 0:
            faults["fail_cursor_reads"] = failing - 1
            with open(root + "/faults.json", "w", encoding="utf-8") as f:
                json.dump(faults, f)
        sys.stderr.write("journalctl: injected failure\n")
        sys.exit(1)
    shown = [line for line in shown if line[0] > int(given.group(1))]
elif opt("--since"):
    since = float(opt("--since").lstrip("@"))
    shown = [line for line in shown if line[1] >= since]
if opt("--until"):
    until = float(opt("--until").lstrip("@"))
    shown = [line for line in shown if line[1] <= until]
if opt("-n"):
    shown = shown[-int(opt("-n")) :]
for _i, ts, message in shown:
    print(f"{ts:.6f} edge kernel: {message}")
if "--show-cursor" in args:
    if shown:
        print(f"-- cursor: s=fake;i={shown[-1][0]}")
    elif opt("--after-cursor"):
        print(f"-- cursor: {opt('--after-cursor')}")
"""


class FakeJournal:
    """The stand-in on ``PATH``; ``log`` adds a kernel line stamped *ts*."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._dir = tmp_path / "journal"
        bin_dir = self._dir / "bin"
        bin_dir.mkdir(parents=True)
        script = self._dir / "journalctl.py"
        script.write_text(_SCRIPT, encoding="utf-8")
        wrapper = bin_dir / "journalctl"
        wrapper.write_text(
            f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        self._lines = self._dir / "journal"
        self._lines.touch()
        (self._dir / "calls").touch()
        self._faults: dict = {}
        monkeypatch.setenv("FAKE_JOURNAL_DIR", str(self._dir))
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    def log(self, ts: float, message: str) -> str:
        """Append a kernel line; return its cursor."""
        with self._lines.open("a", encoding="utf-8") as f:
            f.write(f"{ts:.6f}\t{message}\n")
        return self.newest()

    def newest(self) -> str:
        count = len(self._lines.read_text(encoding="utf-8").splitlines())
        return f"s=fake;i={count}"

    def fail_cursor_reads(self, count: int = -1) -> None:
        """Make the next *count* reads from a cursor fail (-1: all of them)."""
        self._arm(fail_cursor_reads=count)

    def reject_cursors(self, on: bool = True) -> None:
        self._arm(reject_cursors=on)

    def go_down(self, on: bool = True) -> None:
        self._arm(down=on)

    def line_reads(self) -> list[str]:
        """How each read of kernel lines was bounded: ``after-cursor``,
        ``since`` or ``unbounded`` (the whole journal)."""
        calls = (self._dir / "calls").read_text(encoding="utf-8").splitlines()
        bounds = []
        for call in calls:
            args = call.split()
            if "_TRANSPORT=kernel" not in args or "-n" in args:
                continue
            if "--after-cursor" in args:
                bounds.append("after-cursor")
            elif "--since" in args:
                bounds.append("since")
            else:
                bounds.append("unbounded")
        return bounds

    def _arm(self, **faults) -> None:
        self._faults.update(faults)
        (self._dir / "faults.json").write_text(json.dumps(self._faults))
