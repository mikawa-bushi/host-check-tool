"""Tests for the thermal sampler + THERMAL-01 rule (issue v0.3 §3-9)."""

from __future__ import annotations

import time

import procdiag.samplers.thermal as thmod
from procdiag.core import Severity, Window
from procdiag.diagnosis.thermal import ThermalThrottleRule
from procdiag.samplers.thermal import ThermalSampler
from procdiag.storage import Storage


def _insert_thermal(store, *, temp, cur, mx, count, start_ts, interval=10.0):
    rows = [(start_ts + i * interval, temp, cur, mx) for i in range(count)]
    store.buffer("thermal_samples", rows)
    store.flush()


def _window(start, end):
    return Window(start_ts=start, end_ts=end)


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
