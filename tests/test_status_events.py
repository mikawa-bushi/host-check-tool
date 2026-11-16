"""``procdiag status`` lists the recent events that happened, not the start-up notes."""

from __future__ import annotations

from procdiag.core import Capability
from procdiag.status import collect_status
from procdiag.storage import Storage


def test_what_a_session_could_not_observe_does_not_fill_the_recent_events(tmp_path):
    with Storage(tmp_path / "status.db") as store:
        for capability in (
            Capability.PSI,
            Capability.CGROUP_V2,
            Capability.KERNEL_LOG,
            Capability.SYSTEMD,
            Capability.THERMAL,
        ):
            store.record_unobserved(capability, f"{capability.value}: 読めません")
        store.record_event("gap", "12.0s ジョブt1_system")
        store.record_event("stopped", "正常終了")

        events = collect_status(store)["recent_events"]

    assert [e["kind"] for e in events] == ["gap", "stopped"]
