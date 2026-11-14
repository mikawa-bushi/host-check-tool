"""systemd unit generation for procdiag-record.

Maps netdiag's ``_unit_text`` / ``_exec_start`` / ``service install`` to the
procdiag domain.  Key differences from netdiag's service module:

- Unit name: ``procdiag-record.service``  (records in background via ``procdiag record``)
- ``[Service]`` adds ``CPUQuota=2%`` and ``Nice=10`` to bound overhead (design §4.8)
- ``install`` writes a *user* unit and prints the enable commands without
  running ``systemctl`` — same deliberate non-execution as netdiag.
- ``print --system`` emits the system-unit variant to stdout so operators can
  transfer it manually on air-gapped hosts.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional

# Unit name shared by install and print paths.
UNIT_NAME = "procdiag-record.service"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _exec_start(config_path: Optional[str], db_path: Optional[str]) -> str:
    """Build the ExecStart line for the unit.

    Prefers the installed ``procdiag`` binary from PATH; falls back to
    ``<sys.executable> -m procdiag.cli`` so it works from a source checkout.
    """
    exe = shutil.which("procdiag")
    base = exe if exe else f"{sys.executable} -m procdiag.cli"
    parts = [base, "record"]
    if config_path:
        parts += ["--config", config_path]
    if db_path:
        parts += ["--db", db_path]
    return " ".join(parts)


def _unit_text(
    exec_start: str,
    *,
    system: bool = False,
    duration: Optional[str] = None,
) -> str:
    """Render the full .service unit file text.

    ``system=True`` targets ``multi-user.target``; the default user unit
    targets ``default.target``.  ``duration`` is forwarded to the ExecStart
    command if provided.
    """
    if duration:
        exec_start = f"{exec_start} --duration {duration}"

    install_target = "multi-user.target" if system else "default.target"
    return f"""\
[Unit]
Description=procdiag 常駐記録サービス (procdiag record)
After=local-fs.target time-sync.target
Wants=local-fs.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30
Nice=10
CPUQuota=2%

[Install]
WantedBy={install_target}
"""


# ---------------------------------------------------------------------------
# Public API (called by cli.py)
# ---------------------------------------------------------------------------


def print_unit(
    *,
    system: bool = False,
    config_path: Optional[str] = None,
    db_path: Optional[str] = None,
    duration: Optional[str] = None,
) -> str:
    """Return the rendered unit file text.

    Intended for ``procdiag service print``; the caller is responsible for
    writing or echoing the returned string.
    """
    es = _exec_start(config_path, db_path)
    return _unit_text(es, system=system, duration=duration)


def install_unit(
    *,
    config_path: Optional[str] = None,
    db_path: Optional[str] = None,
    duration: Optional[str] = None,
    home_override: Optional[Path] = None,
) -> Path:
    """Write the user unit to ``~/.config/systemd/user/`` and return its path.

    ``home_override`` replaces ``Path.home()`` — used in tests to isolate the
    write from the real home directory.

    We deliberately do NOT call ``systemctl`` here; the commands to enable
    the service are printed by the CLI layer so the operator can review them.
    """
    home = home_override if home_override is not None else Path.home()
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    es = _exec_start(config_path, db_path)
    text = _unit_text(es, system=False, duration=duration)

    unit_path = unit_dir / UNIT_NAME
    unit_path.write_text(text, encoding="utf-8")
    return unit_path
