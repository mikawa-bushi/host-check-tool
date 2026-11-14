"""THERMAL-01: detect CPU thermal throttling from temperature + clock data.

Thermal throttling is a *physical* slowdown (the CPU is clocked down to shed
heat) that PSI cannot distinguish from genuine demand.  THERMAL-01 flags a
sustained current-vs-max clock drop while the package is hot, so the operator
knows to look at cooling / dust / power profile rather than the workload.
Temperature and clock are judged per sample, so a power-saving idle clock and
a hot moment at full clock at different times are not throttling.
"""

from __future__ import annotations

import statistics
from itertools import groupby
from typing import TYPE_CHECKING

from ..core import (
    Capability,
    Confidence,
    Finding,
    Rule,
    Severity,
    Window,
    register,
)
from ..core.util import humanize_duration

if TYPE_CHECKING:
    from ..storage import Storage

# Decision boundaries (kept here so explain/report can cite them verbatim).
_TEMP_HOT_C = 80.0
_FREQ_THROTTLE_RATIO = 0.85  # current clock < 85% of max while hot => throttling
_MIN_CONSECUTIVE_SAMPLES = 3


@register
class ThermalThrottleRule(Rule):
    rule_id = "THERMAL-01"
    category = "cpu"
    title = "CPU サーマルスロットリング疑い"
    default_severity = Severity.WARNING
    # Thermal events are fast; do not gate on a long observation window.
    min_observation_hours = 0.0
    requires_rows = ("thermal_samples",)
    requires_observed = (Capability.THERMAL,)
    what = "高温下で CPU クロックが最大値より継続的に低い状態を検出する"
    why = "PSI(需要)では区別できない物理的な熱制限を、温度×クロック比で識別するため"
    manual_commands = [
        "sensors",
        "watch -n1 'grep MHz /proc/cpuinfo'",
    ]

    temp_hot_c: float = _TEMP_HOT_C
    freq_throttle_ratio: float = _FREQ_THROTTLE_RATIO
    min_points: int = _MIN_CONSECUTIVE_SAMPLES

    def _hot_and_slow(self, row) -> bool:
        temp, cur, mx = (
            row["temp_max_c"],
            row["cpu_freq_cur_mhz"],
            row["cpu_freq_max_mhz"],
        )
        return (
            temp is not None
            and cur is not None
            and bool(mx)
            and temp >= self.temp_hot_c
            and cur / mx < self.freq_throttle_ratio
        )

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        rows = store.rows("thermal_samples", since=window.start_ts, until=window.end_ts)
        runs = [list(run) for hit, run in groupby(rows, self._hot_and_slow) if hit]
        throttled = [r for run in runs if len(run) >= self.min_points for r in run]
        if not throttled:
            return []

        # Sample count times the usual spacing, not first-to-last, so a
        # recorder stop inside a hot run is not counted as throttled time.
        spacing = [b["ts"] - a["ts"] for a, b in zip(rows, rows[1:])]
        duration = len(throttled) * statistics.median(spacing or [0.0])
        peak_temp = max(r["temp_max_c"] for r in throttled)
        med_ratio = statistics.median(
            r["cpu_freq_cur_mhz"] / r["cpu_freq_max_mhz"] for r in throttled
        )
        evidence = {
            "peak_temp_c": round(peak_temp, 1),
            "median_freq_ratio": round(med_ratio, 3),
            "freq_throttle_ratio": self.freq_throttle_ratio,
            "temp_threshold_c": self.temp_hot_c,
            "sample_count": len(throttled),
            "min_consecutive_samples": self.min_points,
            "duration_minutes": round(duration / 60, 1),
        }
        summary = (
            f"高温 {peak_temp:.0f}°C 下でクロックが最大の "
            f"{med_ratio * 100:.0f}% まで低下 (計 {humanize_duration(duration)})"
            " — サーマルスロットリングの疑い"
        )
        return [
            Finding(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                subject="CPU",
                summary=summary,
                evidence=evidence,
                detected_at=(throttled[0]["ts"], throttled[-1]["ts"]),
                confidence=Confidence.MEDIUM,
            )
        ]
