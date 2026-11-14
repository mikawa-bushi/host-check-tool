"""CPU diagnostic rules.

Currently implements:

- **CPU-PERIODIC-01**: Detect periodically recurring CPU spikes using
  ``trend.detect_periodicity`` on ``spike_events`` timestamps, in host local
  time.  Fires INFO when a daily or hourly schedule is found, escalating to
  WARNING when it recurred in many days (hours).

- **CPU-SINGLECORE-01**: Detect single-core saturation — episodes where
  ``cpu_max_core_max`` ≥ 95% while ``cpu_avg`` < 50% for ≥ 15 consecutive
  minutes.  Cross-references ``proc_rollup_1m`` to name the top offending
  process and appends a py-spy suggestion for Python processes.

- **CPU-SATURATION-01**: Detect system-wide CPU pressure — sustained episodes
  where ``psi_cpu_some10`` > 20 for ≥ 15 consecutive minutes.  Collects top
  offending processes from ``spike_events.top_processes`` during the episode
  and fires WARNING.  PSI columns absent (all None) → silent.

Design doc reference: §6.2 CPU-PERIODIC-01 / CPU-SINGLECORE-01 / CPU-SATURATION-01.
"""

from __future__ import annotations

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
from ..spikes import culprits
from .trend import detect_periodicity, find_sustained_episodes

if TYPE_CHECKING:
    from ..storage import Storage

# ---------------------------------------------------------------------------
# Thresholds — exposed as class attributes so tests can inject alternatives
# ---------------------------------------------------------------------------

_CPU_SINGLECORE_CORE_PCT: float = 95.0
_CPU_SINGLECORE_AVG_MAX: float = 50.0
_CPU_SINGLECORE_MIN_MINUTES: int = 15

_CPU_PERIODIC_WARNING_COUNT: int = 5


# ---------------------------------------------------------------------------
# CPU-PERIODIC-01
# ---------------------------------------------------------------------------


