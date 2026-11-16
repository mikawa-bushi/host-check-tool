"""Where the kernel-log sampler resumes reading, and what a failed read costs.

The journal position the sampler keeps decides which kernel lines are read: a
read that fails once must not skip the lines logged meanwhile, a position the
journal will not read from is replaced without re-reading the history, and the
sampler never asks for the whole journal.
"""

from __future__ import annotations

import math
import time
from types import SimpleNamespace
from typing import Callable, Optional

import pytest
from fake_journal import FakeJournal
from test_recorder import FakeClock, FakeSampler, _AdvancingSleep

from procdiag import kernlog
from procdiag.config import ProcdiagConfig, SamplingConfig
from procdiag.core import Capability
from procdiag.core.sampler import RunContext, SampleBatch
from procdiag.recorder import Recorder
from procdiag.samplers import kernel_log
from procdiag.samplers.kernel_log import KernelLogSampler
from procdiag.storage import Storage

_T0 = 1_731_000_000.5
_INTERVAL = 300.0
_BOOT = "Linux version 6.8.0-fake (gcc 13.2.0) #1 SMP PREEMPT_DYNAMIC"
_IO_ERROR = "blk_update_request: I/O error, dev sda, sector 9"
_DAY = 86400.0


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def journal(tmp_path, monkeypatch) -> FakeJournal:
    return FakeJournal(tmp_path, monkeypatch)


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(kernel_log, "time", SimpleNamespace(time=clock))
    return clock


class _Recording:
    """A kernel-log sampler ticking every five minutes, and what it stored."""

    def __init__(self, clock: _Clock, cursor: Optional[str] = None) -> None:
        self.clock = clock
        self.sampler = KernelLogSampler(cursor=cursor)
        self.ctx = RunContext(ProcdiagConfig())
        self.sampler.setup(self.ctx)
        self.kinds: list[str] = []
        self.cursor = cursor

    def tick(self) -> bool:
        """Run one collect; whether it succeeded."""
        self.clock.now += _INTERVAL
        batches = self.sampler.run(self.ctx)
        for batch in batches:
            for row in batch.rows:
                if batch.table == "meta":
                    self.cursor = row[1]
                else:
                    self.kinds.append(row[1])
        return bool(batches)

    def ticks(self, count: int) -> list[bool]:
        return [self.tick() for _ in range(count)]

    def after_tick(self, seconds: float) -> float:
        """A time *seconds* after the last tick started."""
        return self.clock.now + seconds

    def noted(self, text: str) -> bool:
        return any(text in note.text for note in self.ctx.notes)


class TestAStoredPositionTheJournalRejects:
    def test_it_does_not_stop_the_recording(self, journal, clock):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock, cursor="garbage")

        assert recording.tick() is False
        journal.log(recording.after_tick(100), _IO_ERROR)

        assert recording.tick() is True
        assert recording.kinds == ["io_error"]
        assert recording.noted("読み取り位置")
        assert Capability.KERNEL_LOG not in recording.ctx.unobserved

    def test_one_refused_later_is_read_again_from_the_time_of_the_last_read(
        self, journal, clock
    ):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.log(recording.after_tick(100), _IO_ERROR)
        journal.reject_cursors()

        assert recording.tick() is True

        assert recording.kinds == ["io_error"]
        assert journal.line_reads()[-1] == "since"
        assert recording.noted("読み取り位置")
        assert Capability.KERNEL_LOG not in recording.ctx.unobserved


class TestAStoredPositionWithNoNewestMessageToGoTo:
    def test_the_position_is_kept_and_the_whole_journal_is_not_read(
        self, journal, clock, monkeypatch
    ):
        for i in range(40):
            journal.log(_T0 - 3 * _DAY + i, _IO_ERROR)
        recording = _Recording(clock, cursor="garbage")
        monkeypatch.setattr(kernlog, "newest_cursor", lambda: None)

        assert recording.ticks(3) == [False] * 3

        assert recording.kinds == []
        assert "unbounded" not in journal.line_reads()


class TestAReadThatFailsOnce:
    def test_it_does_not_cost_the_lines_logged_while_nothing_recorded(
        self, journal, clock
    ):
        journal.log(_T0 - 500, _BOOT)
        saved = journal.log(_T0 - 400, "usb 1-1: new full-speed USB device")
        journal.log(_T0 - 300, _IO_ERROR)
        journal.log(_T0 - 200, _IO_ERROR)
        recording = _Recording(clock, cursor=saved)
        journal.fail_cursor_reads(1)

        assert recording.ticks(2) == [False, True]

        assert recording.kinds == ["io_error", "io_error"]
        assert recording.cursor == journal.newest()

    def test_it_does_not_count_a_line_again_in_a_quiet_stretch(self, journal, clock):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.log(_T0 + 2 * _INTERVAL - 0.4, _IO_ERROR)
        assert recording.tick() is True
        journal.fail_cursor_reads(1)

        assert recording.ticks(3) == [False, True, True]

        assert recording.kinds == ["io_error"]

    @pytest.mark.parametrize("failures", [1, 2, 5, -1])
    def test_it_does_not_read_the_history_of_a_quiet_journal(
        self, journal, clock, failures
    ):
        for i in range(40):
            journal.log(_T0 - 3 * _DAY + i, _IO_ERROR)
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.fail_cursor_reads(failures)

        recording.ticks(12)

        assert recording.kinds == []
        assert "unbounded" not in journal.line_reads()
        assert recording.cursor


