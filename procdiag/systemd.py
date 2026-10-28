"""Thin, read-only wrappers around the systemd tools (``systemctl`` etc.).

Used by the service sampler (automatic restarts, failed units), by
:mod:`procdiag.kernlog` (``journalctl``) and by ``procdiag snapshot``.  Every
call has a timeout and returns ``None`` instead of raising when systemd is
absent or the command fails, so hosts without systemd (containers,
non-systemd distros) simply skip these checks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

_TIMEOUT_SEC = 10.0
# ``systemctl show`` takes unit names on the command line; chunk long lists so
# the argv stays far below ARG_MAX on hosts with hundreds of units.
_SHOW_CHUNK = 100
SERVICE_PROPERTIES = ("Id", "NRestarts", "Result", "ActiveState", "SubState")


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


def ntp_synchronized() -> Optional[bool]:
    """Whether the system clock is NTP-synchronised (``timedatectl``)."""
    out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if out is None:
        return None
    value = out.strip().lower()
    if value not in ("yes", "no"):
        return None
    return value == "yes"
