"""Shared pure-function utilities for the procdiag package.

All functions here are side-effect-free so they can be tested in isolation.
Follows the netdiag ``core/util.py`` convention: stdlib only, no third-party
imports, docstrings in English, user-visible strings in Japanese.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from collections.abc import Hashable, Iterable
from typing import Any, Optional

# Binary byte units, shared by the samplers, rules and reports so every module
# converts sizes the same way as ``df -h`` / ``free -h`` (powers of 1024).
KiB = 1024
MiB = 1024**2
GiB = 1024**3


def parse_duration(s: str) -> float:
    """Convert a human-readable duration string to seconds.

    Accepted formats: ``60s``, ``24h``, ``90d``, ``1.5h``, ``2.5d``.
    Raises :class:`ValueError` with a Japanese message on invalid input.

    Examples::

        >>> parse_duration("60s")
        60.0
        >>> parse_duration("1.5h")
        5400.0
        >>> parse_duration("90d")
        7776000.0
    """
    s = s.strip()
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([smhd])", s, re.IGNORECASE)
    if not m:
        raise ValueError(
            f"期間の形式が不正です: '{s}'  (例: '60s', '24h', '90d', '1.5h')"
        )
    value = float(m.group(1))
    unit = m.group(2).lower()
    multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    return value * multipliers[unit]


def humanize_bytes(n: Optional[int]) -> str:
    """Format a byte count as a human-readable string.

    Returns ``"—"`` for ``None``.  Uses a binary (1024) base so the numbers
    line up with ``df -h`` / ``free -h`` (which label powers of 1024 as K/M/G).

    Examples::

        >>> humanize_bytes(1234567)
        '1.2MB'
        >>> humanize_bytes(0)
        '0B'
    """
    if n is None:
        return "—"
    if n < 0:
        sign = "-"
        n = -n
    else:
        sign = ""
    for unit, threshold in [
        ("TB", 1024**4),
        ("GB", GiB),
        ("MB", MiB),
        ("KB", KiB),
    ]:
        if n >= threshold:
            return f"{sign}{n / threshold:.1f}{unit}"
    return f"{sign}{n}B"


def humanize_duration(sec: float) -> str:
    """Format a duration in seconds as a human-readable Japanese string.

    Examples::

        >>> humanize_duration(93784)
        '1日2時間'
        >>> humanize_duration(3661)
        '1時間1分'
        >>> humanize_duration(45)
        '45秒'
    """
    sec = int(sec)
    if sec < 0:
        sec = 0

    days = sec // 86400
    sec %= 86400
    hours = sec // 3600
    sec %= 3600
    minutes = sec // 60
    seconds = sec % 60

    parts: list[str] = []
    if days:
        parts.append(f"{days}日")
    if hours:
        parts.append(f"{hours}時間")
    if minutes and not days:  # omit minutes when days are shown
        parts.append(f"{minutes}分")
    if seconds and not days and not hours:
        parts.append(f"{seconds}秒")

    return "".join(parts) if parts else "0秒"


def normalize_cmdline(
    cmdline: list[str], *, replace_digits: bool = True
) -> tuple[str, str]:
    """Normalise a process command line to a stable identity key.

    Returns ``(hash12, display_sample)`` where:

    * ``hash12`` is the first 12 hex characters of the SHA-1 of the
      normalised joined command line.  Stable across restarts with the same
      effective command.
    * ``display_sample`` is a human-readable truncated version of the raw
      command line, suitable for storage in ``proc_registry.cmdline_sample``.

    When ``replace_digits=True`` (the default), sequences of digits are
    replaced by ``"#"`` before hashing so that pid-like or date-like arguments
    (``--pid 12345``, ``--date 2026-06-11``) do not create separate entries in
    the process registry.

    The hash is *stable*: calling with the same logical command always returns
    the same ``hash12``, which is what ``proc_id_for`` relies on.
    """
    raw_text = " ".join(cmdline) if cmdline else ""
    normalised = re.sub(r"\d+", "#", raw_text) if replace_digits else raw_text

    hash12 = hashlib.sha1(normalised.encode("utf-8", errors="replace")).hexdigest()[:12]

    # Keep the display sample short but recognisable.
    display = raw_text[:200] if len(raw_text) > 200 else raw_text

    return hash12, display


def should_use_color(no_color: bool = False) -> bool:
    """Whether ANSI colour should be emitted to stdout.

    Returns False when ``--no-color`` is requested, the ``NO_COLOR`` environment
    variable is set (the no-color.org convention), or stdout is not a TTY.
    Centralises the decision so every command treats colour consistently and
    output copied into tickets/log captures stays free of escape codes.
    """
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def storable_text(text: str) -> str:
    r"""Return *text* in a form SQLite and a UTF-8 stream accept.

    psutil and ``os`` decode host names and paths with ``surrogateescape``, so
    a Shift_JIS argv or a UTF-8 name the kernel cut at 15 bytes arrives with
    lone surrogates, which neither SQLite nor ``click.echo`` can encode.  Non
    UTF-8 bytes become ``\xNN`` so two such names stay distinct.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogateescape").decode(
            "utf-8", "backslashreplace"
        )
    return text


