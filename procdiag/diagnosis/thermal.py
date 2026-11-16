"""THERMAL-01: detect CPU thermal throttling from temperature + clock data.

Thermal throttling is a *physical* slowdown (the CPU is clocked down to shed
heat) that PSI cannot distinguish from genuine demand.  THERMAL-01 flags a
sustained current-vs-max clock drop while the package is hot, so the operator
knows to look at cooling / dust / power profile rather than the workload.
Temperature and clock are judged per sample, so a power-saving idle clock and
a hot moment at full clock at different times are not throttling.  A run of
such samples counts when it lasts ``min_seconds``, measured from the spacing of
the samples around it, so the sampling interval and the recorder stopping
inside a hot spell change neither the threshold nor the total.
"""

from __future__ import annotations

import statistics
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
# A run needs this long, and two samples to have a length at all.  25 s is
# three samples at the default 10 s interval, with room for the jitter in when
# the samples were taken.
_MIN_RUN_SECONDS = 25.0
_MIN_CONSECUTIVE_SAMPLES = 2
# A step between samples longer than this many times the spacing of the
# samples around it is a hole (the recorder was stopped): the samples on either
# side are not consecutive.  The spacing is the median of this many steps on
# each side, so a different sampling interval in another session of the
# recording does not decide it.
_HOLE_FACTOR = 3.0
_NEARBY_STEPS = 8


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
    what = (
        "高温 (80°C 以上) で CPU クロックが最大値の 85% 未満の状態が "
        "25 秒以上続いた期間を検出する (記録が途切れた前後は続いたと数えない)"
    )
    why = "PSI(需要)では区別できない物理的な熱制限を、温度×クロック比で識別するため"
    manual_commands = [
        "sensors",
        "watch -n1 'grep MHz /proc/cpuinfo'",
    ]

    temp_hot_c: float = _TEMP_HOT_C
    freq_throttle_ratio: float = _FREQ_THROTTLE_RATIO
    min_seconds: float = _MIN_RUN_SECONDS
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

    def _runs(self, rows, joined: list[bool]) -> list[list[int]]:
        """Indexes of each stretch of hot and slow *rows* that follow one another."""
        runs: list[list[int]] = []
        run: list[int] = []
        for i, row in enumerate(rows):
            if not self._hot_and_slow(row):
                run = []
            elif run and joined[i - 1]:
                run.append(i)
            else:
                run = [i]
                runs.append(run)
        return runs

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        rows = store.rows("thermal_samples", since=window.start_ts, until=window.end_ts)
        seconds, joined = _coverage([r["ts"] for r in rows])
        runs = self._runs(rows, joined)
        sustained = [
            run
            for run in runs
            if len(run) >= self.min_points
            and sum(seconds[i] for i in run) >= self.min_seconds
        ]
        throttled = [rows[i] for run in sustained for i in run]
        if not throttled:
            return []

        duration = sum(seconds[i] for run in sustained for i in run)
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
            "min_duration_seconds": self.min_seconds,
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


def _coverage(timestamps: list[float]) -> tuple[list[float], list[bool]]:
    """The seconds each sample stands for, and which steps are not holes.

    A sample stands for the step to the next one, or for the usual spacing
    around it when that step is a hole (see ``_HOLE_FACTOR``) or it is the last
    sample.  ``joined[i]`` is whether sample ``i + 1`` follows sample ``i``
    without a hole.
    """
    steps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    usual = []
    for i, step in enumerate(steps):
        nearby = (
            steps[max(0, i - _NEARBY_STEPS) : i] + steps[i + 1 : i + 1 + _NEARBY_STEPS]
        )
        usual.append(statistics.median(nearby) if nearby else step)
    joined = [step <= _HOLE_FACTOR * u for step, u in zip(steps, usual)]
    seconds = [step if ok else u for step, u, ok in zip(steps, usual, joined)]
    return seconds + [usual[-1] if usual else 0.0], joined
