"""Measured rules report a value for every subject, firing or not."""

from __future__ import annotations

import pytest
from synth import add_proc_series, add_sys_baseline, add_zombie_series, make_db

from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Headline, MeasuredRule
from procdiag.diagnosis import Engine

_MB = 1024 * 1024


def _engine() -> Engine:
    return Engine(ProcdiagConfig(rules=RulesConfig(min_observation_hours=6.0)))


class TestMeasuredRuleContract:
    def test_overriding_evaluate_is_rejected(self):
        with pytest.raises(TypeError, match="evaluate"):

            class _Bad(MeasuredRule):
                rule_id = "X-01"
                limit: float = 1.0
                headline = Headline("x", "/h", threshold="limit")

                def evaluate(self, store, window):
                    return []

    def test_headline_threshold_must_be_a_tunable(self):
        with pytest.raises(TypeError, match="threshold"):

            class _Bad(MeasuredRule):
                rule_id = "X-02"
                headline = Headline("x", "/h", threshold="no_such_knob")

    def test_headline_is_required(self):
        with pytest.raises(TypeError, match="headline"):

            class _Bad(MeasuredRule):
                rule_id = "X-03"


class TestMemLeakMeasurements:
    def test_every_process_is_measured_and_only_the_leak_fires(self, tmp_path):
        store = make_db(tmp_path, hours=12)
        add_sys_baseline(store, hours=12)
        add_proc_series(
            store, "leaky", rss_start=200 * _MB, rss_slope_per_h=38 * _MB, hours=12
        )
        add_proc_series(
            store, "steady", rss_start=300 * _MB, rss_slope_per_h=0, hours=12
        )
        diag = _engine().run(store, only=["MEM-LEAK-01"])
        store.close()

        by_name = {m.observation.subject: m for m in diag.measurements["MEM-LEAK-01"]}
        assert set(by_name) == {"leaky", "steady"}
        leaky, steady = by_name["leaky"], by_name["steady"]
        assert leaky.observation.identity == "hash_leaky"
        assert leaky.observation.value == pytest.approx(38, rel=0.05)
        assert leaky.finding is diag.findings[0]
        assert leaky.finding.evidence["slope_mb_per_h"] == leaky.observation.value
        assert steady.finding is None
        assert abs(steady.observation.value) < 1.0

    @pytest.mark.parametrize(
        "rule_id, kwargs, evidence_key",
        [
            ("FD-LEAK-01", {"fds_slope": 10}, "slope_fd_per_h"),
            ("THREAD-LEAK-01", {"threads_slope": 10}, "slope_threads_per_h"),
        ],
    )
    def test_fd_and_thread_leaks_measure_every_process(
        self, tmp_path, rule_id, kwargs, evidence_key
    ):
        store = make_db(tmp_path, hours=12)
        add_sys_baseline(store, hours=12)
        add_proc_series(
            store, "leaky", rss_start=50 * _MB, rss_slope_per_h=0, hours=12, **kwargs
        )
        add_proc_series(
            store, "steady", rss_start=50 * _MB, rss_slope_per_h=0, hours=12
        )
        diag = _engine().run(store, only=[rule_id])
        store.close()

        by_name = {m.observation.subject: m for m in diag.measurements[rule_id]}
        assert by_name["leaky"].observation.value == pytest.approx(10, rel=0.05)
        assert by_name["leaky"].finding.evidence[evidence_key] == pytest.approx(
            by_name["leaky"].observation.value
        )
        assert by_name["steady"].finding is None
        assert by_name["steady"].observation.value == 0.0

    def test_zombie_count_is_measured_when_flat(self, tmp_path):
        store = make_db(tmp_path, hours=8)
        add_sys_baseline(store, hours=8)
        add_zombie_series(store, hours=8, base_zombie_count=3)
        diag = _engine().run(store, only=["ZOMBIE-01"])
        store.close()
        (m,) = diag.measurements["ZOMBIE-01"]
        assert m.observation.subject == "zombie_processes"
        assert m.observation.value == 0.0
        assert m.observation.evidence["method"] == "approx"
        assert m.finding is None

    def test_skipped_rule_has_no_measurements(self, tmp_path):
        store = make_db(tmp_path, hours=2)
        add_sys_baseline(store, hours=2)
        add_proc_series(store, "leaky", rss_start=0, rss_slope_per_h=38 * _MB, hours=2)
        diag = _engine().run(store, only=["MEM-LEAK-01"])
        store.close()
        assert diag.measurements == {}
        assert [r.rule_id for r in diag.skipped_rules] == ["MEM-LEAK-01"]
