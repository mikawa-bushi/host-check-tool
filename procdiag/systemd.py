"""Thin, read-only wrappers around the systemd tools (``systemctl`` etc.).

Used by the service sampler (automatic restarts, failed units), by
:mod:`procdiag.kernlog` (``journalctl``) and by ``procdiag snapshot``.  Every
call has a timeout and returns ``None`` instead of raising when systemd is
absent or the command fails, so hosts without systemd (containers,
non-systemd distros) simply skip these checks.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

_TIMEOUT_SEC = 10.0
# ``systemctl show`` takes unit names on the command line; chunk long lists so
# the argv stays far below ARG_MAX on hosts with hundreds of units.
_SHOW_CHUNK = 100
SERVICE_PROPERTIES = (
    "Id",
    "NRestarts",
    "Restart",
    "RestartForceExitStatus",
    "RestartUSec",
    "LogLevelMax",
    "Result",
    "ActiveState",
    "SubState",
)
# systemd (PID 1) logs "<unit>: Failed with result '<result>'." each time a run
# of a unit ends abnormally, also when Restart= then starts it again.  Since
# v240 the line carries this MESSAGE_ID and UNIT_RESULT; v233-v239 log only the
# plain message.
_UNIT_FAILED_MESSAGE_ID = "d9b373ed55a64feb8242e02dbe79a49c"
_UNIT_FAILED_RE = re.compile(r"Failed with result '([^']+)'")
_TIMESPAN_SEC = {
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "min": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
    "M": 2629800.0,
    "y": 31557600.0,
}
_TIMESPAN_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(us|ms|min|s|h|d|w|M|y)")


def available() -> bool:
    """Whether this host runs systemd and ``systemctl`` is on PATH."""
    return (
        shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()
    )


def run(args: List[str]) -> Optional[str]:
    """Run a command and return stdout, or None on any failure (incl. timeout)."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT_SEC
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _unit_names(args: List[str]) -> Optional[List[str]]:
    """First column of a ``systemctl list-units --no-legend --plain`` listing."""
    out = run(args)
    if out is None:
        return None
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def list_service_units() -> Optional[List[str]]:
    """Names of all loaded ``.service`` units (active or not)."""
    return _unit_names(
        ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain"]
    )


def failed_units() -> Optional[List[str]]:
    """Names of units currently in the ``failed`` state."""
    return _unit_names(
        ["systemctl", "list-units", "--failed", "--no-legend", "--plain"]
    )


def show_units(
    units: Iterable[str], properties: Iterable[str] = SERVICE_PROPERTIES
) -> Optional[Dict[str, Dict[str, str]]]:
    """Return ``{unit: {property: value}}`` from ``systemctl show``.

    ``systemctl show`` prints one ``Key=Value`` block per unit, separated by a
    blank line, in argument order.
    """
    units = list(units)
    props = ",".join(properties)
    out: Dict[str, Dict[str, str]] = {}
    for i in range(0, len(units), _SHOW_CHUNK):
        chunk = units[i : i + _SHOW_CHUNK]
        text = run(["systemctl", "show", "-p", props, "--", *chunk])
        if text is None:
            return None
        for block in text.strip().split("\n\n"):
            fields = dict(
                line.split("=", 1) for line in block.splitlines() if "=" in line
            )
            unit = fields.get("Id")
            if unit:
                out[unit] = fields
    return out


def n_restarts(props: Mapping[str, str]) -> Optional[int]:
    """A unit's ``NRestarts`` as an int; None when absent (systemd < 235) or bad."""
    value = props.get("NRestarts")
    try:
        return int(value) if value else None
    except ValueError:
        return None


def timespan_sec(value: Optional[str]) -> Optional[float]:
    """Seconds of a ``systemctl show`` timespan such as ``100ms`` or
    ``1min 30s``; None when absent, ``infinity`` or unparsable."""
    if not value or value == "infinity":
        return None
    if value == "0":
        return 0.0
    parts = _TIMESPAN_RE.findall(value)
    if "".join(n + u for n, u in parts) != value.replace(" ", ""):
        return None
    return sum(float(n) * _TIMESPAN_SEC[u] for n, u in parts)


def unit_failures(
    units: Iterable[str], since: float
) -> Optional[Dict[str, List[Tuple[float, str]]]]:
    """``{unit: [(ts, result), ...]}``, oldest first, for each abnormal end of
    *units* that systemd logged after *since* (epoch seconds).

    None when ``journalctl`` fails.  Without permission to read the system
    journal ``journalctl`` returns nothing; check that before trusting an
    empty result.
    """
    out = run(
        [
            "journalctl",
            "-q",
            "--no-pager",
            "-o",
            "json",
            "--output-fields=MESSAGE_ID,UNIT,UNIT_RESULT,MESSAGE",
            "--since",
            f"@{math.floor(since)}",
            "_PID=1",
            *(f"UNIT={unit}" for unit in units),
        ]
    )
    if out is None:
        return None
    failures: Dict[str, List[Tuple[float, str]]] = {}
    for line in out.splitlines():
        try:
            entry = json.loads(line)
            ts = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
        except (ValueError, KeyError, TypeError):
            continue
        unit, message = entry.get("UNIT"), entry.get("MESSAGE")
        if entry.get("MESSAGE_ID") == _UNIT_FAILED_MESSAGE_ID:
            result = entry.get("UNIT_RESULT")
        else:
            match = (
                _UNIT_FAILED_RE.search(message) if isinstance(message, str) else None
            )
            if match is None:
                continue
            result = match.group(1)
        if ts > since and isinstance(unit, str):
            failures.setdefault(unit, []).append(
                (ts, result if isinstance(result, str) else "")
            )
    return failures


def ntp_synchronized() -> Optional[bool]:
    """Whether the system clock is NTP-synchronised (``timedatectl``)."""
    out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if out is None:
        return None
    value = out.strip().lower()
    if value not in ("yes", "no"):
        return None
    return value == "yes"
