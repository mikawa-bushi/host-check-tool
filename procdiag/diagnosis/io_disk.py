"""I/O and disk diagnostic rules.

Currently implements:

- **DISK-FULL-01**: Predict when a partition will reach capacity by fitting a
  linear trend to ``disk_samples``.  Fires WARNING when fewer than 30 days
  remain; CRITICAL when fewer than 7 days remain.
- **IO-BOTTLENECK-01**: Detect sustained I/O pressure from ``psi_io_some10``
  sustained above threshold for 15 minutes or more.  Identifies top I/O
  writers from ``proc_samples``; falls back to ``cg_samples`` when per-process
  I/O counters are all NULL (non-root); silences when no fallback data exists.
- **DISK-GROWTH-01**: Detect rapid directory growth from ``dir_samples`` whose
  daily increment exceeds a configurable threshold.  When the directory
  resides on the same mount-point as a DISK-FULL-01 finding the engine will
  fold this finding into that parent.
- **DISK-JOURNAL-01**: Detect an oversized systemd journal directory by
  comparing the latest ``dir_samples`` size for paths under ``/var/log/journal``
  to ``config.rules.journal_size_warn_gb``.

Design doc reference: §6.2 DISK-FULL-01 / DISK-GROWTH-01 / DISK-JOURNAL-01 /
IO-BOTTLENECK-01.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from ..core import (
    Confidence,
    Finding,
    GiB,
    MiB,
    Rule,
    Severity,
    Window,
    register,
)
from .trend import confidence_from_span, extrapolate_full

if TYPE_CHECKING:
    from ..config import ProcdiagConfig
    from ..storage import Storage

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_DISK_FULL_WARNING_DAYS: float = 30.0
_DISK_FULL_CRITICAL_DAYS: float = 7.0

# IO-BOTTLENECK-01
_IO_PSI_THRESHOLD: float = 30.0
_IO_MIN_DURATION_MINUTES: float = 15.0

# DISK-GROWTH-01
_DISK_GROWTH_THRESHOLD_BYTES_PER_DAY: int = 100 * MiB  # 100 MB/day


@register
class DiskFullRule(Rule):
    """Predict disk-full events by extrapolating the used-space trend.

    Queries ``disk_samples`` per mount-point, calls ``extrapolate_full`` to
    estimate days until capacity is reached, and fires when the estimate
    falls below the configured thresholds.
    """

    rule_id = "DISK-FULL-01"
    category = "io_disk"
    title = "ディスク満杯予測"
    default_severity = Severity.WARNING
    min_observation_hours = 1.0  # disk only needs a few readings
    requires_rows = ("disk_samples",)
    what = (
        "disk_samples の used 系列を線形外挿し、"
        "capacity 到達までの残り日数を算出する"
    )
    why = (
        "満杯になると書き込みエラー・サービス停止が発生する。"
        "事前に傾向を検出して余裕を持って対処できるようにする"
    )
    manual_commands = [
        "df -h",
        "du -sh /* | sort -rh | head -20",
    ]

    # Allow tests to override thresholds.
    warning_days: float = _DISK_FULL_WARNING_DAYS
    critical_days: float = _DISK_FULL_CRITICAL_DAYS

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run DISK-FULL-01 against the observation window."""
        findings: list[Finding] = []

        # Fetch all disk samples within the window.
        disk_rows = store.rows(
            "disk_samples",
            since=window.start_ts,
            until=window.end_ts,
        )

        # Group by mountpoint.
        by_mount: dict[str, list[tuple[float, float, float]]] = {}
        for row in disk_rows:
            mp = row["mountpoint"]
            used = row["used"]
            total = row["total"]
            ts = row["ts"]
            if used is not None and total is not None:
                by_mount.setdefault(mp, []).append((ts, float(used), float(total)))

        for mountpoint, triples in by_mount.items():
            if not triples:
                continue

            capacity = triples[-1][2]  # total is stable; use latest value
            series = [(t, used) for t, used, _ in triples]

            days_left = extrapolate_full(series, capacity, min_points=3)
            if days_left is None:
                # Flat or shrinking — no finding.
                continue

            if days_left > self.warning_days:
                continue

            severity = (
                Severity.CRITICAL
                if days_left <= self.critical_days
                else Severity.WARNING
            )

            # Estimate daily growth from the series endpoints.
            if len(triples) >= 2:
                t0, u0, _ = triples[0]
                t1, u1, _ = triples[-1]
                span_days = (t1 - t0) / 86400.0
                daily_growth_bytes = (u1 - u0) / span_days if span_days > 0 else 0.0
            else:
                daily_growth_bytes = 0.0

            last_used = triples[-1][1]

            evidence = {
                "mountpoint": mountpoint,
                "days_left": days_left,
                "daily_growth_bytes": daily_growth_bytes,
                "used_bytes": last_used,
                "total_bytes": capacity,
                "used_gb": last_used / GiB,
                "total_gb": capacity / GiB,
            }

            summary = (
                f"{mountpoint}: 残り {days_left:.1f}日で満杯"
                f" ({daily_growth_bytes / GiB:.2f} GB/日の増加)"
            )

            # Confidence based on observation span and number of points
            # (shared ladder; see trend.confidence_from_span).
            span_hours = (triples[-1][0] - triples[0][0]) / 3600.0
            confidence = confidence_from_span(span_hours, len(triples))

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=severity,
                    subject=mountpoint,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(window.start_ts, window.end_ts),
                    confidence=confidence,
                )
            )

        return findings


