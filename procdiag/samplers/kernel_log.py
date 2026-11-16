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
A DB recorded before this version has no cursor and starts after the newest
message at setup.  A cursor the journal refuses (it is only a string in
``meta``) does not stop the recording: the sampler reads on from the time of
its last read, or from the newest message when it has none.
"""

from __future__ import annotations

import time
from typing import Optional

from .. import kernlog
from ..core.result import Capability
from ..core.sampler import Note, RunContext, SampleBatch, Sampler, register_sampler
from ..storage import make_row

CURSOR_KEY = "kernel_log_cursor"
_CURSOR_REFUSED_NOTE = (
    "kernel_log: ジャーナルが読み取り位置を受け付けなかったため、"
    "前回の読み取り時刻から読み直しました"
)
_CURSOR_RESET_ERROR = (
    "保存していた読み取り位置をジャーナルが受け付けなかったため、"
    "いまの位置から読み直します (記録が止まっていた間のカーネルログは読めていません)"
)


@register_sampler
class KernelLogSampler(Sampler):
    """Tier-3: record classified kernel-log anomalies."""

    name = "kernel_log"
    title = "カーネルログの異常 (I/O エラー・ハング等)"
    tier = "T3"
    interval_key = "kernel_log_interval"
    capability = Capability.KERNEL_LOG
    what = (
        "カーネルログからディスク I/O エラー・FS エラー・ハングタスク・"
        "ハードウェアエラー・segfault・USB 切断を抽出して記録する"
    )
    why = (
        "「固まる・落ちる」の原因はカーネルログにしか現れないことが多い。"
        "DB に残すことで、別 PC に持ち出しての解析でも根拠を確認できる"
    )
    manual_commands = [
        "journalctl -k -b all -p warning --since today",
        "dmesg -T | tail -50",
    ]

    def __init__(self, cursor: Optional[str] = None) -> None:
        """*cursor*: where the DB's last recording stopped reading (``meta``
        :data:`CURSOR_KEY`); None starts after the newest message at setup."""
        self._enabled = False
        self._cursor = cursor
        # When the last read that worked began; None before the first one.
        self._read_at: Optional[float] = None

    def setup(self, ctx: RunContext) -> None:
        """Enable only when the kernel journal is readable."""
        newest = kernlog.newest_cursor()
        self._enabled = newest is not None
        self._cursor = self._cursor or newest
        ctx.capabilities[Capability.KERNEL_LOG.value] = self._enabled
        if not self._enabled:
            ctx.lacks(
                Capability.KERNEL_LOG,
                "kernel_log: カーネルログを読めません (root または systemd-journal/adm "
                "グループが必要) — カーネル異常の記録をスキップします",
            )

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Read anomalies logged since the previous read."""
        if not self._enabled:
            return []
        started = time.time()
        events, cursor = kernlog.read_events(after_cursor=self._cursor)
        if events is None and self._cursor:
            events, cursor = self._read_without_cursor(ctx)
        if events is None:
            raise OSError("journalctl でカーネルログを読めませんでした")
        self._cursor, self._read_at = cursor, started
        position = make_row("meta", key=CURSOR_KEY, value=self._cursor)
        batches = [SampleBatch(table="meta", rows=[position])]
        if events:
            rows = [
                make_row("kernel_events", ts=e.ts, kind=e.kind, message=e.message)
                for e in events
            ]
            batches.append(SampleBatch(table="kernel_events", rows=rows))
        return batches

    def _read_without_cursor(
        self, ctx: RunContext
    ) -> tuple[Optional[list[kernlog.KernelEvent]], Optional[str]]:
        """Read on when the journal refuses the cursor: ``(events, cursor)``,
        ``(None, None)`` when the journal cannot be read at all.

        From the start of the last read that worked, so nothing in between is
        lost.  With no such read (a cursor stored by an earlier recording) the
        position moves to the newest message and this run fails: the next one
        reads from there, and a journal that fails for another reason keeps
        failing instead of being skipped over.
        """
        if self._read_at is not None:
            events, cursor = kernlog.read_events(since=self._read_at)
            if events is None:
                return None, None
            ctx.notes.append(Note("kernel_log", _CURSOR_REFUSED_NOTE))
            return events, cursor
        newest = kernlog.newest_cursor()
        if newest is None:
            return None, None
        self._cursor = newest
        raise OSError(_CURSOR_RESET_ERROR)