@register
class CpuPeriodicRule(Rule):
    """Detect periodic CPU spike patterns in spike_events.

    Collects timestamps of cpu/storm trigger events and calls
    ``detect_periodicity`` to find daily or hourly recurrence.
    """

    rule_id = "CPU-PERIODIC-01"
    category = "cpu"
    title = "周期的 CPU スパイク"
    default_severity = Severity.INFO
    min_observation_hours = 0.0  # periodicity detection needs calendar span
    requires_schema = ("spike_events",)
    what = (
        "spike_events を負荷のまとまりごとに始まりの時刻 (ホストのローカル時刻) にまとめ、"
        "記録した日 (時間) の半分以上で同じ時刻 (既定 ±2分) に始まり、"
        "でたらめな時刻なら 1% 未満でしか起きない揃い方のときに日次・時次の周期とする。"
        "偶然の確率は、その時刻の前後 1 時間と全体のうち負荷の始まりがいちばん多い頻度で見積もる"
    )
    why = (
        "定時タスク(cron/systemd timer)の競合は周期的なスパイクとして現れる。"
        "周期が判明すれば cron 設定との突合でタスクを特定できる"
    )
    manual_commands = [
        "crontab -l",
        "systemctl list-timers --all",
    ]

    tol_sec: float = 120.0
    min_events: int = 3
    warning_count: int = _CPU_PERIODIC_WARNING_COUNT

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run CPU-PERIODIC-01 against the observation window."""
        # Collect spike event timestamps for cpu and storm triggers.
        rows = store.rows(
            "spike_events",
            since=window.start_ts,
            until=window.end_ts,
            where="trigger_type IN ('cpu', 'storm')",
        )
        if not rows:
            return []

        timestamps = [float(row["ts"]) for row in rows]
        trigger_type = rows[0]["trigger_type"] if rows else "cpu"
        recorded = [
            row["ts_min"]
            for row in store.rows(
                "sys_rollup_1m", since=window.start_ts, until=window.end_ts
            )
        ] or [
            window.start_ts + 60.0 * i for i in range(int(window.span_hours * 60) + 1)
        ]

        period = detect_periodicity(
            timestamps,
            tol_sec=self.tol_sec,
            min_events=self.min_events,
            recorded_minutes=recorded,
        )
        if period is None:
            return []

        severity = (
            Severity.WARNING if period.count >= self.warning_count else Severity.INFO
        )

        evidence = {
            "period_kind": period.kind,
            "anchor": period.anchor,
            "count": period.count,
            "cycles": period.cycles,
            "chance": period.chance,
            "event_count": len(timestamps),
            "tol_minutes": self.tol_sec / 60.0,
            "trigger_type": trigger_type,
        }

        kind_label = "日次" if period.kind == "daily" else "時次"
        summary = f"{kind_label}周期スパイク ({period.anchor} 頃、{period.count}回観測)"

        confidence = Confidence.HIGH if period.count >= 7 else Confidence.MEDIUM

        return [
            Finding(
                rule_id=self.rule_id,
                severity=severity,
                subject=f"cpu_spike/{period.kind}",
                summary=summary,
                evidence=evidence,
                detected_at=(window.start_ts, window.end_ts),
                confidence=confidence,
            )
        ]


# ---------------------------------------------------------------------------
# CPU-SINGLECORE-01
# ---------------------------------------------------------------------------


@register
class CpuSinglecoreRule(Rule):
    """Detect single-core saturation with low overall CPU utilisation.

    Scans ``sys_rollup_1m`` for consecutive minutes where
    ``cpu_max_core_max`` ≥ 95% and ``cpu_avg`` < 50%, then cross-references
    ``proc_rollup_1m`` to name the top offender and flags Python processes.
    """

    rule_id = "CPU-SINGLECORE-01"
    category = "cpu"
    title = "シングルコア飽和"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0
    requires_rows = ("sys_rollup_1m",)
    what = (
        "sys_rollup_1m で cpu_max_core_max ≥ 95% かつ cpu_avg < 50% の"
        "連続分数を数え、15分以上継続したらプロセスを突合する"
    )
    why = (
        "シングルコア飽和はマルチコアシステムで見落とされやすい。"
        "CPU全体が低くてもシングルコアが詰まると特定スレッドの応答が悪化する"
    )
    manual_commands = [
        "top -H",
        "ps -eLf | sort -k3 -rn | head -20",
    ]

    # Allow tests to override thresholds.
    core_pct_threshold: float = _CPU_SINGLECORE_CORE_PCT
    avg_pct_max: float = _CPU_SINGLECORE_AVG_MAX
    min_consecutive_minutes: int = _CPU_SINGLECORE_MIN_MINUTES

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run CPU-SINGLECORE-01 against the observation window."""
        sys_rows = store.rows(
            "sys_rollup_1m",
            since=window.start_ts,
            until=window.end_ts,
        )
        if not sys_rows:
            return []

        # Aggregate per minute bucket (ts_min rounded to the nearest minute).
        # This handles the case where multiple rows share nearly identical timestamps.
        # For each bucket, a minute is "saturated" if ANY row in it shows
        # cpu_max_core_max >= threshold AND cpu_avg < ceiling.
        minute_data: dict[int, dict] = {}
        for row in sys_rows:
            core_max = row["cpu_max_core_max"]
            cpu_avg = row["cpu_avg"]
            ts = row["ts_min"]
            bucket = int(ts / 60) * 60  # floor to minute boundary
            if bucket not in minute_data:
                minute_data[bucket] = {
                    "core_max": core_max,
                    "cpu_avg": cpu_avg,
                    "ts": ts,
                }
            else:
                # Keep the worst-case (highest core, lowest avg) for the bucket.
                if core_max is not None:
                    prev = minute_data[bucket]["core_max"]
                    if prev is None or core_max > prev:
                        minute_data[bucket]["core_max"] = core_max
                if cpu_avg is not None:
                    prev = minute_data[bucket]["cpu_avg"]
                    if prev is None or cpu_avg < prev:
                        minute_data[bucket]["cpu_avg"] = cpu_avg

        # A minute is "saturated" when its worst-case core is pegged while the
        # overall average stays low.  find_sustained_episodes handles the
        # consecutive-run detection (90s jitter tolerance, min-length, trailing).
        samples = [
            (
                bucket,
                minute_data[bucket]["ts"],
                (
                    minute_data[bucket]["core_max"] is not None
                    and minute_data[bucket]["cpu_avg"] is not None
                    and minute_data[bucket]["core_max"] >= self.core_pct_threshold
                    and minute_data[bucket]["cpu_avg"] < self.avg_pct_max
                ),
            )
            for bucket in sorted(minute_data)
        ]
        saturated_episodes = find_sustained_episodes(
            samples, min_consecutive=self.min_consecutive_minutes
        )

        if not saturated_episodes:
            return []

        findings: list[Finding] = []

        for ep_start, ep_end in saturated_episodes:
            # Find top processes by cpu_max during this episode.
            proc_rows = store.rows(
                "proc_rollup_1m",
                since=ep_start,
                until=ep_end,
            )

            # Aggregate cpu_max per proc_id.
            cpu_by_proc: dict[int, float] = {}
            for row in proc_rows:
                pid = row["proc_id"]
                cpu = row["cpu_max"]
                if cpu is not None and (
                    pid not in cpu_by_proc or cpu > cpu_by_proc[pid]
                ):
                    cpu_by_proc[pid] = cpu

            top_procs_ids = sorted(
                cpu_by_proc, key=lambda p: cpu_by_proc[p], reverse=True
            )[:5]

            top_process_names: list[str] = []
            has_python = False
            for proc_id in top_procs_ids:
                name = store.proc_name(proc_id)
                top_process_names.append(name)
                if "python" in name.lower():
                    has_python = True

            # Get representative values from sys_rollup for this episode.
            ep_sys_rows = [r for r in sys_rows if ep_start <= r["ts_min"] <= ep_end]
            core_max_val = (
                max(
                    r["cpu_max_core_max"]
                    for r in ep_sys_rows
                    if r["cpu_max_core_max"] is not None
                )
                if ep_sys_rows
                else self.core_pct_threshold
            )
            cpu_avg_val = (
                sum(r["cpu_avg"] for r in ep_sys_rows if r["cpu_avg"] is not None)
                / len([r for r in ep_sys_rows if r["cpu_avg"] is not None])
                if ep_sys_rows
                else 0.0
            )
            duration_minutes = round((ep_end - ep_start) / 60.0) + 1

            subject = top_process_names[0] if top_process_names else "不明"

            evidence = {
                "core_max_pct": round(core_max_val, 1),
                "cpu_avg_pct": round(cpu_avg_val, 1),
                "duration_minutes": duration_minutes,
                "top_processes": top_process_names,
                "has_python_process": has_python,
                "episode_start": ep_start,
                "episode_end": ep_end,
            }

            summary = (
                f"シングルコア飽和: cpu_max_core={core_max_val:.0f}%"
                f", cpu_avg={cpu_avg_val:.1f}%"
                f", {duration_minutes}分継続"
                + (f", 上位: {subject}" if subject != "不明" else "")
            )

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.WARNING,
                    subject=subject,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(ep_start, ep_end),
                    confidence=(
                        Confidence.HIGH if duration_minutes >= 30 else Confidence.MEDIUM
                    ),
                )
            )

        return findings