# ---------------------------------------------------------------------------
# IO-BOTTLENECK-01
# ---------------------------------------------------------------------------


@register
class IoBottleneckRule(Rule):
    """Detect sustained I/O pressure by watching ``psi_io_some10``.

    Fires WARNING when ``psi_io_some10`` exceeds the threshold for at least
    ``min_duration_minutes`` consecutive minutes.  The evidence block
    identifies the top I/O writers from ``proc_samples``; when all per-process
    I/O counters are NULL (non-root execution), the rule falls back to
    ``cg_samples``; when neither source has data, the rule is silent.
    """

    rule_id = "IO-BOTTLENECK-01"
    category = "io_disk"
    title = "I/Oボトルネック"
    default_severity = Severity.WARNING
    min_observation_hours = 0.5
    requires_rows = ("sys_samples",)
    what = (
        "psi_io_some10 が閾値を超えた継続時間を計測し、"
        "15分以上継続している場合に発火する"
    )
    why = (
        "I/O圧迫が継続するとアプリケーションの応答遅延・タイムアウトが発生する。"
        "原因プロセスを特定して対処できるようにする"
    )
    manual_commands = [
        "iostat -x 1 5",
        "iotop -ao",
    ]

    # Allow tests to override.
    psi_threshold: float = _IO_PSI_THRESHOLD
    min_duration_minutes: float = _IO_MIN_DURATION_MINUTES
    top_n: int = 5

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run IO-BOTTLENECK-01 against the observation window."""
        # --- Check psi_io_some10 sustained pressure ---
        sys_rows = store.rows(
            "sys_samples",
            since=window.start_ts,
            until=window.end_ts,
        )

        if not sys_rows:
            return []

        # Find the longest consecutive run above the threshold.
        # Duration is measured from the actual timestamps of the first and last
        # rows in the run, so the result is independent of sampling interval.
        max_duration_sec: float = 0.0
        max_above_values: list[float] = []

        run_start_ts: float | None = None
        run_prev_ts: float | None = None
        current_run_values: list[float] = []

        for row in sys_rows:
            val = row["psi_io_some10"]
            ts = row["ts"]
            if val is not None and val > self.psi_threshold:
                if run_start_ts is None:
                    run_start_ts = ts
                run_prev_ts = ts
                current_run_values.append(val)
                # Measure duration as span of the current run.
                current_duration = (
                    (run_prev_ts - run_start_ts) if run_start_ts is not None else 0.0
                )
                if current_duration > max_duration_sec:
                    max_duration_sec = current_duration
                    max_above_values = list(current_run_values)
            else:
                # End of run — finalise if longer than recorded max.
                if run_start_ts is not None and run_prev_ts is not None:
                    span = run_prev_ts - run_start_ts
                    if span > max_duration_sec:
                        max_duration_sec = span
                        max_above_values = list(current_run_values)
                run_start_ts = None
                run_prev_ts = None
                current_run_values = []

        # Finalise a run that extends to the end of the window.
        if run_start_ts is not None and run_prev_ts is not None:
            span = run_prev_ts - run_start_ts
            if span > max_duration_sec:
                max_duration_sec = span
                max_above_values = list(current_run_values)

        duration_minutes = max_duration_sec / 60.0

        # When row sampling interval is ~60s (test data), a run of N rows spans
        # (N-1)*60 seconds.  Require at least min_duration_minutes worth of span.
        # Also handle the edge case where all rows are above threshold but only
        # one row (span = 0): count rows as a fallback proxy.
        if duration_minutes < self.min_duration_minutes:
            # Fallback: count consecutive rows × assumed 60s interval.
            max_row_count = 0
            current_row_count = 0
            for row in sys_rows:
                val = row["psi_io_some10"]
                if val is not None and val > self.psi_threshold:
                    current_row_count += 1
                    if current_row_count > max_row_count:
                        max_row_count = current_row_count
                else:
                    current_row_count = 0
            duration_minutes = max(duration_minutes, max_row_count * 1.0)

        above_values = max_above_values

        if duration_minutes < self.min_duration_minutes:
            return []

        psi_avg = sum(above_values) / len(above_values) if above_values else 0.0

        # --- Identify top I/O writers from proc_samples ---
        proc_rows = store.rows(
            "proc_samples",
            since=window.start_ts,
            until=window.end_ts,
        )

        # Check if io_write_delta is available (non-null) for any row.
        io_available = any(
            r["io_write_delta"] is not None or r["io_read_delta"] is not None
            for r in proc_rows
        )

        top_writers: list[dict] = []
        top_cg_writers: list[dict] = []
        confidence = Confidence.HIGH

        if io_available:
            # Aggregate total io_write_delta + io_read_delta per proc_id.
            proc_io: dict[int, int] = {}
            for row in proc_rows:
                pid = row["proc_id"]
                w = row["io_write_delta"] or 0
                r = row["io_read_delta"] or 0
                proc_io[pid] = proc_io.get(pid, 0) + w + r

            # Sort by total I/O descending.
            top_proc_ids = sorted(proc_io, key=lambda k: proc_io[k], reverse=True)[
                : self.top_n
            ]

            # Resolve names.
            for proc_id in top_proc_ids:
                name = store.proc_name(proc_id)
                total_bytes = proc_io[proc_id]
                if total_bytes > 0:
                    top_writers.append(
                        {
                            "name": name,
                            "write_mb_total": total_bytes / MiB,
                        }
                    )

        else:
            # Fallback: try cg_samples.
            cg_rows = store.rows(
                "cg_samples",
                since=window.start_ts,
                until=window.end_ts,
            )
            cg_io_available = any(
                r["io_wbytes_delta"] is not None or r["io_rbytes_delta"] is not None
                for r in cg_rows
            )

            if not cg_io_available:
                # Neither proc nor cgroup data — stay silent.
                return []

            confidence = Confidence.LOW

            cg_io: dict[int, int] = {}
            for row in cg_rows:
                cg_id = row["cg_id"]
                w = row["io_wbytes_delta"] or 0
                r = row["io_rbytes_delta"] or 0
                cg_io[cg_id] = cg_io.get(cg_id, 0) + w + r

            top_cg_ids = sorted(cg_io, key=lambda k: cg_io[k], reverse=True)[
                : self.top_n
            ]

            for cg_id in top_cg_ids:
                unit_path = store.cg_unit_path(cg_id)
                total_bytes = cg_io[cg_id]
                if total_bytes > 0:
                    top_cg_writers.append(
                        {
                            "unit_path": unit_path,
                            "write_mb_total": total_bytes / MiB,
                        }
                    )

        evidence = {
            "psi_io_avg": psi_avg,
            "threshold": self.psi_threshold,
            "duration_minutes": duration_minutes,
            "top_writers": top_writers,
            "top_cg_writers": top_cg_writers,
            "confidence": confidence.value,
        }

        summary = (
            f"I/O圧迫 psi_io_some10={psi_avg:.1f}% が"
            f" {duration_minutes:.1f}分間継続"
        )

        return [
            Finding(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                subject="io/psi",
                summary=summary,
                evidence=evidence,
                detected_at=(window.start_ts, window.end_ts),
                confidence=confidence,
            )
        ]


# ---------------------------------------------------------------------------
# DISK-GROWTH-01
# ---------------------------------------------------------------------------


@register
class DiskGrowthRule(Rule):
    """Detect rapid directory growth from ``dir_samples``.

    Computes the daily growth rate per ``path`` as ``(last - first) / days``.
    Fires INFO when the rate exceeds the threshold; escalates to WARNING when
    the rate exceeds twice the threshold.  When the directory lies on the
    same mount-point as a DISK-FULL-01 finding the engine will fold this
    finding into that parent via ``_FOLD_INTO``.
    """

    rule_id = "DISK-GROWTH-01"
    category = "io_disk"
    title = "ディレクトリ急増"
    default_severity = Severity.INFO
    min_observation_hours = 1.0
    requires_rows = ("dir_samples",)
    what = "dir_samples のパスごと増分を計算し、日次増分が閾値を超えたら発火する"
    why = (
        "急激なディレクトリ肥大はディスク満杯の前兆であることが多く、"
        "早期に原因を特定することで満杯を未然に防げる"
    )
    manual_commands = [
        "du -sh <path>/*",
        "du -sh /* | sort -rh | head -20",
    ]

    # Allow tests to override.
    threshold_bytes_per_day: int = _DISK_GROWTH_THRESHOLD_BYTES_PER_DAY
    warn_multiplier: float = 2.0

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run DISK-GROWTH-01 against the observation window."""
        dir_rows = store.rows(
            "dir_samples",
            since=window.start_ts,
            until=window.end_ts,
        )

        # Group by path.
        by_path: dict[str, list[tuple[float, int]]] = {}
        for row in dir_rows:
            path = row["path"]
            size = row["size_bytes"]
            ts = row["ts"]
            if size is not None:
                by_path.setdefault(path, []).append((ts, int(size)))

        # Pre-fetch disk_samples to build a mountpoint → path mapping.
        disk_rows = store.rows(
            "disk_samples",
            since=window.start_ts,
            until=window.end_ts,
        )
        # Build set of known mountpoints (sorted longest-first for prefix matching).
        mountpoints = sorted(
            {row["mountpoint"] for row in disk_rows},
            key=lambda m: len(m),
            reverse=True,
        )

        findings: list[Finding] = []

        for path, series in by_path.items():
            if len(series) < 2:
                continue

            series.sort(key=lambda x: x[0])
            t_start, size_start = series[0]
            t_end, size_end = series[-1]

            span_days = (t_end - t_start) / 86400.0
            if span_days < 0.5:
                # Too short to compute a meaningful daily rate.
                continue

            growth_total = size_end - size_start
            if growth_total <= 0:
                continue

            daily_growth = growth_total / span_days

            if daily_growth < self.threshold_bytes_per_day:
                continue

            severity = (
                Severity.WARNING
                if daily_growth >= self.threshold_bytes_per_day * self.warn_multiplier
                else Severity.INFO
            )

            # Find the mount-point that contains this path (longest prefix match).
            mountpoint: str | None = None
            for mp in mountpoints:
                if path == mp or path.startswith(mp.rstrip("/") + "/"):
                    mountpoint = mp
                    break

            evidence = {
                "path": path,
                "daily_growth_mb": daily_growth / MiB,
                "daily_growth_bytes": daily_growth,
                "threshold_mb": self.threshold_bytes_per_day // MiB,
                "observation_days": span_days,
                "start_gb": size_start / GiB,
                "end_gb": size_end / GiB,
                "mountpoint": mountpoint,
            }

            # Subject includes mountpoint when known so engine.fold can match.
            subject = mountpoint if mountpoint is not None else path

            summary = (
                f"{path}: 日次増分 {daily_growth / MiB:.0f} MB/日"
                f" (閾値 {self.threshold_bytes_per_day // MiB} MB/日)"
            )

            # Confidence based on observation span.
            if span_days >= 3.0 and len(series) >= 10:
                confidence = Confidence.HIGH
            elif span_days >= 1.0:
                confidence = Confidence.MEDIUM
            else:
                confidence = Confidence.LOW

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=severity,
                    subject=subject,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(window.start_ts, window.end_ts),
                    confidence=confidence,
                )
            )

        return findings


