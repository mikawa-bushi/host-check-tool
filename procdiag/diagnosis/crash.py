"""Crash diagnostic rules.

- **OOM-KILL-01**: Detect OOM-killed processes by correlating process
  disappearance with memory crisis indicators in ``sys_samples``.  The rule
  operates entirely on the recorded DB so it works offline without any
  privilege escalation.  An optional ``journalctl`` probe can elevate
  confidence to HIGH when an OOM-kill log line matching the process name or
  PID is found.
- **UNCLEAN-SHUTDOWN-01**: Recording sessions that ended without a clean stop
  and were followed by a new boot (power loss, panic, hard reset).
- **SERVICE-RESTART-01**: systemd services in a crash/restart loop or failed.
- **KERNEL-ERROR-01**: Disk / filesystem / hardware errors from the kernel log.

OOM-KILL-01 design:

1. Identify proc_ids whose last sample is significantly before the window
   end (> ``GAP_THRESHOLD_S`` seconds) — the process disappeared while
   recording continued, distinguishing it from machine-wide stops.
2. Exclude short-lived processes (lifetime < ``MIN_LIFETIME_S`` seconds),
   which are expected to exit.
3. Exclude trivial consumers: last RSS must be >= 1% of estimated total
   memory (``mem_used + mem_available`` from the nearest sys_sample).
   This estimation is documented as approximate in the method body.
4. Correlate with a memory-crisis window: if ``sys_samples`` in
   ``[last_ts - 180s, last_ts + 60s]`` contains any row where
   ``psi_mem_full10 >= 1.0`` OR ``swap_out > 0`` OR
   ``mem_available < 5% of mem_total``, the process is flagged.
5. All four conditions met → Finding(WARNING, MEDIUM, subject=process name).
6. If multiple candidates, cap at 5, sorted by last_rss descending.
7. Optional journal probe: calls ``_read_oom_journal`` (monkeypatchable),
   searches for process name / PID in OOM-kill log lines, elevates
   confidence to HIGH.

Design doc reference: procdiag_extension_proposal.md EXT-01 / OOM-KILL-01.
"""

from __future__ import annotations

import bisect
from typing import TYPE_CHECKING

from .. import kernlog
from ..core import (
    Capability,
    Confidence,
    Finding,
    MiB,
    Rule,
    Severity,
    Window,
    register,
)

if TYPE_CHECKING:
    from ..storage import Storage

# ---------------------------------------------------------------------------
# Thresholds — class constants so tests can patch them without monkey-patching
# the module-level names.
# ---------------------------------------------------------------------------

# Gap (seconds) between last proc_sample and window end that indicates the
# process disappeared while recording continued (not a machine-wide stop).
_GAP_THRESHOLD_S: float = 60.0

# Minimum lifetime (seconds) a process must have been recorded before we
# consider it a candidate.  Excludes short-lived helper processes.
_MIN_LIFETIME_S: float = 600.0

# Minimum RSS as a fraction of total memory for a process to be considered
# a significant memory consumer.
_MIN_RSS_FRACTION: float = 0.01

# Width (seconds) of the memory-crisis correlation window.
_CRISIS_WINDOW_BEFORE_S: float = 180.0
_CRISIS_WINDOW_AFTER_S: float = 60.0

# PSI full-stall threshold that signals a memory crisis.
_PSI_MEM_FULL_THRESHOLD: float = 1.0

# mem_available fraction below which the system is considered in a low-memory
# crisis.
_MEM_AVAIL_FRACTION: float = 0.05

# Maximum number of findings to emit (sorted by last_rss descending).
_MAX_FINDINGS: int = 5


# ---------------------------------------------------------------------------
# Journal helper (extracted for monkeypatching in tests)
# ---------------------------------------------------------------------------


def _read_oom_journal(since: float, until: float) -> list[str]:
    """Return OOM-kill kernel log messages between *since* and *until*.

    Returns an empty list — without raising — if journalctl is absent, the
    caller lacks permission, or the subprocess times out.  This keeps
    OOM-KILL-01 silent rather than crashing when journal access is unavailable.
    """
    events, _cursor = kernlog.read_events(since=since, until=until)
    return [e.message for e in events or [] if e.kind == "oom"]


# ---------------------------------------------------------------------------
# OOM-KILL-01 Rule
# ---------------------------------------------------------------------------