# ---------------------------------------------------------------------------
# CPU-SATURATION-01
# ---------------------------------------------------------------------------

_CPU_SATURATION_PSI_THRESHOLD: float = 20.0  # psi_cpu_some10 > this value
_CPU_SATURATION_MIN_MINUTES: int = 15  # consecutive minutes required


@register
class CpuSaturationRule(Rule):
    """Detect sustained system-wide CPU pressure via PSI.

    Scans ``sys_samples`` for consecutive minutes where ``psi_cpu_some10``
    exceeds the threshold (default 20%), then cross-references ``spike_events``
    during those episodes to collect top offending processes.  Fires WARNING
    when a sustained episode (≥ 15 min) is found.

    PSI columns absent from the DB (all None) → silent (no false positives on
    kernels that do not expose PSI).
    """

    rule_id = "CPU-SATURATION-01"
    category = "cpu"
    title = "CPU 飽和 (PSI)"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0
    requires_rows = ("sys_samples", "sys_samples.psi_cpu_some10")
    requires_observed = (Capability.PSI,)
    what = (
        "sys_samples の psi_cpu_some10 が閾値超の連続分数を数え、15分以上継続したら"
        "spike_events の top_processes から犯人プロセスを集計して列挙する"
    )
    why = (
        "PSI の some10 は少なくとも1タスクが CPU 待ちであった割合。"
        "20% 超が15分継続すると体感遅延が確実に発生している"
    )
    manual_commands = [
        "cat /proc/pressure/cpu",
        "top -b -n 1 | head -20",
        "ps -eo pid,ppid,cmd,%cpu --sort=-%cpu | head -20",
        "sar -u 1 10",
    ]

    psi_threshold: float = _CPU_SATURATION_PSI_THRESHOLD
    min_consecutive_minutes: int = _CPU_SATURATION_MIN_MINUTES

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run CPU-SATURATION-01 against the observation window."""
        raw_rows = store.rows(
            "sys_samples",
            since=window.start_ts,
            until=window.end_ts,
        )
        if not raw_rows:
            return []

        # Detect PSI absence: if all psi_cpu_some10 values are None, stay silent.
        all_psi_none = all(r["psi_cpu_some10"] is None for r in raw_rows)
        if all_psi_none:
            return []

        # Bucket samples into per-minute slots.  A minute is "saturated" when
        # ANY sample in the bucket has psi_cpu_some10 > threshold.
        minute_data: dict[int, dict] = {}
        for row in raw_rows:
            ts = row["ts"]
            psi = row["psi_cpu_some10"]
            bucket = int(ts / 60) * 60
            if bucket not in minute_data:
                minute_data[bucket] = {"max_psi": psi, "ts": ts}
            else:
                prev = minute_data[bucket]["max_psi"]
                if psi is not None and (prev is None or psi > prev):
                    minute_data[bucket]["max_psi"] = psi

        # A minute is "saturated" when its peak psi_cpu_some10 exceeds the
        # threshold; find_sustained_episodes handles consecutive-run detection.
        samples = [
            (
                bucket,
                minute_data[bucket]["ts"],
                (
                    minute_data[bucket]["max_psi"] is not None
                    and minute_data[bucket]["max_psi"] > self.psi_threshold
                ),
            )
            for bucket in sorted(minute_data)
        ]
        saturated_episodes = find_sustained_episodes(
            samples, min_consecutive=self.min_consecutive_minutes
        )

        if not saturated_episodes:
            return []

        findings: list[Finding] = []

        for ep_start, ep_end in saturated_episodes:
            # Collect top processes from spike_events during the episode.
            spike_rows = store.rows(
                "spike_events",
                since=ep_start,
                until=ep_end,
            )

            # Tally how often each process was a spike culprit in the episode.
            proc_counts: dict[str, int] = {}
            for row in spike_rows:
                for culprit in culprits(row["top_processes"], store):
                    name = culprit["name"]
                    proc_counts[name] = proc_counts.get(name, 0) + 1

            top_procs = sorted(proc_counts, key=lambda k: proc_counts[k], reverse=True)[
                :5
            ]

            # Duration and peak PSI for this episode.
            ep_rows = [r for r in raw_rows if ep_start <= r["ts"] <= ep_end]
            peak_psi = max(
                (
                    r["psi_cpu_some10"]
                    for r in ep_rows
                    if r["psi_cpu_some10"] is not None
                ),
                default=self.psi_threshold,
            )
            duration_minutes = max(1, round((ep_end - ep_start) / 60.0) + 1)

            subject = top_procs[0] if top_procs else "不明"

            evidence = {
                "psi_threshold": self.psi_threshold,
                "peak_psi_cpu_some10": round(peak_psi, 2),
                "duration_minutes": duration_minutes,
                "top_processes": top_procs,
                "episode_start": ep_start,
                "episode_end": ep_end,
                "spike_event_count": len(spike_rows),
            }

            summary = (
                f"CPU 飽和: psi_cpu_some10 最大 {peak_psi:.1f}%"
                f"、{duration_minutes}分継続"
                + (f"、上位: {subject}" if subject != "不明" else "")
            )

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.WARNING,
                    subject=subject,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(ep_start, ep_end),
                    confidence=(
                        Confidence.HIGH if duration_minutes >= 30 else Confidence.MEDIUM
                    ),
                )
            )

        return findings
