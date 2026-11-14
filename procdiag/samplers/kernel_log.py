"""Tier-3 kernel-log sampler: disk / filesystem / hardware anomalies.

Every few minutes the sampler reads the kernel messages logged since its last
read (resuming from the journal cursor, so nothing is read twice) and records
only the lines :mod:`procdiag.kernlog` classifies as anomalies.  Recording them
in the DB — instead of reading the journal at ``check`` time — keeps the
evidence with the data when the DB is carried off the host for analysis.

The cursor is stored in ``meta`` in the same flush as the rows it covers, so
the next recording into the DB resumes right after the last stored read: the
lines logged while nothing was recording, or before a reboot, are read then.

Capability-gated: reading the system journal needs root or the
``systemd-journal`` / ``adm`` group; without it the sampler records nothing.
"""

from __future__ import annotations

from typing import Optional

from .. import kernlog
from ..core.sampler import RunContext, SampleBatch, Sampler, register_sampler
from ..storage import make_row

CURSOR_KEY = "kernel_log_cursor"


@register_sampler
class KernelLogSampler(Sampler):
    """Tier-3: record classified kernel-log anomalies."""

    name = "kernel_log"
    title = "カーネルログの異常 (I/O エラー・ハング等)"
    tier = "T3"
    interval_key = "kernel_log_interval"
    what = (
        "カーネルログからディスク I/O エラー・FS エラー・ハングタスク・"
        "ハードウェアエラー・segfault・USB 切断を抽出して記録する"
    )
    why = (
        "「固まる・落ちる」の原因はカーネルログにしか現れないことが多い。"
        "DB に残すことで、別 PC に持ち出しての解析でも根拠を確認できる"
    )
    manual_commands = [
        "journalctl -k -p warning --since today",
        "dmesg -T | tail -50",
    ]

    def __init__(self, cursor: Optional[str] = None) -> None:
        """*cursor*: where the DB's last recording stopped reading (``meta``
        :data:`CURSOR_KEY`); None starts after the newest message at setup."""
        self._enabled = False
        self._cursor = cursor

    def setup(self, ctx: RunContext) -> None:
        """Enable only when the kernel journal is readable."""
        newest = kernlog.newest_cursor()
        self._enabled = newest is not None
        self._cursor = self._cursor or newest
        ctx.capabilities["kernel_log"] = self._enabled
        if not self._enabled:
            ctx.notes.append(
                "kernel_log: カーネルログを読めません (root または systemd-journal/adm "
                "グループが必要) — カーネル異常の記録をスキップします"
            )

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Read anomalies logged since the previous read."""
        if not self._enabled:
            return []
        events, self._cursor = kernlog.read_events(after_cursor=self._cursor)
        position = make_row("meta", key=CURSOR_KEY, value=self._cursor)
        batches = [SampleBatch(table="meta", rows=[position])]
        if events:
            rows = [
                make_row("kernel_events", ts=e.ts, kind=e.kind, message=e.message)
                for e in events
            ]
            batches.append(SampleBatch(table="kernel_events", rows=rows))
        return batches
