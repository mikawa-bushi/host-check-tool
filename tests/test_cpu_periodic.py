"""CPU-PERIODIC-01 reports a schedule only when one exists, at host local time."""

from __future__ import annotations

import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from synth import add_spike_events, add_sys_baseline, make_db

from procdiag.config import ProcdiagConfig, TriggerConfig
from procdiag.core import Severity
from procdiag.core.sampler import SystemSnapshot
from procdiag.diagnosis import Engine, detect_periodicity
from procdiag.samplers.trigger import SpikeTrigger

_H = 3600.0
_D = 86400.0
_MIDNIGHT_UTC = 1_780_000_000.0 - (1_780_000_000.0 % _D)


@pytest.fixture
def host_tz(monkeypatch):
    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


def _findings(tmp_path, spikes, *, start, hours):
    store = make_db(tmp_path, hours=hours)
    add_sys_baseline(store, hours=hours, start_ts=start)
    add_spike_events(store, "cpu", spikes)
    diag = Engine(ProcdiagConfig()).run(store, only=["CPU-PERIODIC-01"])
    store.close()
    return [f for f in diag.findings if f.rule_id == "CPU-PERIODIC-01"]


def _job_spikes(rng, job_starts):
    """Spike events of a CPU-heavy job: pressure is seen 3-75 s after the
    scheduled time (PSI averaging, systemd AccuracySec) and the trigger
    re-fires every 30 s while the run lasts 20 s to 5 min."""
    spikes = []
    for start in job_starts:
        onset = start + rng.uniform(3, 75)
        end = onset + rng.uniform(20, 300)
        t = onset
        while t <= end:
            spikes.append(t)
            t += 30.0
    return spikes


def _trigger_spikes(start, seconds):
    trigger = SpikeTrigger(TriggerConfig(), psi_available=True)
    spikes = []
    for i in range(int(seconds)):
        snap = SystemSnapshot(
            ts=start + i,
            cpu_total=97.0,
            cpu_max_core=100.0,
            mem_total=8 << 30,
            mem_available=6 << 30,
            swap_out_delta=0,
            psi={"cpu_some10": 60.0, "mem_some10": 0.0, "mem_full10": 0.0},
        )
        if trigger.observe(snap, float(i)) is not None:
            spikes.append(start + i)
    return spikes


class TestHostLocalTime:
    def test_daily_job_is_reported_at_the_local_time_crontab_shows(
        self, tmp_path, host_tz
    ):
        host_tz("Asia/Tokyo")
        jst = ZoneInfo("Asia/Tokyo")
        start = datetime(2026, 5, 1, tzinfo=jst).timestamp()
        spikes = [
            datetime(2026, 5, 1 + day, 12, 0, 30, tzinfo=jst).timestamp()
            for day in range(4)
        ]

        findings = _findings(tmp_path, spikes, start=start, hours=4 * 24)

        assert [f.summary for f in findings] == ["日次周期スパイク (12:00 頃、4回観測)"]
        assert "毎日 12:00 頃" in findings[0].proposal

    def test_hourly_job_minute_is_local_in_a_half_hour_zone(self, tmp_path, host_tz):
        host_tz("Asia/Kolkata")
        ist = ZoneInfo("Asia/Kolkata")
        start = datetime(2026, 5, 1, 9, 0, tzinfo=ist).timestamp()
        spikes = [start + h * _H + 30 * 60 + 20 for h in range(6)]

        findings = _findings(tmp_path, spikes, start=start, hours=6)

        assert [f.summary for f in findings] == ["時次周期スパイク (:30 頃、6回観測)"]


