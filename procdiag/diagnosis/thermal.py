"""THERMAL-01: detect CPU thermal throttling from temperature + clock data.

Thermal throttling is a *physical* slowdown (the CPU is clocked down to shed
heat) that PSI cannot distinguish from genuine demand.  THERMAL-01 flags a
sustained current-vs-max clock drop while the package is hot, so the operator
knows to look at cooling / dust / power profile rather than the workload.
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

from ..core import Confidence, Finding, Rule, Severity, Window, register

if TYPE_CHECKING:
    from ..storage import Storage

# Decision boundaries (kept here so explain/report can cite them verbatim).
_TEMP_HOT_C = 80.0
_FREQ_THROTTLE_RATIO = 0.85  # median current clock < 85% of max => throttling
_MIN_POINTS = 3


@register
class ThermalThrottleRule(Rule):
    rule_id = "THERMAL-01"
    category = "cpu"
    title = "CPU サーマルスロットリング疑い"
    default_severity = Severity.WARNING
    # Thermal events are fast; do not gate on a long observation window.
    min_observation_hours = 0.0
    what = "高温下で CPU クロックが最大値より継続的に低い状態を検出する"
    why = "PSI(需要)では区別できない物理的な熱制限を、温度×クロック比で識別するため"
    manual_commands = [
        "sensors",
        "watch -n1 'grep MHz /proc/cpuinfo'",
    ]

    temp_hot_c: float = _TEMP_HOT_C
    freq_throttle_ratio: float = _FREQ_THROTTLE_RATIO
    min_points: int = _MIN_POINTS

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        rows = store.rows("thermal_samples", since=window.start_ts, until=window.end_ts)
        temps = [r["temp_max_c"] for r in rows if r["temp_max_c"] is not None]
        ratios = [
            r["cpu_freq_cur_mhz"] / r["cpu_freq_max_mhz"]
            for r in rows
            if r["cpu_freq_cur_mhz"] is not None and r["cpu_freq_max_mhz"]
        ]
        if len(temps) < self.min_points or len(ratios) < self.min_points:
            return []

        peak_temp = max(temps)
        med_ratio = statistics.median(ratios)
        if peak_temp < self.temp_hot_c or med_ratio >= self.freq_throttle_ratio:
            return []

        evidence = {
            "peak_temp_c": round(peak_temp, 1),
            "median_freq_ratio": round(med_ratio, 3),
            "freq_throttle_ratio": self.freq_throttle_ratio,
            "temp_threshold_c": self.temp_hot_c,
            "sample_count": len(ratios),
        }
        summary = (
            f"高温 {peak_temp:.0f}°C 下でクロックが最大の "
            f"{med_ratio * 100:.0f}% まで低下 — サーマルスロットリングの疑い"
        )
        return [
            Finding(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                subject="CPU",
                summary=summary,
                evidence=evidence,
                detected_at=(window.start_ts, window.end_ts),
                confidence=Confidence.MEDIUM,
            )
        ]
