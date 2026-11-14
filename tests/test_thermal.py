"""Tests for the thermal sampler + THERMAL-01 rule (issue v0.3 §3-9)."""

from __future__ import annotations

import time
from collections import namedtuple

import pytest
from synth import add_sys_baseline, make_db

import procdiag.samplers.thermal as thmod
import procdiag.snapshot as snap
from procdiag.config import ProcdiagConfig
from procdiag.core import Severity, Window
from procdiag.diagnosis import Engine
from procdiag.diagnosis.thermal import ThermalThrottleRule
from procdiag.samplers.thermal import ThermalSampler
from procdiag.storage import Storage

_Sensor = namedtuple("_Sensor", "label current high critical")
_Freq = namedtuple("_Freq", "current min max")
_MAX_MHZ = 4000.0
_STEP_S = 10.0
_DAY_STEPS = 24 * 360


def _insert_thermal(store, *, temp, cur, mx, count, start_ts, interval=10.0):
    rows = [(start_ts + i * interval, temp, cur, mx) for i in range(count)]
    store.buffer("thermal_samples", rows)
    store.flush()


def _window(start, end):
    return Window(start_ts=start, end_ts=end)


def _diagnose(tmp_path, sample_at):
    """Run THERMAL-01 over a day of 10-second rows from ``sample_at(i) -> (°C, MHz)``.

    ``sample_at`` returns None where the recorder was stopped.
    """
    store = make_db(tmp_path, hours=24)
    add_sys_baseline(store, hours=24)
    t0 = time.time() - 24 * 3600 + 120
    samples = ((t0 + i * _STEP_S, sample_at(i)) for i in range(_DAY_STEPS - 24))
    rows = [(ts, *s, _MAX_MHZ) for ts, s in samples if s is not None]
    store.buffer("thermal_samples", rows)
    store.flush()
    diag = Engine(ProcdiagConfig()).run(store, only=["THERMAL-01"])
    store.close()
    assert diag.evaluated_rules == ["THERMAL-01"]
    return diag.findings


class TestThermalTimeAlignment:
    def test_hot_job_at_full_clock_on_a_power_saving_host_is_not_throttling(
        self, tmp_path
    ):
        job = range(12 * 360, 12 * 360 + 60)
        findings = _diagnose(
            tmp_path, lambda i: (88.0, _MAX_MHZ) if i in job else (45.0, 1200.0)
        )
        assert findings == []

    def test_hours_hot_and_slowed_in_an_otherwise_cool_day_are_reported(self, tmp_path):
        shift = range(8 * 360, 16 * 360)
        findings = _diagnose(
            tmp_path, lambda i: (92.0, 2000.0) if i in shift else (55.0, _MAX_MHZ)
        )
        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "THERMAL-01"
        assert f.severity == Severity.WARNING
        assert f.evidence["peak_temp_c"] == 92.0
        assert f.evidence["median_freq_ratio"] == 0.5
        assert f.evidence["duration_minutes"] == pytest.approx(480, abs=1)

    def test_a_recording_stop_inside_hot_hours_is_not_counted_as_throttled_time(
        self, tmp_path
    ):
        stop = range(7 * 360, 7 * 360 + 240)

        def two_hot_hours_around_a_40_minute_stop(i):
            if i in stop:
                return None
            if 6 * 360 <= i < 8 * 360 + 240:
                return (92.0, 2000.0)
            return (55.0, _MAX_MHZ)

        (f,) = _diagnose(tmp_path, two_hot_hours_around_a_40_minute_stop)
        assert f.evidence["duration_minutes"] == pytest.approx(120, abs=1)

    def test_single_hot_and_slow_moments_are_not_sustained_throttling(self, tmp_path):
        def cooldown_after_each_job(i):
            if i % 360 == 0:
                return (88.0, 1200.0)
            if i % 360 in (100, 101):
                return (84.0, 1200.0)
            return (45.0, 1200.0)

        assert _diagnose(tmp_path, cooldown_after_each_job) == []


class TestThermalRule:
    def test_detects_throttling(self, tmp_path):
        db = tmp_path / "th.db"
        with Storage(db) as store:
            t0 = time.time() - 3600
            # hot (90 °C) with clock pinned to 60 % of max → throttling
            _insert_thermal(
                store, temp=90.0, cur=1200.0, mx=2000.0, count=6, start_ts=t0
            )
            findings = ThermalThrottleRule().evaluate(store, _window(t0, t0 + 60))
        assert len(findings) == 1
        assert findings[0].rule_id == "THERMAL-01"
        assert findings[0].severity == Severity.WARNING
        assert findings[0].evidence["peak_temp_c"] == 90.0

    def test_no_throttle_when_cool(self, tmp_path):
        db = tmp_path / "th2.db"
        with Storage(db) as store:
            t0 = time.time() - 3600
            # low clock ratio but cool → not a thermal problem
            _insert_thermal(
                store, temp=50.0, cur=1200.0, mx=2000.0, count=6, start_ts=t0
            )
            findings = ThermalThrottleRule().evaluate(store, _window(t0, t0 + 60))
        assert findings == []

    def test_no_throttle_when_full_clock(self, tmp_path):
        db = tmp_path / "th3.db"
        with Storage(db) as store:
            t0 = time.time() - 3600
            # hot but clock near max → not throttling
            _insert_thermal(
                store, temp=90.0, cur=1950.0, mx=2000.0, count=6, start_ts=t0
            )
            findings = ThermalThrottleRule().evaluate(store, _window(t0, t0 + 60))
        assert findings == []

    def test_no_data_no_finding(self, tmp_path):
        db = tmp_path / "th4.db"
        with Storage(db) as store:
            t0 = time.time() - 3600
            findings = ThermalThrottleRule().evaluate(store, _window(t0, t0 + 60))
        assert findings == []