class TestNoScheduleIsNotReported:
    @pytest.mark.parametrize("per_day, days", [(10, 2), (20, 1), (30, 2)])
    def test_spikes_at_random_times_are_not_a_schedule(self, per_day, days):
        reported = []
        for seed in range(200):
            rng = random.Random(seed)
            spikes = sorted(
                _MIDNIGHT_UTC + rng.uniform(0, days * _D) for _ in range(per_day * days)
            )
            if detect_periodicity(spikes) is not None:
                reported.append(seed)
        assert len(reported) <= 2, reported[:10]

    def test_random_spikes_in_a_recording_give_no_finding(self, tmp_path):
        rng = random.Random(1)
        start = _MIDNIGHT_UTC + 3 * _H
        spikes = sorted(start + rng.uniform(0, _D) for _ in range(20))

        assert _findings(tmp_path, spikes, start=start, hours=25) == []

    def test_one_long_cpu_load_is_not_a_schedule(self, tmp_path):
        start = _MIDNIGHT_UTC + 3 * _H + 7 * 60
        spikes = _trigger_spikes(start, 5 * _H)
        assert len(spikes) > 100

        assert _findings(tmp_path, spikes, start=start - 60, hours=6) == []


class TestRealScheduleIsReported:
    def test_daily_job_amid_random_spikes(self, tmp_path):
        rng = random.Random(7)
        start = _MIDNIGHT_UTC + 6 * _H
        days = 7
        spikes = _job_spikes(
            rng, [_MIDNIGHT_UTC + d * _D + 3 * _H for d in range(1, 8)]
        )
        spikes += [start + rng.uniform(0, days * _D) for _ in range(20 * days)]

        findings = _findings(tmp_path, sorted(spikes), start=start, hours=days * 24)

        assert [f.summary for f in findings] == ["日次周期スパイク (03:00 頃、7回観測)"]
        assert findings[0].severity is Severity.WARNING

    def test_hourly_job_over_several_days_is_hourly(self, tmp_path):
        rng = random.Random(3)
        start = _MIDNIGHT_UTC
        spikes = _job_spikes(rng, [start + h * _H + 15 * 60 for h in range(72)])

        findings = _findings(tmp_path, spikes, start=start, hours=72)

        assert [f.summary for f in findings] == ["時次周期スパイク (:15 頃、72回観測)"]

    def test_daily_job_on_a_quiet_host_is_not_hourly(self, tmp_path):
        rng = random.Random(5)
        spikes = _job_spikes(rng, [_MIDNIGHT_UTC + d * _D + 3 * _H for d in range(5)])

        findings = _findings(tmp_path, spikes, start=_MIDNIGHT_UTC, hours=5 * 24)

        assert [f.summary for f in findings] == ["日次周期スパイク (03:00 頃、5回観測)"]

    @pytest.mark.parametrize(
        "kind, job_starts, anchor",
        [
            ("daily", [_MIDNIGHT_UTC + d * _D + 3 * _H for d in range(5)], "03:00"),
            ("hourly", [_MIDNIGHT_UTC + h * _H + 15 * 60 for h in range(6)], ":15"),
        ],
    )
    def test_jittered_jobs_are_found_at_their_scheduled_time(
        self, kind, job_starts, anchor
    ):
        found = []
        for seed in range(200):
            spikes = _job_spikes(random.Random(seed), job_starts)
            period = detect_periodicity(spikes)
            if period is not None and (period.kind, period.anchor) == (kind, anchor):
                found.append(seed)
        assert len(found) >= 190


class TestScheduleAcrossTheBoundary:
    def test_daily_job_straddling_midnight_counts_every_day(self, tmp_path):
        spikes = [
            _MIDNIGHT_UTC + d * _D + 23 * _H + 59 * 60 + (40 if d % 2 == 0 else 80)
            for d in range(6)
        ]

        findings = _findings(tmp_path, spikes, start=_MIDNIGHT_UTC, hours=7 * 24)

        assert [f.summary for f in findings] == ["日次周期スパイク (23:59 頃、6回観測)"]
        assert findings[0].severity is Severity.WARNING

    def test_hourly_job_straddling_the_hour_counts_every_hour(self, tmp_path):
        lags = [40, 80, 55, 75, 45, 90, 50, 70]
        start = _MIDNIGHT_UTC + 2 * _H
        spikes = [start + h * _H + 59 * 60 + lag for h, lag in enumerate(lags)]

        findings = _findings(tmp_path, spikes, start=start, hours=9)

        assert [f.summary for f in findings] == ["時次周期スパイク (:59 頃、8回観測)"]
