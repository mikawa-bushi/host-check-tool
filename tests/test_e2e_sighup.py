"""The terminal-closing e2e test holds whichever way the runner was started.

``nohup pytest`` leaves SIGHUP ignored, and an ignored signal is inherited by
the ``record`` the test starts: the case where record has to stop on SIGHUP
then never stopped.  The test resets SIGHUP in the child it spawns.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_RUN_WITH_SIGHUP_IGNORED = """\
import os, signal, sys
signal.signal(signal.SIGHUP, signal.SIG_IGN)
os.execv(sys.executable, [sys.executable, "-m", "pytest", "-q",
                          "-p", "no:cacheprovider", "--basetemp", sys.argv[1], sys.argv[2]])
"""


@pytest.mark.e2e
def test_the_hangup_test_passes_when_the_runner_ignores_sighup(tmp_path: Path) -> None:
    test = Path(__file__).parent / "test_e2e.py"
    node = f"{test}::test_closing_the_terminal_stops_record_unless_started_with_nohup[False]"

    result = subprocess.run(
        [sys.executable, "-c", _RUN_WITH_SIGHUP_IGNORED, str(tmp_path / "inner"), node],
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stdout[-3000:]