class TestAReadThatKeepsFailing:
    def test_two_failed_ticks_keep_the_position_and_the_third_falls_back(
        self, journal, clock
    ):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.fail_cursor_reads()
        journal.log(recording.after_tick(100), _IO_ERROR)

        assert recording.ticks(3) == [False, False, True]

        assert recording.kinds == ["io_error"]
        assert recording.noted("読み取り位置")

    def test_it_then_reads_on_from_the_time_of_the_last_read_without_failing(
        self, journal, clock
    ):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.fail_cursor_reads()
        assert recording.ticks(3) == [False, False, True]
        journal.log(recording.after_tick(100), _IO_ERROR)

        assert recording.ticks(3) == [True, True, True]

        assert recording.kinds == ["io_error"]
        assert "unbounded" not in journal.line_reads()

    def test_it_goes_back_to_the_position_once_the_journal_reads_from_it(
        self, journal, clock
    ):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.fail_cursor_reads()
        assert recording.ticks(3) == [False, False, True]
        journal.fail_cursor_reads(0)
        journal.log(recording.after_tick(100), _IO_ERROR)

        assert recording.ticks(2) == [True, True]

        assert recording.kinds == ["io_error"]
        assert journal.line_reads()[-2:] == ["after-cursor", "after-cursor"]

    def test_a_failure_after_the_position_works_again_counts_from_one(
        self, journal, clock
    ):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.fail_cursor_reads()
        assert recording.ticks(3) == [False, False, True]
        journal.fail_cursor_reads(0)
        assert recording.tick() is True
        journal.log(recording.after_tick(100), _IO_ERROR)
        journal.fail_cursor_reads(1)

        assert recording.ticks(2) == [False, True]

        assert recording.kinds == ["io_error"]

    def test_a_read_that_returns_no_cursor_keeps_the_position(
        self, journal, clock, monkeypatch
    ):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        monkeypatch.setattr(kernlog, "read_events", lambda **kw: ([], None))

        assert recording.tick() is True

        assert recording.cursor == journal.newest()

    def test_a_journal_that_is_down_does_not_cost_the_position(self, journal, clock):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.go_down()
        journal.log(recording.after_tick(100), _IO_ERROR)

        assert recording.ticks(5) == [False] * 5
        journal.go_down(False)

        assert recording.ticks(3) == [True, True, True]
        assert recording.kinds == ["io_error"]


class TestAJournalThatFailsAfterItWasRead:
    def test_it_is_a_note_and_not_a_recording_without_the_kernel_log(
        self, journal, clock
    ):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.go_down()

        assert recording.ticks(4) == [False] * 4

        assert recording.noted("collect失敗")
        assert Capability.KERNEL_LOG not in recording.ctx.unobserved

    def test_one_that_was_never_read_leaves_the_rules_not_observed(
        self, journal, clock
    ):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        journal.go_down()

        assert recording.ticks(2) == [False, False]

        assert Capability.KERNEL_LOG in recording.ctx.unobserved


class TestAJournalThatCameBackAfterTwoFailedReads:
    """Two failed reads before any worked say nothing about what the third reads."""

    def test_the_third_read_takes_back_what_the_failed_ones_said(self, journal, clock):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        journal.fail_cursor_reads(2)
        journal.log(recording.after_tick(100), _IO_ERROR)

        assert recording.ticks(2) == [False, False]
        assert Capability.KERNEL_LOG in recording.ctx.unobserved
        assert recording.tick() is True

        assert recording.kinds == ["io_error"]
        assert Capability.KERNEL_LOG not in recording.ctx.unobserved
        assert recording.ctx.capabilities[Capability.KERNEL_LOG.value] is True

    def test_lines_the_position_skipped_over_keep_it_unobserved(self, journal, clock):
        journal.log(_T0 - 100, _BOOT)
        saved = journal.log(_T0 - 90, "usb 1-1: new full-speed USB device")
        recording = _Recording(clock, cursor=saved)
        journal.fail_cursor_reads(3)
        journal.log(recording.after_tick(10), _IO_ERROR)

        assert recording.ticks(4) == [False, False, False, True]

        assert recording.kinds == []
        assert Capability.KERNEL_LOG in recording.ctx.unobserved

    def test_a_journal_that_never_reads_stays_unobserved(self, journal, clock):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        journal.go_down()

        assert recording.ticks(6) == [False] * 6

        assert Capability.KERNEL_LOG in recording.ctx.unobserved