def _fake_sensors(monkeypatch, sensors):
    monkeypatch.setattr(
        thmod.psutil,
        "sensors_temperatures",
        lambda fahrenheit=False: sensors,
        raising=False,
    )
    monkeypatch.setattr(
        thmod.psutil, "cpu_freq", lambda percpu=False: _Freq(900.0, 800.0, 4200.0)
    )


def _recorded_temp(monkeypatch, sensors):
    _fake_sensors(monkeypatch, sensors)
    (batch,) = ThermalSampler().collect(None)
    return batch.rows[0][1]


_NVME = [_Sensor("Composite", 82.85, 84.85, 89.85)]
_IDLE_CPU = [
    _Sensor("Package id 0", 48.0, 100.0, 100.0),
    _Sensor("Core 0", 46.0, 100.0, 100.0),
]


class TestCpuTemperature:
    @pytest.mark.parametrize(
        "sensors, cpu_temp",
        [
            (
                {
                    "coretemp": _IDLE_CPU,
                    "nvme": _NVME,
                    "acpitz": [_Sensor("", 27.8, None, None)],
                },
                48.0,
            ),
            (
                {
                    "k10temp": [_Sensor("Tctl", 51.0, None, None)],
                    "amdgpu": [_Sensor("edge", 79.0, None, None)],
                    "nvme": _NVME,
                },
                51.0,
            ),
            ({"cpu_thermal": [_Sensor("", 55.0, None, None)], "nvme": _NVME}, 55.0),
        ],
        ids=["intel", "amd", "arm"],
    )
    def test_records_the_cpu_not_a_hotter_drive_or_gpu(
        self, monkeypatch, sensors, cpu_temp
    ):
        assert _recorded_temp(monkeypatch, sensors) == cpu_temp

    def test_without_a_cpu_sensor_falls_back_to_the_acpi_thermal_zone(
        self, monkeypatch
    ):
        sensors = {"acpitz": [_Sensor("", 61.0, None, None)], "nvme": _NVME}
        assert _recorded_temp(monkeypatch, sensors) == 61.0

    def test_without_cpu_or_system_zone_no_temperature_is_recorded(self, monkeypatch):
        sensors = {"nvme": _NVME, "iwlwifi_1": [_Sensor("", 70.0, None, None)]}
        assert _recorded_temp(monkeypatch, sensors) is None

    def test_snapshot_does_not_show_a_hot_drive_as_the_cpu(self, monkeypatch):
        _fake_sensors(monkeypatch, {"coretemp": _IDLE_CPU, "nvme": _NVME})
        (item,) = snap._check_temperature(ProcdiagConfig())
        assert item.label == "CPU 温度"
        assert item.status == "ok"
        assert item.detail.startswith("48°C")

    def test_snapshot_without_a_cpu_sensor_reports_no_temperature(self, monkeypatch):
        _fake_sensors(monkeypatch, {"nvme": _NVME})
        (item,) = snap._check_temperature(ProcdiagConfig())
        assert item.status == "info"
        assert "83" not in item.detail


class TestThermalSampler:
    def test_collect_emits_row(self, monkeypatch):
        class _Temp:
            current = 70.0

        class _Freq:
            current = 1500.0
            max = 2000.0

        monkeypatch.setattr(
            thmod.psutil,
            "sensors_temperatures",
            lambda: {"coretemp": [_Temp()]},
            raising=False,
        )
        monkeypatch.setattr(thmod.psutil, "cpu_freq", lambda: _Freq())

        batches = ThermalSampler().collect(None)
        assert len(batches) == 1
        assert batches[0].table == "thermal_samples"
        ts, temp, cur, mx = batches[0].rows[0]
        assert temp == 70.0
        assert cur == 1500.0
        assert mx == 2000.0

    def test_collect_silent_when_unreadable(self, monkeypatch):
        def _boom():
            raise OSError("no sensors")

        monkeypatch.setattr(thmod.psutil, "sensors_temperatures", _boom, raising=False)
        monkeypatch.setattr(thmod.psutil, "cpu_freq", lambda: None)

        assert ThermalSampler().collect(None) == []
