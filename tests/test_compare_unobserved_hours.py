"""``compare`` weighs a clear period by the hours that could be observed.

An after period of 18 recorded hours, 6 of them without PSI, saw 12 hours of
CPU saturation at most; against a before period of 15 hours that is too short
to call the saturation gone.
"""

from __future__ import annotations

import time

from synth import add_cpu_saturation_samples, add_sys_baseline, make_db
from test_observation import _HOUR, _compare, _recording_without_psi_then_hole


def test_the_hours_without_psi_do_not_count_as_observed(tmp_path, monkeypatch):
    now = time.time()
    before = make_db(tmp_path / "before", hours=15)
    add_sys_baseline(before, hours=15, start_ts=now - 80 * _HOUR)
    add_cpu_saturation_samples(before, start_ts=now - 78 * _HOUR)
    before_path = before.path
    before.close()
    after = tmp_path / "after.db"
    _recording_without_psi_then_hole(after, monkeypatch, now - 30 * _HOUR)

    _code, data = _compare(before_path, after, "CPU-SATURATION-01")

    (row,) = data["rows"]
    assert row["before"]["state"] == "detected"
    assert row["verdict"] == "not_evaluable"