class TestWhatTheRecorderKeepsOfIt:
    @staticmethod
    def _recorded(journal, tmp_path, *, failing_reads: int):
        journal.log(time.time() - 10, _BOOT)

        def go_wrong() -> None:
            journal.fail_cursor_reads(failing_reads)
            journal.log(time.time(), _IO_ERROR)

        sampler = _ScriptedKernelLog({1: go_wrong})
        config = ProcdiagConfig(
            sampling=SamplingConfig(
                system_interval=0.1, kernel_log_interval=0.3, dir_scan_interval=5.0
            )
        )
        clock = FakeClock(0.0)
        db = tmp_path / "kept.db"
        recorder = Recorder(
            config,
            db_path=db,
            samplers={"system": FakeSampler(), "kernel_log": sampler},
            duration=2.0,
            clock=clock,
            sleep=_AdvancingSleep(clock),
        )
        assert recorder.run() == 0
        with Storage(db, read_only=True) as store:
            return (
                [
                    p.capability
                    for p in store.unobserved_periods()
                    if math.isfinite(p.start_ts)
                ],
                [r["kind"] for r in store.rows("kernel_events")],
                [
                    r["detail"]
                    for r in store.rows("ops_events", where="kind = 'unobserved'")
                ],
            )

    def test_two_failed_reads_then_a_good_one_leave_no_unobserved_period(
        self, journal, tmp_path
    ):
        capabilities, kinds, events = self._recorded(journal, tmp_path, failing_reads=2)

        assert kinds == ["io_error"]
        assert Capability.KERNEL_LOG not in capabilities
        assert events == []

    def test_a_journal_that_stays_unreadable_is_kept_as_unobserved(
        self, journal, tmp_path
    ):
        capabilities, kinds, events = self._recorded(
            journal, tmp_path, failing_reads=-1
        )

        assert kinds == []
        assert Capability.KERNEL_LOG in capabilities
        assert len(events) == 1


class TestAStreamOfLinesWhileThePositionKeepsFailing:
    def test_a_line_read_just_before_the_last_read_began_is_not_read_again(
        self, journal, clock
    ):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.fail_cursor_reads()
        journal.log(recording.after_tick(3 * _INTERVAL) - 0.3, _IO_ERROR)

        assert recording.ticks(3) == [False, False, True]
        assert recording.tick() is True

        assert recording.kinds == ["io_error"]

    def test_lines_in_every_second_are_read_once_each(self, journal, clock):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.fail_cursor_reads()

        for _ in range(8):
            for offset in (0.1, 0.2, 0.3, 0.4):
                journal.log(recording.after_tick(_INTERVAL) - offset, _IO_ERROR)
            recording.tick()

        assert len(recording.kinds) == 8 * 4

    def test_two_lines_that_differ_only_in_when_are_both_kept(self, journal, clock):
        journal.log(_T0 - 100, _BOOT)
        recording = _Recording(clock)
        assert recording.tick() is True
        journal.fail_cursor_reads()
        for _ in range(3):
            recording.tick()
        journal.log(recording.after_tick(_INTERVAL) - 0.3, _IO_ERROR)
        journal.log(recording.after_tick(_INTERVAL) - 0.2, _IO_ERROR)

        recording.ticks(2)

        assert recording.kinds == ["io_error", "io_error"]


class _ScriptedKernelLog(KernelLogSampler):
    """The kernel-log sampler, with something done to the journal before a tick."""

    def __init__(self, before: dict[int, Callable[[], None]]) -> None:
        super().__init__()
        self._before = before
        self._ticks = 0

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        self._ticks += 1
        self._before.get(self._ticks, lambda: None)()
        return super().collect(ctx)


class TestWhatTheRecorderKeeps:
    def test_a_collect_failure_is_kept_after_the_note_about_the_position(
        self, journal, tmp_path
    ):
        journal.log(time.time() - 10, _BOOT)
        sampler = _ScriptedKernelLog(
            {2: journal.reject_cursors, 4: journal.go_down},
        )
        config = ProcdiagConfig(
            sampling=SamplingConfig(
                system_interval=0.1, kernel_log_interval=0.3, dir_scan_interval=5.0
            )
        )
        clock = FakeClock(0.0)
        db = tmp_path / "kept.db"
        recorder = Recorder(
            config,
            db_path=db,
            samplers={"system": FakeSampler(), "kernel_log": sampler},
            duration=2.0,
            clock=clock,
            sleep=_AdvancingSleep(clock),
        )

        assert recorder.run() == 0

        with Storage(db, read_only=True) as store:
            kept = [
                row["detail"]
                for row in store.rows("ops_events", where="kind = 'sampler_failed'")
            ]
        assert len(kept) == 2, kept
        assert "読み取り位置" in kept[0]
        assert "collect失敗" in kept[1]
