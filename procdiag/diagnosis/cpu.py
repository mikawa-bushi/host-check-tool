"""CPU diagnostic rules.

Currently implements:

- **CPU-PERIODIC-01**: Detect periodically recurring CPU spikes using
  ``trend.detect_periodicity`` on ``spike_events`` timestamps, in host local
  time.  Fires INFO when a daily or hourly schedule is found, escalating to
  WARNING when it recurred in many days (hours).

- **CPU-SINGLECORE-01**: Detect single-core saturation — episodes where the
  busiest core is at ≥ 95% while the rest of the CPU is quiet (total CPU below
  ``avg_pct_max``, raised on hosts with fewer than 4 CPUs) for ≥ 15 consecutive
  minutes.  A minute counts when that held for half of it, judged per second
  from ``sys_samples`` where they remain and from ``sys_rollup_1m`` elsewhere.
  Cross-references ``proc_rollup_1m`` to name the process that used the most
  CPU during the episode and appends a py-spy suggestion for Python processes.

- **CPU-SATURATION-01**: Detect system-wide CPU pressure — sustained episodes
  where ``psi_cpu_some10`` > 20 for ≥ 15 consecutive minutes, a minute counting
  when that held for half of it.  Collects top offending processes from
  ``spike_events.top_processes`` during the episode and fires WARNING.  PSI
  columns absent (all None) → silent.

Design doc reference: §6.2 CPU-PERIODIC-01 / CPU-SINGLECORE-01 / CPU-SATURATION-01.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..core import (
    Confidence,
    Finding,
    Rule,
    Severity,
    Window,
    register,
)
from ..spikes import culprits
from .episodes import MINUTE, held, sample_interval, sustained_minutes
from .trend import detect_periodicity, find_sustained_episodes

if TYPE_CHECKING:
    from ..storage import Storage

# ---------------------------------------------------------------------------
# Thresholds — exposed as class attributes so tests can inject alternatives
# ---------------------------------------------------------------------------

_CPU_SINGLECORE_CORE_PCT: float = 95.0
_CPU_SINGLECORE_AVG_MAX: float = 50.0
_CPU_SINGLECORE_MIN_MINUTES: int = 15
# ``avg_pct_max`` leaves room for one pinned core plus other work on a host of
# at least this many CPUs; fewer CPUs make the pinned core a bigger share, and
# the limit rises by the difference.
_CPU_SINGLECORE_REFERENCE_CPUS: int = 4
# A rollup minute carries only the busiest second's core and the minute's
# average, so it counts when the average alone shows at least this share of a
# core busy: one busy second adds only 1/60 of a core to it.
_CPU_SINGLECORE_ROLLUP_CORE_SHARE: float = 0.5

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
    """Detect single-core saturation with the rest of the CPU quiet.

    A minute is saturated when the busiest core was at ≥ 95% and the total CPU
    stayed below the ceiling for half of it, judged per second from
    ``sys_samples`` where they remain.  Minutes only ``sys_rollup_1m`` still
    covers are judged from its columns (see ``_rollup_saturated``).  Fires for
    each run of ``min_consecutive_minutes`` such minutes and cross-references
    ``proc_rollup_1m`` to name the top offender and flag Python processes.
    """

    rule_id = "CPU-SINGLECORE-01"
    category = "cpu"
    title = "シングルコア飽和"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0
    requires_rows = ("sys_rollup_1m",)
    what = (
        "最も忙しいコアが 95% 以上で CPU 全体が cpu_avg 未満 (50%。CPU が 4 個より"
        "少ないホストでは 1 コア分が占める割合の違いだけ引き上げる) の秒が"
        "1 分の半分以上を占める分を数え、15分以上連続したらプロセスを突合する。"
        "生データが残っていない時間帯は 1 分集計 (その分で最も忙しかった秒のコア・"
        "全体の平均) で判定し、平均が 1 コアの半分に満たない分は数えない"
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
        raw_rows = store.rows(
            "sys_samples",
            since=window.start_ts,
            until=window.end_ts,
        )

        ncpu = _cpu_count_floor(raw_rows, sys_rows)
        if ncpu < 2:
            return []
        avg_limit = self.avg_pct_max + max(
            0.0, 100.0 / ncpu - 100.0 / _CPU_SINGLECORE_REFERENCE_CPUS
        )

        saturated, minute_stats = self._saturated_minutes(
            raw_rows, sys_rows, ncpu, avg_limit
        )
        saturated_episodes = find_sustained_episodes(
            [
                (minute, float(minute), saturated[minute])
                for minute in sorted(saturated)
            ],
            min_consecutive=self.min_consecutive_minutes,
        )

        if not saturated_episodes:
            return []

        findings: list[Finding] = []

        for ep_start, ep_end in saturated_episodes:
            # Rank processes by the CPU they used during the episode: one that
            # peaked once does not outrank one that was busy throughout.
            proc_rows = store.rows(
                "proc_rollup_1m",
                since=ep_start,
                until=ep_end,
            )
            cpu_by_proc: dict[int, float] = {}
            for row in proc_rows:
                if row["cpu_avg"] is not None:
                    pid = row["proc_id"]
                    cpu_by_proc[pid] = cpu_by_proc.get(pid, 0.0) + row["cpu_avg"]

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

            ep_stats = [
                stats
                for minute, stats in minute_stats.items()
                if ep_start <= minute <= ep_end
            ]
            core_values = [core for core, _ in ep_stats if core is not None]
            avg_values = [avg for _, avg in ep_stats if avg is not None]
            core_max_val = max(core_values, default=self.core_pct_threshold)
            cpu_avg_val = sum(avg_values) / len(avg_values) if avg_values else 0.0
            duration_minutes = round((ep_end - ep_start) / 60.0) + 1

            subject = top_process_names[0] if top_process_names else "不明"

            evidence = {
                "core_max_pct": round(core_max_val, 1),
                "cpu_avg_pct": round(cpu_avg_val, 1),
                "cpu_avg_limit_pct": round(avg_limit, 1),
                "cpu_count_estimate": ncpu,
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

    def _saturated_minutes(
        self, raw_rows, rollup_rows, ncpu: int, avg_limit: float
    ) -> tuple[dict[int, bool], dict[int, tuple[float | None, float | None]]]:
        """Per minute: whether it was saturated, and (busiest core, average CPU).

        A minute that ``sys_samples`` covers for at least half of it is judged
        per second; every other minute ``sys_rollup_1m`` has is judged from the
        rollup row.
        """
        interval = sample_interval([row["ts"] for row in raw_rows])
        by_minute: dict[int, list] = {}
        for row in raw_rows:
            by_minute.setdefault(int(row["ts"] // MINUTE) * int(MINUTE), []).append(row)

        saturated: dict[int, bool] = {}
        stats: dict[int, tuple[float | None, float | None]] = {}
        for minute, rows in by_minute.items():
            if not held(len(rows), interval):
                continue
            pinned = sum(
                row["cpu_max_core"] is not None
                and row["cpu_total"] is not None
                and row["cpu_max_core"] >= self.core_pct_threshold
                and row["cpu_total"] < avg_limit
                for row in rows
            )
            cores = [r["cpu_max_core"] for r in rows if r["cpu_max_core"] is not None]
            totals = [r["cpu_total"] for r in rows if r["cpu_total"] is not None]
            saturated[minute] = held(pinned, interval)
            stats[minute] = (
                max(cores, default=None),
                sum(totals) / len(totals) if totals else None,
            )

        from_raw = set(saturated)
        for row in rollup_rows:
            minute = int(row["ts_min"] // MINUTE) * int(MINUTE)
            if minute in from_raw:
                continue
            core, avg = row["cpu_max_core_max"], row["cpu_avg"]
            judged = self._rollup_saturated(core, avg, ncpu, avg_limit)
            if judged or minute not in saturated:
                saturated[minute] = judged
                stats[minute] = (core, avg)
        return saturated, stats

    def _rollup_saturated(
        self, core: float | None, avg: float | None, ncpu: int, avg_limit: float
    ) -> bool:
        """Judge a minute from ``cpu_max_core_max`` and ``cpu_avg`` alone.

        The rollup keeps the busiest core of the single busiest second and the
        average total CPU of the minute, so ``core >= threshold`` can be one
        second.  A core busy for ``s`` seconds of the minute adds ``s / 60`` of
        a core to the average, so the average must be at least half a core:
        one busy second never reaches it, while half a minute of a pinned core
        does.  Other load adds to the average too, which can let a minute of
        short bursts on a busy host through, but never a quiet one.
        """
        if core is None or avg is None:
            return False
        core_share_floor = _CPU_SINGLECORE_ROLLUP_CORE_SHARE * 100.0 / ncpu
        return core >= self.core_pct_threshold and core_share_floor <= avg < avg_limit


def _cpu_count_floor(raw_rows, rollup_rows) -> int:
    """The CPU count the recording itself proves is at least there.

    The database does not store the CPU count.  One sample's busiest core cannot
    exceed the CPU total times the CPU count, so ``cpu_max_core / cpu_total`` is
    at most the count for every sample (and ``cpu_max_core_max / cpu_max`` for a
    rollup minute).  The largest ratio is the tightest bound; it reaches the
    count whenever one core was busy while the rest were idle, the very
    situation this rule looks for.  1 when no row tells.
    """
    best = 1.0
    for row in raw_rows:
        core, total = row["cpu_max_core"], row["cpu_total"]
        if core is not None and total:
            best = max(best, core / total)
    for row in rollup_rows:
        core, total = row["cpu_max_core_max"], row["cpu_max"]
        if core is not None and total:
            best = max(best, core / total)
    return max(1, math.ceil(best - 1e-6))


# ---------------------------------------------------------------------------
# CPU-SATURATION-01
# ---------------------------------------------------------------------------

_CPU_SATURATION_PSI_THRESHOLD: float = 20.0  # psi_cpu_some10 > this value
_CPU_SATURATION_MIN_MINUTES: int = 15  # consecutive minutes required


@register
class CpuSaturationRule(Rule):
    """Detect sustained system-wide CPU pressure via PSI.

    Scans ``sys_samples`` for consecutive minutes where ``psi_cpu_some10``
    exceeded the threshold (default 20%) for at least half of the minute, then
    cross-references ``spike_events`` during those episodes to collect top
    offending processes.  Fires WARNING when a sustained episode (≥ 15 min) is
    found.

    PSI columns absent from the DB (all None) → silent (no false positives on
    kernels that do not expose PSI).
    """

    rule_id = "CPU-SATURATION-01"
    category = "cpu"
    title = "CPU 飽和 (PSI)"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0
    requires_rows = ("sys_samples",)
    what = (
        "sys_samples の psi_cpu_some10 が閾値を超えていた秒が 1 分の半分以上を占める分を数え、"
        "15分以上連続したら spike_events の top_processes から犯人プロセスを集計して列挙する"
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

        saturated_episodes = sustained_minutes(
            [
                (
                    row["ts"],
                    row["psi_cpu_some10"] is not None
                    and row["psi_cpu_some10"] > self.psi_threshold,
                )
                for row in raw_rows
            ],
            min_minutes=self.min_consecutive_minutes,
        )

        if not saturated_episodes:
            return []

        findings: list[Finding] = []

        for ep_start, ep_end in saturated_episodes:
            # Collect top processes from spike_events during the episode.
            spike_rows = store.rows(
                "spike_events",
                since=ep_start,
                until=ep_end + MINUTE,
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
            ep_rows = [r for r in raw_rows if ep_start <= r["ts"] < ep_end + MINUTE]
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
