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
message at setup.  A read from the saved cursor that fails (a timeout, a busy
journal) does not move it: the run fails and the next one reads from the same
place, so the lines logged meanwhile are neither lost nor read twice.  Only a
cursor journalctl itself rejects, or one that failed on three runs in a row
while the journal could be read without it, is replaced (the cursor is only a
string in ``meta``): the sampler reads on from the start of its last read that
worked, or from the newest message when it has not read yet.  That read starts
at a whole second, so the lines of the last one that fall in it are dropped, not
recorded twice.

Two failed reads before any worked make the recording "not observed" for
KERNEL-ERROR-01; a read from the same position that works later takes that back
(it returns what the failed ones would have), unless the position moved to the
newest message, past lines nothing read, or the journal no longer holds the
position: journalctl then reads on from the next entry it has, and the lines
between were dropped with the old ones.  That is asked of journalctl, since a
read that returns lines, or none, does not tell it.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from typing import Optional

from .. import kernlog
from ..core.result import Capability
from ..core.sampler import Note, RunContext, SampleBatch, Sampler, register_sampler
from ..storage import make_row

CURSOR_KEY = "kernel_log_cursor"
# The recorder keeps one note per key; the sampler's own collect failures use
# its name, so this note needs a key of its own.
CURSOR_NOTE_KEY = "kernel_log:cursor"
# Failed reads from one cursor, in a row, before it is replaced although
# journalctl did not call it invalid.  A timeout or a restarting journald lasts
# a run or two; a cursor that is still unreadable after three runs (15 minutes
# at the default interval) is not going to be read.  Nothing is lost meanwhile:
# the replacement reads from the start of the last read that worked.
_FAILED_READS_BEFORE_REPLACING = 3
_READ_FAILED = "journalctl でカーネルログを読めませんでした"
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
        # Reads from the cursor that failed in a row; 0 once one works.
        self._failed_reads = 0
        # Lines of the reads so far that a read from the start of the last one's
        # second would return again: ``--since`` counts whole seconds.  Two lines
        # of the same time and text are two lines, so each is counted.
        self._tail: Counter[kernlog.KernelEvent] = Counter()
        # Whether lines were skipped that nothing read: the position moved to the
        # newest message, or the journal no longer held it.
        self._skipped = False

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
        resumed_from = self._cursor
        events, cursor = self._read(ctx)
        self._cursor, self._read_at = cursor or self._cursor, started
        floor = math.floor(started)
        kept = self._tail + Counter(events)
        self._tail = Counter({e: n for e, n in kept.items() if e.ts >= floor})
        if not self._skipped:
            self._skipped = not self._began_where_the_failed_reads_did(
                ctx, resumed_from
            )
        if not self._skipped:
            ctx.observed(Capability.KERNEL_LOG)
        position = make_row("meta", key=CURSOR_KEY, value=self._cursor)
        batches = [SampleBatch(table="meta", rows=[position])]
        if events:
            rows = [
                make_row("kernel_events", ts=e.ts, kind=e.kind, message=e.message)
                for e in events
            ]
            batches.append(SampleBatch(table="kernel_events", rows=rows))
        return batches

    @staticmethod
    def _began_where_the_failed_reads_did(
        ctx: RunContext, cursor: Optional[str]
    ) -> bool:
        """Whether nothing was dropped between the failed reads and this one.

        Only asked when there is something to take back.  The position the
        failed reads kept is where this read began, as long as the journal still
        holds it.
        """
        if Capability.KERNEL_LOG not in ctx.unobserved:
            return True
        return cursor is not None and kernlog.cursor_exists(cursor)

    def _read(self, ctx: RunContext) -> tuple[list[kernlog.KernelEvent], Optional[str]]:
        """What was logged since the cursor, and the cursor to go on from.

        Raises when the journal cannot be read; the cursor then stays where it
        is, except when it is replaced (see :meth:`_read_past_cursor`).
        """
        try:
            events, cursor = kernlog.read_events(after_cursor=self._cursor)
        except kernlog.CursorRejected:
            return self._read_past_cursor(ctx)
        if events is not None:
            self._failed_reads = 0
            return events, cursor
        self._failed_reads += 1
        if self._failed_reads < _FAILED_READS_BEFORE_REPLACING:
            raise OSError(_READ_FAILED)
        return self._read_past_cursor(ctx)

    def _read_past_cursor(
        self, ctx: RunContext
    ) -> tuple[list[kernlog.KernelEvent], Optional[str]]:
        """Read on without the cursor the journal will not read from.

        From the start of the last read that worked, so nothing in between is
        lost.  The cursor of the last line read replaces it; a read of no lines
        has none, and the old cursor then stays (it still marks the last line
        recorded, and no cursor would mean the next read starts at the whole
        journal).  With no read that worked (a cursor stored by an earlier
        recording) the position moves to the newest message and this run fails:
        the next one reads from there, and a journal that fails for another
        reason keeps failing instead of being skipped over.
        """
        if self._read_at is None:
            newest = kernlog.newest_cursor()
            if newest is None:
                raise OSError(_READ_FAILED)
            self._cursor = newest
            self._skipped = True
            raise OSError(_CURSOR_RESET_ERROR)
        events, cursor = kernlog.read_events(since=self._read_at)
        if events is None:
            raise OSError(_READ_FAILED)
        ctx.notes.append(Note(CURSOR_NOTE_KEY, _CURSOR_REFUSED_NOTE))
        return self._unrecorded(events), cursor

    def _unrecorded(
        self, events: list[kernlog.KernelEvent]
    ) -> list[kernlog.KernelEvent]:
        """*events* less the lines already recorded, each one counted once."""
        recorded = Counter(self._tail)
        fresh = []
        for event in events:
            if recorded[event] > 0:
                recorded[event] -= 1
            else:
                fresh.append(event)
        return fresh
