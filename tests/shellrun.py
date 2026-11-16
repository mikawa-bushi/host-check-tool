"""Paste a suggested command into a real shell, with stand-ins on PATH.

The stand-ins record the arguments they were called with and do nothing, so a
test can say what a command line really ran: which commands, with which
arguments.  ``touch`` is one of them, the payload of an injected command.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SHELLS = [
    pytest.param(
        name,
        marks=pytest.mark.skipif(shutil.which(name) is None, reason=f"needs {name}"),
    )
    for name in ("bash", "dash", "sh")
]

_STUB = """#!/bin/sh
{{
  printf '%s\\0%s\\0' "${{0##*/}}" "$#"
  for a in "$@"; do printf '%s\\0' "$a"; done
}} >> '{log}'
"""

COMMANDS = ("probe", "pgrep", "du", "sudo", "touch", "curl", "reboot")


@dataclass(frozen=True)
class Pasted:
    calls: list[tuple[str, list[str]]]
    returncode: int
    stderr: str


class Stubs:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.log = directory / "calls.log"
        for name in COMMANDS:
            path = directory / name
            path.write_text(_STUB.format(log=self.log))
            path.chmod(0o755)

    def paste(self, shell: str, command: str) -> Pasted:
        self.log.write_bytes(b"")
        env = dict(os.environ, PATH=f"{self.directory}:{os.environ['PATH']}")
        done = subprocess.run(
            [shell, "-c", command],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        return Pasted(self._calls(), done.returncode, done.stderr)

    def _calls(self) -> list[tuple[str, list[str]]]:
        fields = self.log.read_bytes().decode("utf-8", "surrogateescape").split("\0")
        fields.pop()
        calls, i = [], 0
        while i < len(fields):
            name, argc = fields[i], int(fields[i + 1])
            calls.append((name, fields[i + 2 : i + 2 + argc]))
            i += 2 + argc
        return calls