# What a terminal may act on or that changes what is shown: C0 / DEL / C1
# controls, the Unicode line and paragraph separators, and the bidirectional
# marks and overrides.
_TERMINAL_ACTIVE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
# What ``json.dumps(ensure_ascii=False)`` leaves unescaped from the set above.
_JSON_LEFT_RAW = re.compile(
    "[\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_char(match: "re.Match[str]") -> str:
    ch = match.group()
    if ch in _NAMED_ESCAPES:
        return _NAMED_ESCAPES[ch]
    code = ord(ch)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def neutralize(text: str, *, allow: Iterable[str] = ()) -> str:
    r"""Make host-supplied *text* one line of inert, printable text.

    Control characters and newlines become ``\x1b`` / ``\n`` style escapes, so
    a process name cannot clear the screen, set the title, hide the rest of the
    line or start a forged line of its own.  *allow* lists exact escape
    sequences procdiag emitted itself (its colour codes); those pass through.
    """
    text = storable_text(text)
    if not allow:
        return _TERMINAL_ACTIVE.sub(_escape_char, text)
    keep = re.compile("(" + "|".join(re.escape(a) for a in allow) + ")")
    return "".join(
        part if i % 2 else _TERMINAL_ACTIVE.sub(_escape_char, part)
        for i, part in enumerate(keep.split(text))
    )


def neutralized_lines(lines: Iterable[str], *, allow: Iterable[str] = ()) -> str:
    """Join *lines* with newlines, each one passed through :func:`neutralize`."""
    allow = tuple(allow)
    return "\n".join(neutralize(line, allow=allow) for line in lines)


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _json_surrogate(match: "re.Match[str]") -> str:
    """A non UTF-8 byte kept by ``surrogateescape``, as the text ``\\xNN`` (JSON-escaped)."""
    code = ord(match.group())
    return f"\\\\x{code - 0xDC00:02x}" if 0xDC80 <= code <= 0xDCFF else "\\ufffd"


def dumps_json(obj: Any) -> str:
    """``json.dumps`` for reports: UTF-8 text with terminal-active code points escaped.

    ``ensure_ascii=False`` keeps Japanese readable but would also pass DEL, C1
    controls (U+009B is a CSI) and the bidi overrides to a terminal as they are,
    and a name with non UTF-8 bytes (lone surrogates) would not encode at all.
    """
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    text = _LONE_SURROGATE.sub(_json_surrogate, text)
    return _JSON_LEFT_RAW.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def shell_quote(value: object) -> str:
    """*value* as one shell word, safe to paste into a command line."""
    return shlex.quote(str(value))


_UNIT_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,254}")


def is_unit_name(name: object) -> bool:
    """Whether *name* could be a systemd unit name (so ``systemctl restart`` fits)."""
    return isinstance(name, str) and _UNIT_NAME.fullmatch(name) is not None


class CounterDelta:
    """Turn cumulative OS counters into per-tick deltas, keyed by any hashable.

    ``update(key, value)`` returns ``None`` the first time a key is seen (there
    is no baseline to diff against) and ``max(0, value - previous)`` afterwards.
    The clamp is the single policy for counter resets — a restarted cgroup unit
    or a wrapped kernel counter restarts from 0, and a raw negative delta would
    corrupt every rollup sum built on it.  Keeping the clamp here means a new
    sampler cannot forget it (the v0.3 cgroup bug C-01 was exactly that).
    """

    def __init__(self) -> None:
        self._prev: dict[Hashable, int] = {}

    def update(self, key: Hashable, value: int) -> Optional[int]:
        """Record *value* for *key* and return the clamped delta (None at first)."""
        prev = self._prev.get(key)
        self._prev[key] = value
        if prev is None:
            return None
        return max(0, value - prev)
