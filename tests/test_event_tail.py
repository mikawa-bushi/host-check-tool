"""Events a recording writes after its last system sample still count.

A stop reads the kernel log and the services once more, and a flush that
recovers from a write outage records what it dropped; both land after the last
system sample, which is where ``check`` ends its window.  A period that ends
before the last sample keeps its events out, and every place that tells the
reader to look at the kernel log names all boots, not just the running one.
"""

from __future__ import annotations

from synth import add_sys_baseline, make_db
from test_observation import _add_kernel_errors
from test_rules_v4 import _svc

import procdiag.snapshot as snap
from procdiag.config import ProcdiagConfig
from procdiag.core.result import Window
from procdiag.diagnosis import Engine
from procdiag.diagnosis.crash import KernelErrorRule
from procdiag.kernlog import KernelEvent
from procdiag.samplers.kernel_log import KernelLogSampler

_IO_ERROR = "ディスク I/O エラー"


def _recorded_hours(tmp_path, hours: float = 3):
    store = make_db(tmp_path, hours=hours)
    add_sys_baseline(store, hours=hours)
    last = store.conn.execute("SELECT MAX(ts) FROM sys_samples").fetchone()[0]
    return store, last


def _diagnose(store, rule: str, **kwargs):
    store.flush()
    return Engine(ProcdiagConfig()).run(store, only=[rule], **kwargs)


class TestEventsAfterTheLastSample:
    def test_a_kernel_error_logged_in_the_last_interval_is_counted(self, tmp_path):
        store, last = _recorded_hours(tmp_path)
        _add_kernel_errors(store, last + 30.0)

        diag = _diagnose(store, "KERNEL-ERROR-01")

        assert [f.subject for f in diag.findings] == [_IO_ERROR]
        store.close()

    def test_a_service_failure_seen_by_the_stop_is_counted(self, tmp_path):
        store, last = _recorded_hours(tmp_path)
        _svc(store, last + 30.0, "db.service", "failed", 0, "oom-kill")

        diag = _diagnose(store, "SERVICE-RESTART-01")

        assert [f.subject for f in diag.findings] == ["db.service"]
        store.close()

    def test_the_rows_a_recovering_last_flush_dropped_are_noted(self, tmp_path):
        store, _ = _recorded_hours(tmp_path)
        store.record_event("data_lost", "DB に書けなかったため 20 行を捨てました")

        diag = _diagnose(store, "CPU-SATURATION-01")

        assert any("20 行" in n for n in diag.quality.notes), diag.quality.notes
        store.close()

    def test_a_period_that_ends_before_the_last_sample_leaves_them_out(self, tmp_path):
        store, last = _recorded_hours(tmp_path)
        end = last - 3600.0
        _add_kernel_errors(store, end + 30.0)
        store.record_event("data_lost", "DB に書けなかったため 20 行を捨てました")

        window = Window(start_ts=last - 3 * 3600.0, end_ts=end)
        errors = _diagnose(store, "KERNEL-ERROR-01", window=window)
        lost = _diagnose(store, "CPU-SATURATION-01", window=window)

        assert errors.findings == []
        assert not any("20 行" in n for n in lost.quality.notes)
        store.close()


class TestWhereToLookInTheKernelLog:
    def test_every_hint_reaches_back_past_the_running_boot(self, tmp_path, monkeypatch):
        store, last = _recorded_hours(tmp_path)
        _add_kernel_errors(store, last - 600.0)
        finding = _diagnose(store, "KERNEL-ERROR-01").findings[0]
        store.close()

        monkeypatch.setattr(
            snap.kernlog,
            "read_events",
            lambda since: ([KernelEvent(1.0, "io_error", "I/O error")], None),
        )
        (item,) = snap._check_kernel_log()

        assert "journalctl -k" in finding.proposal
        for text in (finding.proposal, item.hint, *KernelErrorRule.manual_commands):
            for command in text.split("`"):
                if command.startswith("journalctl -k"):
                    assert "-b all" in command, command
        for command in KernelLogSampler.manual_commands:
            if command.startswith("journalctl -k"):
                assert "-b all" in command, command
