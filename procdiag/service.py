"""systemd unit generation for procdiag-record.

Maps netdiag's ``_unit_text`` / ``_exec_start`` / ``service install`` to the
procdiag domain.  Key differences from netdiag's service module:

- Unit name: ``procdiag-record.service``  (records in background via ``procdiag record``)
- ``[Service]`` adds ``CPUQuota=2%`` and ``Nice=10`` to bound overhead (design §4.8)
- ``install`` writes a *user* unit and prints the enable commands without
  running ``systemctl`` — same deliberate non-execution as netdiag.
- ``print --system`` emits the system-unit variant to stdout so operators can
  transfer it manually on air-gapped hosts.

systemd starts the unit from its own working directory ($HOME for a user unit,
``/`` for a system unit) and splits ``ExecStart=`` by its own quoting rules, so
every path given to ``record`` is made absolute and quoted here.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Optional

from .core.util import parse_duration

# Unit name shared by install and print paths.
UNIT_NAME = "procdiag-record.service"

# A system unit given neither --config nor --db records here.  StateDirectory=
# makes systemd create the folder before ExecStart, so no setup step is needed.
SYSTEM_STATE_DIR = "procdiag"
SYSTEM_DB = f"/var/lib/{SYSTEM_STATE_DIR}/procdiag.db"

# ``procdiag record`` exits 4 for a precondition error: a broken config, an
# unusable DB, a bad option.  Starting again cannot fix it, so the unit does not
# restart on it.  Other failures (exit 1: the DB could not be written, e.g. a
# full disk) can clear up, so they keep Restart=on-failure.
PRECONDITION_EXIT_STATUS = 4

_PLAIN_WORD = re.compile(r"[A-Za-z0-9_@+=:,./-]+")
_C_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _quote(word: str) -> str:
    """One ``ExecStart=`` word, quoted as systemd.syntax(7) reads it.

    systemd also expands ``%`` specifiers and ``$`` variables in command lines,
    so a literal ``%`` or ``$`` is doubled.
    """
    word = word.replace("%", "%%").replace("$", "$$")
    if _PLAIN_WORD.fullmatch(word):
        return word
    out = []
    for ch in word:
        if ch in _C_ESCAPES:
            out.append(_C_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _absolute(option: str, value: str) -> str:
    """*value* as an absolute path, ``~`` expanded, relative to the current folder."""
    if not value:
        raise ValueError(f"{option} に空の値は指定できません")
    return os.path.abspath(os.path.expanduser(value))


def _exec_argv(
    config_path: Optional[str], db_path: Optional[str], duration: Optional[str]
) -> list[str]:
    """The ``record`` command line the unit runs, every path absolute.

    Prefers the installed ``procdiag`` binary from PATH; falls back to
    ``<sys.executable> -m procdiag.cli`` so it works from a source checkout.
    Raises :class:`ValueError` for an empty path or an invalid *duration*.
    """
    exe = shutil.which("procdiag")
    argv = [os.path.abspath(exe)] if exe else [sys.executable, "-m", "procdiag.cli"]
    argv.append("record")
    if config_path is not None:
        argv += ["--config", _absolute("--config", config_path)]
    if db_path is not None:
        argv += ["--db", _absolute("--db", db_path)]
    if duration is not None:
        parse_duration(duration)
        argv += ["--duration", duration.strip()]
    return argv


def _unit_text(
    argv: list[str], *, system: bool = False, own_state: bool = False
) -> str:
    """Render the full .service unit file text.

    ``system=True`` targets ``multi-user.target``; the default user unit
    targets ``default.target``.  ``own_state`` gives the unit a state directory
    of its own to run in (``StateDirectory=`` / ``WorkingDirectory=``).
    """
    install_target = "multi-user.target" if system else "default.target"
    # time-sync.target belongs to the system manager (systemd.special(7)); a
    # user manager has no unit to wait for the clock with.
    after = "local-fs.target time-sync.target" if system else "local-fs.target"
    state = (
        f"StateDirectory={SYSTEM_STATE_DIR}\n"
        f"WorkingDirectory=/var/lib/{SYSTEM_STATE_DIR}\n"
        if own_state
        else ""
    )
    exec_start = " ".join(_quote(word) for word in argv)
    return f"""\
[Unit]
Description=procdiag 常駐記録サービス (procdiag record)
After={after}
Wants=local-fs.target

[Service]
Type=simple
{state}ExecStart={exec_start}
Restart=on-failure
RestartSec=10
# 設定・DB・引数の誤り (終了コード {PRECONDITION_EXIT_STATUS}) は、再起動しても直らないので再起動しない
RestartPreventExitStatus={PRECONDITION_EXIT_STATUS}
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
    writing or echoing the returned string.  A system unit with neither
    *config_path* nor *db_path* records under ``/var/lib/procdiag``.
    Raises :class:`ValueError` for an empty path or an invalid *duration*.
    """
    own_state = system and config_path is None and db_path is None
    argv = _exec_argv(config_path, SYSTEM_DB if own_state else db_path, duration)
    return _unit_text(argv, system=system, own_state=own_state)


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
    Raises :class:`ValueError` (before writing anything) for an empty path or
    an invalid *duration*.
    """
    text = _unit_text(_exec_argv(config_path, db_path, duration))
    home = home_override if home_override is not None else Path.home()
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    unit_path = unit_dir / UNIT_NAME
    unit_path.write_text(text, encoding="utf-8")
    return unit_path


def system_print_command(
    config_path: Optional[str], db_path: Optional[str], duration: Optional[str]
) -> str:
    """The ``procdiag service print --system`` command with the same options.

    Paths are absolute so the command works from any folder; the result is
    quoted for a POSIX shell.
    """
    words = ["procdiag", "service", "print", "--system"]
    if config_path is not None:
        words += ["--config", _absolute("--config", config_path)]
    if db_path is not None:
        words += ["--db", _absolute("--db", db_path)]
    if duration is not None:
        words += ["--duration", duration]
    return shlex.join(words)
