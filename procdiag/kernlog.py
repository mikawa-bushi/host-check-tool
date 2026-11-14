"""Kernel-log anomaly reader (the journal's kernel lines) for recorder, snapshot, rules.

Production-floor PCs "freeze" or "fall over" for reasons that only the kernel
log states plainly: a failing disk (I/O errors, a filesystem remounted
read-only), hung tasks, machine-check (hardware) errors, segfaulting
applications, or a USB device dropping off the bus.  This module classifies
those lines into a few kinds; everything else in the kernel log is ignored.

Reading the system journal needs root or the ``systemd-journal`` / ``adm``
group.  Without it ``journalctl`` returns nothing, which callers treat as
"capability unavailable" rather than "no errors".
"""

from __future__ import annotations

import math
import re
from typing import List, NamedTuple, Optional, Tuple

from .systemd import run

_MAX_MESSAGE = 300

# (kind, Japanese label, pattern).  Order matters: the first match wins, so the
# specific filesystem patterns are tried before the generic "I/O error".
KINDS: Tuple[Tuple[str, str, "re.Pattern[str]"], ...] = (
    ("oom", "OOM Killer", re.compile(r"Out of memory|oom-kill|Killed process", re.I)),
    (
        "fs_error",
        "ファイルシステムエラー",
        re.compile(
            r"EXT4-fs error|EXT4-fs \(.*\): Remounting filesystem read-only"
            r"|XFS \(.*\): (?:Corruption|metadata I/O error"
            r"|Metadata (?:corruption|CRC error) detected|Filesystem has been shut down)"
            r"|BTRFS (?:error|critical)|BTRFS: error \(device"
            r"|Remounting filesystem read-only",
            re.I,
        ),
    ),
    (
        "io_error",
        "ディスク I/O エラー",
        # Block-layer statuses that mean the device failed the request.
        # "operation not supported", the resource and the retry statuses are
        # not disk faults and stay unmatched.
        re.compile(
            r"I/O error|blk_update_request|Buffer I/O error|critical medium error"
            r"|(?:critical (?:target|nexus)|timeout|recoverable transport"
            r"|device offline|protection) error, dev "
            r"|FAILED Result: hostbyte"
            r"|QID \d+ timeout, (?:aborting|reset controller|disable controller)"
            r"|Device not ready; aborting"
            r"|ata\d+(?:\.\d+)?: (?:failed command|exception Emask)",
            re.I,
        ),
    ),
    (
        "hung_task",
        "ハングタスク (処理の停止)",
        re.compile(r"blocked for more than \d+ seconds", re.I),
    ),
    (
        "mce",
        "ハードウェアエラー (MCE)",
        re.compile(r"Machine check|\[Hardware Error\]", re.I),
    ),
    (
        "segfault",
        "アプリケーションの異常終了 (segfault)",
        re.compile(r"segfault at|general protection fault|traps: .* trap", re.I),
    ),
    ("usb_disconnect", "USB 機器の切断", re.compile(r"USB disconnect", re.I)),
    (
        "thermal",
        "温度による制限",
        re.compile(r"temperature above threshold|clock throttled", re.I),
    ),
)
LABELS = {kind: label for kind, label, _ in KINDS}


class KernelEvent(NamedTuple):
    ts: float
    kind: str
    message: str


def classify(message: str) -> Optional[str]:
    """Return the anomaly kind of a kernel log line, or None if it is benign."""
    for kind, _label, pattern in KINDS:
        if pattern.search(message):
            return kind
    return None


def parse_short_unix(text: str) -> Tuple[List[KernelEvent], Optional[str]]:
    """Parse ``journalctl -o short-unix --show-cursor`` output.

    Lines look like ``1730000000.123456 host kernel: <message>``; the final
    ``-- cursor: <c>`` line (if any) is returned so the next read can resume
    exactly after it.
    """
    events: List[KernelEvent] = []
    cursor: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("-- cursor: "):
            cursor = line[len("-- cursor: ") :].strip()
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        message = parts[2]
        if message.startswith("kernel: "):
            message = message[len("kernel: ") :]
        kind = classify(message)
        if kind is not None:
            events.append(KernelEvent(ts, kind, message[:_MAX_MESSAGE]))
    return events, cursor


def read_events(
    *,
    since: Optional[float] = None,
    until: Optional[float] = None,
    after_cursor: Optional[str] = None,
) -> Tuple[Optional[List[KernelEvent]], Optional[str]]:
    """Read classified kernel events; return ``(events, cursor)``.

    *after_cursor* resumes exactly after a previous read (no duplicates);
    otherwise *since* (epoch seconds) bounds the start.  *until* bounds the
    end.  Times are passed as ``@epoch`` so the host's time zone cannot shift
    the range.  Earlier boots are included: ``-k`` would limit the read to the
    running boot and hide the lines logged before a reboot or a power loss.
    ``events`` is None when the journal cannot be read at all (no journalctl,
    no permission).
    """
    args = ["journalctl", "_TRANSPORT=kernel", "-q", "--no-pager"]
    args += ["-o", "short-unix", "--show-cursor"]
    if after_cursor:
        args += ["--after-cursor", after_cursor]
    elif since is not None:
        args += ["--since", f"@{math.floor(since)}"]
    if until is not None:
        args += ["--until", f"@{math.ceil(until)}"]
    out = run(args)
    # journalctl exits 0 with no output when it may not read the system journal.
    if out is None or (not out.strip() and not journal_readable()):
        return None, after_cursor
    events, cursor = parse_short_unix(out)
    return events, cursor or after_cursor


def journal_readable() -> bool:
    """Whether this process can read kernel messages from the journal."""
    out = run(["journalctl", "-k", "-n", "1", "-q", "--no-pager"])
    return bool(out and out.strip())


def newest_cursor() -> Optional[str]:
    """Cursor of the newest kernel message; None when the journal is unreadable."""
    out = run(
        ["journalctl", "_TRANSPORT=kernel", "-n", "1", "-q", "--no-pager"]
        + ["-o", "short-unix", "--show-cursor"]
    )
    return parse_short_unix(out)[1] if out else None