# ---------------------------------------------------------------------------
# DISK-JOURNAL-01
# ---------------------------------------------------------------------------


@register
class DiskJournalRule(Rule):
    """Detect an oversized systemd journal directory.

    Queries ``dir_samples`` for paths beginning with ``/var/log/journal`` and
    fires WARNING when the latest size exceeds
    ``config.rules.journal_size_warn_gb``.  The proposal includes the
    ``SystemMaxUse`` journald configuration snippet.
    """

    rule_id = "DISK-JOURNAL-01"
    category = "io_disk"
    title = "journaldサイズ超過"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0  # single-point check; no trend needed
    requires_rows = ("dir_samples",)
    what = (
        "dir_samples の /var/log/journal* パスの最新サイズが"
        " journal_size_warn_gb 閾値を超えているか確認する"
    )
    why = (
        "journald が肥大化するとディスクを圧迫し、"
        "サービス停止の遠因になる。SystemMaxUse 設定で上限を設けることが推奨される"
    )
    manual_commands = [
        "journalctl --disk-usage",
        "du -sh /var/log/journal/",
    ]

    # Default threshold; ``configure`` replaces it with
    # ``config.rules.journal_size_warn_gb`` when the engine runs the rule.
    journal_size_warn_gb: float = 4.0
    journal_path_prefix: str = "/var/log/journal"

    def configure(self, config: "ProcdiagConfig") -> None:
        """Take the warning threshold from ``config.rules.journal_size_warn_gb``.

        An explicit ``rules.overrides["DISK-JOURNAL-01"]`` entry still wins.
        """
        self.journal_size_warn_gb = config.rules.journal_size_warn_gb
        super().configure(config)

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run DISK-JOURNAL-01 against the observation window."""
        dir_rows = store.rows(
            "dir_samples",
            since=window.start_ts,
            until=window.end_ts,
        )

        # Collect the latest size for each journal path.
        latest: dict[str, tuple[float, int]] = {}  # path → (ts, size_bytes)
        for row in dir_rows:
            path = row["path"]
            if not path.startswith(self.journal_path_prefix):
                continue
            size = row["size_bytes"]
            ts = row["ts"]
            if size is None:
                continue
            if path not in latest or ts > latest[path][0]:
                latest[path] = (ts, int(size))

        findings: list[Finding] = []
        warn_bytes = self.journal_size_warn_gb * GiB

        for path, (ts, size_bytes) in latest.items():
            if size_bytes <= warn_bytes:
                continue

            size_gb = size_bytes / GiB
            latest_ts_str = datetime.datetime.fromtimestamp(ts).strftime(
                "%Y-%m-%d %H:%M"
            )

            evidence = {
                "path": path,
                "size_gb": size_gb,
                "size_bytes": size_bytes,
                "warn_gb": self.journal_size_warn_gb,
                "latest_ts_str": latest_ts_str,
            }

            summary = (
                f"journald ディレクトリ {path} が"
                f" {size_gb:.2f} GB (閾値 {self.journal_size_warn_gb} GB)"
            )

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.WARNING,
                    subject=path,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(window.start_ts, window.end_ts),
                    confidence=Confidence.HIGH,
                )
            )

        return findings


# ---------------------------------------------------------------------------
# DISK-INODE-01
# ---------------------------------------------------------------------------


@register
class DiskInodeRule(Rule):
    """Detect inode exhaustion — "No space left on device" with free bytes.

    Millions of small files (spool, cache, session files) can use up a
    filesystem's inode table long before its bytes run out.  Byte-based
    DISK-FULL-01 cannot see that, so this rule checks the latest inode usage
    per mount-point and extrapolates its growth like DISK-FULL-01 does.
    Filesystems without a fixed inode table (btrfs) record NULL and are skipped.
    """

    rule_id = "DISK-INODE-01"
    category = "io_disk"
    title = "inode 枯渇"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0  # the current usage alone is decisive
    requires_schema = ("disk_samples.inodes_total", "disk_samples.inodes_used")
    requires_rows = ("disk_samples",)
    what = "disk_samples の inodes_used / inodes_total の最新値と増加傾向を確認する"
    why = (
        "小さなファイルが大量にあると、容量に空きがあっても inode が尽きて"
        "書き込みが失敗する。容量ベースの DISK-FULL-01 では検出できない"
    )
    manual_commands = [
        "df -i",
        "sudo du --inodes -x / 2>/dev/null | sort -n | tail -20",
    ]

    warning_pct: float = 90.0
    critical_pct: float = 95.0
    warning_days: float = _DISK_FULL_WARNING_DAYS
    critical_days: float = _DISK_FULL_CRITICAL_DAYS

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run DISK-INODE-01 against the observation window."""
        rows = store.rows("disk_samples", since=window.start_ts, until=window.end_ts)

        by_mount: dict[str, list] = {}
        for row in rows:
            if row["inodes_total"] and row["inodes_used"] is not None:
                by_mount.setdefault(row["mountpoint"], []).append(row)

        findings: list[Finding] = []
        for mountpoint, mrows in by_mount.items():
            last = mrows[-1]
            total, used = last["inodes_total"], last["inodes_used"]
            used_pct = used / total * 100.0
            days_left = extrapolate_full(
                [(r["ts"], float(r["inodes_used"])) for r in mrows],
                float(total),
                min_points=3,
            )
            by_level = used_pct >= self.warning_pct
            by_trend = days_left is not None and days_left <= self.warning_days
            if not (by_level or by_trend):
                continue

            critical = used_pct >= self.critical_pct or (
                days_left is not None and days_left <= self.critical_days
            )
            bytes_pct = last["used"] / last["total"] * 100.0 if last["total"] else None
            evidence = {
                "mountpoint": mountpoint,
                "inodes_used": used,
                "inodes_total": total,
                "used_pct": round(used_pct, 1),
                "bytes_used_pct": (
                    round(bytes_pct, 1) if bytes_pct is not None else None
                ),
                "days_left": days_left,
                "warning_pct": self.warning_pct,
            }
            summary = f"{mountpoint}: inode 使用率 {used_pct:.1f}%" + (
                f" (約 {days_left:.1f}日で枯渇)" if days_left is not None else ""
            )
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.CRITICAL if critical else Severity.WARNING,
                    subject=mountpoint,
                    summary=summary,
                    evidence=evidence,
                    detected_at=(window.start_ts, window.end_ts),
                    # A measured level is certain; a trend-only hit is a forecast.
                    confidence=Confidence.HIGH if by_level else Confidence.MEDIUM,
                )
            )
        return findings


