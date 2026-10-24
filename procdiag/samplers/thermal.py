"""Tier-3 thermal sampler: CPU temperature and clock-throttling signals.

Capability-gated: hosts without readable sensors / cpufreq simply record no
rows, and ``ctx.capabilities["thermal"]`` is set to ``False`` so ``doctor`` can
surface it.  Reads only local ``psutil`` (which wraps ``/sys/class/thermal`` and
``/sys/.../cpufreq``) — no external calls, no root required.

THERMAL-01 correlates a sustained current-vs-max clock drop with a high
temperature to flag *thermal throttling* — a physical slowdown whose remedy
(cooling, dust, power profile) differs from application demand (which PSI
already covers).
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

import psutil

from ..core.sampler import RunContext, SampleBatch, Sampler, register_sampler
from ..storage import make_row


@register_sampler
class ThermalSampler(Sampler):
    """Tier-3 sampler: hottest core temperature and current/max CPU frequency."""

    name = "thermal"
    title = "CPU温度・クロック (サーマルスロットリング検出)"
    tier = "T3"
    interval_key = "thermal_interval"
    requires_root = False
    what = "CPU最大温度と現在/最大クロックを定期記録する"
    why = "熱によるクロック低下(物理制限)はPSI(需要)と区別できないため、温度×クロック比で識別する"
    manual_commands = [
        "sensors",
        "grep MHz /proc/cpuinfo",
    ]

    def setup(self, ctx: RunContext) -> None:
        """Probe sensor / cpufreq availability and record the capability."""
        has_temp = self._read_temp() is not None
        cur, mx = self._read_freq()
        has_freq = cur is not None or mx is not None
        available = has_temp or has_freq
        ctx.capabilities["thermal"] = available
        if not available:
            ctx.notes.append(
                "thermal: 温度センサ・cpufreq が読めません — サーマル検出を無効化"
            )

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Record one (temp_max, freq_cur, freq_max) row when anything is readable."""
        ts = time.time()
        temp = self._read_temp()
        freq_cur, freq_max = self._read_freq()
        if temp is None and freq_cur is None and freq_max is None:
            return []  # nothing readable this tick — stay silent
        row = make_row(
            "thermal_samples",
            ts=ts,
            temp_max_c=temp,
            cpu_freq_cur_mhz=freq_cur,
            cpu_freq_max_mhz=freq_max,
        )
        return [SampleBatch(table="thermal_samples", rows=[row])]

    @staticmethod
    def _read_temp() -> Optional[float]:
        """Return the hottest reported core temperature in °C, or None."""
        fn = getattr(psutil, "sensors_temperatures", None)
        if fn is None:
            return None
        try:
            temps = fn()
        except Exception:
            return None
        best: Optional[float] = None
        for entries in (temps or {}).values():
            for entry in entries:
                cur = getattr(entry, "current", None)
                if cur is not None and (best is None or cur > best):
                    best = float(cur)
        return best

    @staticmethod
    def _read_freq() -> Tuple[Optional[float], Optional[float]]:
        """Return ``(current_mhz, max_mhz)``, each None when unavailable."""
        try:
            f = psutil.cpu_freq()
        except Exception:
            return (None, None)
        if f is None:
            return (None, None)
        cur = getattr(f, "current", None)
        mx = getattr(f, "max", None)
        cur = float(cur) if cur else None
        mx = float(mx) if mx else None  # a reported 0.0 max means "unknown"
        return (cur, mx)