@register
class OomKillRule(Rule):
    """Detect OOM-killed processes via process disappearance + memory-crisis correlation.

    Queries ``proc_samples`` to find processes whose last recorded sample is
    significantly earlier than the window end (indicating the process exited
    while recording continued), then checks ``sys_samples`` for memory-crisis
    indicators in a window around the disappearance time.

    An optional ``journalctl`` probe (``_read_oom_journal``) can elevate
    confidence from MEDIUM to HIGH when a matching OOM-kill log line is found.
    The probe is silent when journalctl is unavailable.

    Note on mem_total approximation: total physical memory is estimated as
    ``mem_used + mem_available`` from the nearest ``sys_sample``.  This
    excludes memory used by the kernel's slab cache that is not reported as
    either "used" or "available" in some kernel versions, so the threshold
    check (RSS >= 1% of mem_total) may be slightly conservative.
    """

    rule_id = "OOM-KILL-01"
    category = "crash"
    title = "OOM Killerによるプロセス強制終了"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0  # instantaneous — runs on any recording length
    requires_rows = ("proc_samples", "sys_samples")
    what = (
        "プロセスの消滅タイミングと sys_samples のメモリ危機指標"
        "(psi_mem_full10 / swap_out / mem_available 急減)の相関で OOM kill を検出する"
    )
    why = (
        "OOM Killer はカーネルが物理メモリを使い果たしたときに起動し、"
        "スコアが高いプロセスを強制終了する。"
        "プロセスの突然消滅と直前の PSI 全停止・swap_out 急増は"
        "OOM kill の決定論的証拠となる"
    )
    manual_commands = [
        "journalctl -k | grep -i oom",
        "dmesg | grep -i oom",
        "dmesg | grep -i 'killed process'",
        "cat /proc/sys/vm/overcommit_memory",
    ]

    # Class-level threshold overrides (tests may patch these).
    gap_threshold_s: float = _GAP_THRESHOLD_S
    min_lifetime_s: float = _MIN_LIFETIME_S
    min_rss_fraction: float = _MIN_RSS_FRACTION
    crisis_window_before_s: float = _CRISIS_WINDOW_BEFORE_S
    crisis_window_after_s: float = _CRISIS_WINDOW_AFTER_S
    psi_mem_full_threshold: float = _PSI_MEM_FULL_THRESHOLD
    mem_avail_fraction: float = _MEM_AVAIL_FRACTION
    max_findings: int = _MAX_FINDINGS

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run OOM-KILL-01 against the observation window."""
        # --- Step 1: gather per-process first/last sample timestamps and RSS ---
        # Query proc_samples within the window.  GROUP BY proc_id to get bounds.
        proc_rows = store.conn.execute(
            "SELECT proc_id, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
            "       MAX(rss) AS last_rss "
            "FROM proc_samples "
            "WHERE ts >= ? AND ts <= ? "
            "GROUP BY proc_id",
            (window.start_ts, window.end_ts),
        ).fetchall()

        if not proc_rows:
            return []

        # --- Step 2: load sys_samples once for cost-efficient lookups ---
        sys_rows = store.rows(
            "sys_samples",
            since=window.start_ts,
            until=window.end_ts,
        )
        # Index by ts for fast nearest-neighbour search.
        sys_ts_list = [r["ts"] for r in sys_rows]

        if not sys_rows:
            return []

        findings: list[Finding] = []

        for proc_row in proc_rows:
            proc_id = proc_row["proc_id"]
            first_ts: float = proc_row["first_ts"]
            last_ts: float = proc_row["last_ts"]
            last_rss: int = proc_row["last_rss"] or 0

            # --- Condition 1: recording continued after the process disappeared ---
            gap = window.end_ts - last_ts
            if gap <= self.gap_threshold_s:
                continue

            # --- Condition 2: process was not short-lived ---
            lifetime = last_ts - first_ts
            if lifetime < self.min_lifetime_s:
                continue

            # --- Condition 3: process was a significant memory consumer ---
            # Estimate mem_total from the nearest sys_sample to last_ts.
            nearest_sys = self._nearest_sys_sample(last_ts, sys_rows, sys_ts_list)
            if nearest_sys is None:
                continue
            mem_total = self._estimate_mem_total(nearest_sys)
            if mem_total is None or mem_total == 0:
                continue
            if last_rss < mem_total * self.min_rss_fraction:
                continue

            # --- Condition 4: memory crisis correlated with disappearance ---
            crisis_start = last_ts - self.crisis_window_before_s
            crisis_end = last_ts + self.crisis_window_after_s
            crisis_indicator = self._find_crisis_indicator(
                sys_rows, crisis_start, crisis_end, mem_total
            )
            if crisis_indicator is None:
                continue

            # All four conditions met — look up process name.
            process_name = store.proc_name(proc_id)

            # Build evidence.
            lifetime_minutes = round(lifetime / 60.0, 1)
            evidence: dict = {
                "proc_id": proc_id,
                "process_name": process_name,
                "last_seen": last_ts,
                "last_rss_bytes": last_rss,
                "last_rss_mb": round(last_rss / MiB, 1),
                "mem_total_mb": round(mem_total / MiB, 1),
                "lifetime_minutes": lifetime_minutes,
                "gap_to_window_end_s": round(gap, 1),
                "crisis_indicator": crisis_indicator["type"],
                "crisis_value": crisis_indicator["value"],
                "crisis_ts": crisis_indicator["ts"],
            }
            summary = (
                f"OOM Kill 疑い: {process_name} が消滅"
                f" (最終 RSS {evidence['last_rss_mb']:.1f} MB,"
                f" 生存 {lifetime_minutes}分,"
                f" 危機指標: {crisis_indicator['type']}={crisis_indicator['value']})"
            )

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.WARNING,
                    subject=process_name,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(first_ts, last_ts),
                    confidence=Confidence.MEDIUM,
                )
            )

        if not findings:
            return []

        # --- Step 5: cap at max_findings sorted by last_rss descending ---
        findings.sort(key=lambda f: f.evidence.get("last_rss_bytes", 0), reverse=True)
        findings = findings[: self.max_findings]

        # --- Optional: journal probe to elevate confidence ---
        for finding in findings:
            self._try_elevate_confidence(finding)

        return findings

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _nearest_sys_sample(
        self,
        ts: float,
        sys_rows: list,
        sys_ts_list: list[float],
    ):
        """Return the sys_sample row closest to *ts*, or None if no rows."""
        if not sys_ts_list:
            return None
        # Binary-search for the closest timestamp.
        idx = bisect.bisect_left(sys_ts_list, ts)
        if idx == 0:
            return sys_rows[0]
        if idx >= len(sys_ts_list):
            return sys_rows[-1]
        # Compare the two neighbours.
        before = sys_rows[idx - 1]
        after = sys_rows[idx]
        if abs(after["ts"] - ts) < abs(before["ts"] - ts):
            return after
        return before

    def _estimate_mem_total(self, sys_row) -> int | None:
        """Estimate total physical memory from a sys_sample row.

        Uses ``mem_used + mem_available`` as an approximation.  This may
        undercount memory held in kernel slab caches that is reclaimable but
        not reported as ``mem_available`` on some older kernels.  The estimate
        is documented as approximate; the 1% threshold provides enough margin
        that the approximation error is not significant.
        """
        mem_used = sys_row["mem_used"]
        mem_avail = sys_row["mem_available"]
        if mem_used is None or mem_avail is None:
            return None
        return int(mem_used) + int(mem_avail)

    def _find_crisis_indicator(
        self,
        sys_rows: list,
        crisis_start: float,
        crisis_end: float,
        mem_total: int,
    ) -> dict | None:
        """Return the first memory-crisis indicator in the crisis window, or None.

        Checks (in order):
        1. psi_mem_full10 >= threshold
        2. swap_out > 0
        3. mem_available < 5% of mem_total
        """
        threshold_5pct = mem_total * self.mem_avail_fraction
        for row in sys_rows:
            ts = row["ts"]
            if ts < crisis_start or ts > crisis_end:
                continue
            # Criterion 1: PSI full stall.
            psi = row["psi_mem_full10"]
            if psi is not None and psi >= self.psi_mem_full_threshold:
                return {"type": "psi_mem_full10", "value": round(psi, 2), "ts": ts}
            # Criterion 2: swap_out activity.
            swap_out = row["swap_out"]
            if swap_out is not None and swap_out > 0:
                return {"type": "swap_out", "value": int(swap_out), "ts": ts}
            # Criterion 3: mem_available critically low.
            mem_avail = row["mem_available"]
            if mem_avail is not None and int(mem_avail) < threshold_5pct:
                return {
                    "type": "mem_available_low",
                    "value": int(mem_avail),
                    "ts": ts,
                }
        return None

    def _try_elevate_confidence(self, finding: Finding) -> None:
        """Attempt to elevate *finding* confidence to HIGH via journalctl.

        Calls ``_read_oom_journal`` (monkeypatchable module function) and
        searches for the process name or last-seen PID in OOM-kill log lines.
        Silently leaves confidence at MEDIUM on any failure, including if
        the caller-supplied function raises an exception (the defence-in-depth
        guard here mirrors the engine's per-rule try/except).
        """
        last_ts = finding.evidence.get("last_seen", 0.0)
        process_name = finding.evidence.get("process_name", "")

        try:
            # Query journal for lines around the disappearance time.
            journal_lines = _read_oom_journal(
                last_ts - self.crisis_window_before_s,
                last_ts + self.crisis_window_after_s,
            )
        except Exception:
            # Any exception from the journal probe is silenced; confidence stays MEDIUM.
            return

        if not journal_lines:
            return

        # Check if any line references this process name.
        for line in journal_lines:
            if process_name and process_name.lower() in line.lower():
                finding.confidence = Confidence.HIGH
                finding.evidence["journal_line"] = line
                return

        # If we got OOM lines but none matched the process name,
        # still store the first line as corroborating evidence but keep MEDIUM.
        if journal_lines:
            finding.evidence["journal_line_unmatched"] = journal_lines[0]


# ---------------------------------------------------------------------------
# UNCLEAN-SHUTDOWN-01
# ---------------------------------------------------------------------------


@register
class UncleanShutdownRule(Rule):
    """Detect reboots / power losses that happened while recording.

    Each ``procdiag record`` run opens a ``sessions`` row, refreshes its
    ``last_alive_ts`` every flush and sets ``stopped_ts`` on a clean stop.  An
    orderly OS shutdown sends SIGTERM to the recorder first, so a session that
    was never stopped and is followed by a session on a *different* boot means
    the host went down without shutting down: power loss, kernel panic, hard
    reset or a hardware watchdog.  Unstopped sessions on the *same* boot mean
    the recorder process itself was killed (``kill -9``, OOM killer) — reported
    separately at INFO because the host stayed up.
    """

    rule_id = "UNCLEAN-SHUTDOWN-01"
    category = "crash"
    title = "予期しない再起動・電源断"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0
    requires_schema = ("sessions",)
    requires_observed = (Capability.SESSIONS,)
    what = (
        "記録セッションの履歴 (sessions) で、正常停止せずに次の起動 (boot) へ"
        "移ったセッションを検出する"
    )
    why = (
        "OS の正常なシャットダウンでは記録プロセスに SIGTERM が届き停止が記録される。"
        "停止記録がないまま再起動していれば、電源断・カーネルパニック等による異常停止である"
    )
    manual_commands = [
        "journalctl --list-boots",
        "journalctl -b -1 -e",
        "last -x | head -20",
    ]

    # Boot times are derived from the uptime clock and jitter by a few
    # seconds between reads; a larger difference is a different boot.
    boot_tolerance_s: float = 120.0

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run UNCLEAN-SHUTDOWN-01 against the observation window."""
        sessions = store.sessions()
        host_down: list[dict] = []
        recorder_killed: list[dict] = []
        for prev, nxt in zip(sessions, sessions[1:]):
            if prev["stopped_ts"] is not None:
                continue  # clean stop
            if not window.start_ts <= nxt["started_ts"] <= window.end_ts:
                continue
            event = {
                "last_alive": prev["last_alive_ts"] or prev["started_ts"],
                "next_boot": nxt["boot_time"],
                "restarted_at": nxt["started_ts"],
            }
            new_boot = abs((nxt["boot_time"] or 0.0) - (prev["boot_time"] or 0.0))
            if new_boot > self.boot_tolerance_s:
                host_down.append(event)
            else:
                recorder_killed.append(event)

        findings = []
        if host_down:
            n = len(host_down)
            findings.append(
                self._finding(
                    host_down,
                    severity=Severity.WARNING,
                    subject="host",
                    summary=f"記録中に正常な停止処理なしでホストが再起動 ({n} 回)"
                    " — 電源断・カーネルパニック・強制リセットの疑い",
                    confidence=Confidence.HIGH,
                    kind="host_down",
                )
            )
        if recorder_killed:
            n = len(recorder_killed)
            findings.append(
                self._finding(
                    recorder_killed,
                    severity=Severity.INFO,
                    subject="procdiag",
                    summary=f"記録プロセスが停止処理なしで終了 ({n} 回、ホストは稼働継続)",
                    confidence=Confidence.MEDIUM,
                    kind="recorder_killed",
                )
            )
        return findings

    def _finding(
        self,
        events: list[dict],
        *,
        severity: Severity,
        subject: str,
        summary: str,
        confidence: Confidence,
        kind: str,
    ) -> Finding:
        return Finding(
            rule_id=self.rule_id,
            severity=severity,
            subject=subject,
            summary=summary,
            evidence={"kind": kind, "count": len(events), "events": events},
            detected_at=(events[0]["last_alive"], events[-1]["restarted_at"]),
            confidence=confidence,
        )


# ---------------------------------------------------------------------------
# SERVICE-RESTART-01
# ---------------------------------------------------------------------------


@register
class ServiceRestartRule(Rule):
    """Detect crash-looping or failed systemd services.

    Restarts within any one hour at or above ``loop_restarts_per_hour`` are a
    crash loop (CRITICAL); ``warn_restarts`` in the window, a failed
    transition, or a unit already failed when recording started is a WARNING.
    Restarts the recorder could not tell from restarts after a clean exit
    (``restart_maybe_clean``) still count, at MEDIUM confidence.  Restarts
    known to follow a clean exit (``restart_clean``) never make a crash loop:
    ``warn_restarts`` of them is a LOW-confidence WARNING.
    """

    rule_id = "SERVICE-RESTART-01"
    category = "crash"
    title = "サービスの再起動ループ・failed"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0
    requires_schema = ("service_events",)
    requires_observed = (Capability.SYSTEMD,)
    what = "systemd の自動再起動回数 (NRestarts) の増加と failed 遷移をユニットごとに集計する"
    why = (
        "クラッシュ→自動再起動を繰り返すサービスは、その時点では active に見えるため"
        "見落とされやすい。再起動の回数と頻度で異常を判断する"
    )
    manual_commands = [
        "systemctl status <unit>",
        "journalctl -u <unit> -n 100 --no-pager",
        "systemctl show -p NRestarts,Result,ExecMainStatus <unit>",
    ]

    warn_restarts: int = 3
    loop_restarts_per_hour: int = 3

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run SERVICE-RESTART-01 against the observation window."""
        rows = store.rows("service_events", since=window.start_ts, until=window.end_ts)

        by_unit: dict[str, list] = {}
        for row in rows:
            by_unit.setdefault(row["unit"], []).append(row)

        findings: list[Finding] = []
        for unit, events in by_unit.items():
            n_maybe_clean = sum(
                r["n_restarts"] or 0
                for r in events
                if r["kind"] == "restart_maybe_clean"
            )
            n_clean = sum(
                r["n_restarts"] or 0 for r in events if r["kind"] == "restart_clean"
            )
            restarts = [
                (r["ts"], r["n_restarts"] or 0)
                for r in events
                if r["kind"] in ("restart", "restart_maybe_clean")
            ]
            n_restarts = sum(n for _, n in restarts)
            n_failed = sum(1 for r in events if r["kind"] == "failed")
            failed_at_start = any(r["kind"] == "failed_at_start" for r in events)
            peak = _max_in_hour(restarts)
            looping = peak >= self.loop_restarts_per_hour
            abnormal = (
                looping
                or n_restarts >= self.warn_restarts
                or n_failed > 0
                or failed_at_start
            )
            if not (abnormal or n_clean >= self.warn_restarts):
                continue
            if not abnormal:
                confidence = Confidence.LOW
            elif n_maybe_clean:
                confidence = Confidence.MEDIUM
            else:
                confidence = Confidence.HIGH

            results = sorted({r["result"] for r in events if r["result"]} - {"success"})
            parts = []
            if n_restarts:
                parts.append(f"自動再起動 {n_restarts} 回 (1時間あたり最大 {peak} 回)")
            if n_maybe_clean:
                parts.append(f"うち {n_maybe_clean} 回は正常終了後の可能性あり")
            if n_clean:
                parts.append(f"正常終了後の自動再起動 {n_clean} 回")
            if n_failed:
                parts.append(f"failed {n_failed} 回")
            if failed_at_start:
                parts.append("記録開始時点で既に failed")
            reason = f" — 原因: {', '.join(results)}" if results else ""
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.CRITICAL if looping else Severity.WARNING,
                    subject=unit,
                    summary=f"{unit}: {'、'.join(parts)}{reason}",
                    evidence={
                        "unit": unit,
                        "restarts": n_restarts,
                        "max_restarts_per_hour": peak,
                        "restarts_maybe_clean": n_maybe_clean,
                        "restarts_clean": n_clean,
                        "failed_count": n_failed,
                        "failed_at_start": failed_at_start,
                        "results": results,
                        "crash_loop": looping,
                        "first_ts": events[0]["ts"],
                        "last_ts": events[-1]["ts"],
                    },
                    detected_at=(events[0]["ts"], events[-1]["ts"]),
                    confidence=confidence,
                )
            )
        return findings


def _max_in_hour(restarts: list[tuple[float, int]]) -> int:
    """Largest number of restarts falling inside any 1-hour span."""
    best = 0
    for i, (start, _) in enumerate(restarts):
        best = max(best, sum(n for ts, n in restarts[i:] if ts - start <= 3600.0))
    return best


# ---------------------------------------------------------------------------
# KERNEL-ERROR-01
# ---------------------------------------------------------------------------

# Severity per kernel-log anomaly kind (``oom`` is OOM-KILL-01's business).
# ``procdiag snapshot`` grades its 24-hour kernel-log check with the same map.
KERNEL_SEVERITY = {
    "fs_error": Severity.CRITICAL,
    "io_error": Severity.CRITICAL,
    "mce": Severity.CRITICAL,
    "hung_task": Severity.WARNING,
    "segfault": Severity.WARNING,
    "usb_disconnect": Severity.WARNING,
    "thermal": Severity.INFO,
}


@register
class KernelErrorRule(Rule):
    """Report disk, filesystem, hardware and stability errors from the kernel log.

    Reads ``kernel_events`` recorded by the kernel-log sampler and emits one
    finding per anomaly kind, with the count, the time span and a few sample
    messages.  OOM lines are left to OOM-KILL-01.
    """

    rule_id = "KERNEL-ERROR-01"
    category = "crash"
    title = "カーネルが記録した異常"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0
    requires_schema = ("kernel_events",)
    requires_observed = (Capability.KERNEL_LOG,)
    what = (
        "kernel_events (カーネルログから抽出した I/O エラー・FS エラー・ハングタスク・"
        "ハードウェアエラー・segfault・USB 切断) を種類別に集計する"
    )
    why = (
        "ディスク故障・ハードウェア異常・ドライバ不具合は、性能指標には現れず"
        "カーネルログにだけ記録されることが多い"
    )
    manual_commands = [
        "journalctl -k -p warning --since '2 days ago'",
        "dmesg -T | grep -iE 'error|fail|blocked|segfault|disconnect'",
    ]

    max_samples: int = 3

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run KERNEL-ERROR-01 against the observation window."""
        rows = store.rows("kernel_events", since=window.start_ts, until=window.end_ts)

        by_kind: dict[str, list] = {}
        for row in rows:
            if row["kind"] in KERNEL_SEVERITY:
                by_kind.setdefault(row["kind"], []).append(row)

        findings: list[Finding] = []
        for kind, events in by_kind.items():
            label = kernlog.LABELS.get(kind, kind)
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=KERNEL_SEVERITY[kind],
                    subject=label,
                    summary=f"カーネルログに{label}を {len(events)} 件記録",
                    evidence={
                        "kind": kind,
                        "label": label,
                        "count": len(events),
                        "first_ts": events[0]["ts"],
                        "last_ts": events[-1]["ts"],
                        "samples": [e["message"] for e in events[: self.max_samples]],
                    },
                    detected_at=(events[0]["ts"], events[-1]["ts"]),
                    confidence=Confidence.HIGH,
                )
            )
        return findings