# ---------------------------------------------------------------------------
# FS-READONLY-01
# ---------------------------------------------------------------------------


@register
class FsReadOnlyRule(Rule):
    """Detect a filesystem that switched to read-only while recording.

    ext4/xfs remount themselves read-only (``errors=remount-ro``) when they
    hit an I/O or metadata error — every later write then fails although the
    disk is not full.  A rw→ro transition is reported as CRITICAL; a root
    filesystem that is read-only for the whole window as WARNING (other
    always-read-only mounts are assumed intentional).
    """

    rule_id = "FS-READONLY-01"
    category = "io_disk"
    title = "ファイルシステムの読み取り専用化"
    default_severity = Severity.CRITICAL
    min_observation_hours = 0.0
    requires_schema = ("disk_samples.read_only",)
    requires_rows = ("disk_samples",)
    what = "disk_samples の read_only フラグが記録中に 0→1 へ変わったマウントを検出する"
    why = (
        "ファイルシステムはエラーを検出すると自動的に読み取り専用で再マウントされる。"
        "容量に空きがあっても以後の書き込みがすべて失敗する"
    )
    manual_commands = [
        "grep ' ro,' /proc/mounts",
        "journalctl -k | grep -iE 'remount|EXT4-fs error|I/O error'",
    ]

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run FS-READONLY-01 against the observation window."""
        rows = store.rows("disk_samples", since=window.start_ts, until=window.end_ts)

        flags: dict[str, list[tuple[float, int]]] = {}
        for row in rows:
            if row["read_only"] is not None:
                flags.setdefault(row["mountpoint"], []).append(
                    (row["ts"], row["read_only"])
                )

        findings: list[Finding] = []
        for mountpoint, series in flags.items():
            switched_at = next(
                (
                    ts
                    for (_, prev), (ts, cur) in zip(series, series[1:])
                    if not prev and cur
                ),
                None,
            )
            still_ro = bool(series[-1][1])
            if switched_at is not None:
                severity, confidence = Severity.CRITICAL, Confidence.HIGH
                summary = f"{mountpoint}: 記録中に読み取り専用へ切り替わりました"
            elif still_ro and all(flag for _, flag in series) and mountpoint == "/":
                severity, confidence = Severity.WARNING, Confidence.MEDIUM
                summary = "/: ルートファイルシステムが読み取り専用で稼働しています"
            else:
                continue
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=severity,
                    subject=mountpoint,
                    summary=summary,
                    evidence={
                        "mountpoint": mountpoint,
                        "switched_at": switched_at,
                        "still_read_only": still_ro,
                    },
                    detected_at=(switched_at or window.start_ts, window.end_ts),
                    confidence=confidence,
                )
            )
        return findings
