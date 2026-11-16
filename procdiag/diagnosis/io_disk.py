"""I/O and disk diagnostic rules.

Currently implements:

- **DISK-FULL-01**: Report a partition that is full for non-root processes
  (WARNING from 90% used, CRITICAL from 95%), or predict when it will be by
  fitting a linear trend to ``disk_samples`` (WARNING when fewer than 30 days
  remain; CRITICAL when fewer than 7 days remain).
- **IO-BOTTLENECK-01**: Detect sustained I/O pressure from ``psi_io_some10``
  sustained above threshold for 15 minutes or more.  Ranks the processes that
  did the most I/O while it lasted from ``proc_samples``; falls back to
  ``cg_samples`` when per-process I/O counters are all NULL (non-root);
  silences when no fallback data exists.
- **DISK-GROWTH-01**: Detect rapid directory growth from ``dir_samples`` whose
  daily increment between complete scans exceeds a configurable threshold.
  When the directory
  resides on the same mount-point as a DISK-FULL-01 finding the engine will
  fold this finding into that parent.
- **DISK-JOURNAL-01**: Detect an oversized systemd journal directory by
  comparing the latest ``dir_samples`` size for paths under ``/var/log/journal``
  to ``config.rules.journal_size_warn_gb``.  Not evaluated when no such path
  was measured (watching ``/var/log`` alone does not measure the journal).

Design doc reference: §6.2 DISK-FULL-01 / DISK-GROWTH-01 / DISK-JOURNAL-01 /
IO-BOTTLENECK-01.
"""

from __future__ import annotations

import datetime
import math
from typing import TYPE_CHECKING, Optional

from ..core import (
    Capability,
    Confidence,
    Finding,
    GiB,
    MiB,
    Rule,
    Severity,
    SkippedRule,
    Window,
    covers,
    register,
)
from .episodes import MINUTE, sustained_minutes
from .trend import Forecast, confidence_from_span, forecast_full

if TYPE_CHECKING:
    from ..config import ProcdiagConfig
    from ..storage import Storage

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_DISK_FULL_WARNING_DAYS: float = 30.0
_DISK_FULL_CRITICAL_DAYS: float = 7.0
# A forecast of DISK-FULL-01 / DISK-INODE-01 needs this much of the mount's
# recording, the 6 hours the other trend rules need by default.
_MIN_TREND_HOURS: float = 6.0

# IO-BOTTLENECK-01
_IO_PSI_THRESHOLD: float = 30.0
_IO_MIN_DURATION_MINUTES: float = 15.0

# DISK-GROWTH-01
_DISK_GROWTH_THRESHOLD_BYTES_PER_DAY: int = 100 * MiB  # 100 MB/day


@register
class DiskFullRule(Rule):
    """Report a disk that is full or will soon be.

    The usage is that of the space a non-root process can write (``used`` +
    ``avail``, as df shows it): ext4 keeps 5% for root by default, and the
    applications fail when the rest is gone.  The latest usage is checked
    against ``warning_pct`` / ``critical_pct`` like ``snapshot`` does, and
    the ``used`` series is extrapolated to that space with
    :func:`~.trend.forecast_full` once the mount's current filesystem covers
    ``min_trend_hours`` of recording.  Rows recorded before ``avail`` existed
    count usage against ``total``.
    """

    rule_id = "DISK-FULL-01"
    category = "io_disk"
    title = "ディスク満杯予測"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0  # a full disk is decisive at once
    requires_rows = ("disk_samples",)
    what = (
        "disk_samples の最新の使用率 (root 以外が書き込める容量に対する割合) を"
        "確認し、used 系列を線形外挿して、その容量に届くまでの残り日数を算出する"
    )
    why = (
        "満杯になると書き込みエラー・サービス停止が発生する。"
        "事前に傾向を検出して余裕を持って対処できるようにする。"
        "root 用の予約領域はアプリケーションには使えないので、空きに数えない"
    )
    manual_commands = [
        "df -h",
        "du -sh /* | sort -rh | head -20",
    ]

    # Allow tests to override thresholds.
    warning_days: float = _DISK_FULL_WARNING_DAYS
    critical_days: float = _DISK_FULL_CRITICAL_DAYS
    warning_pct: float = 90.0
    critical_pct: float = 95.0
    min_trend_hours: float = _MIN_TREND_HOURS

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run DISK-FULL-01 against the observation window."""
        findings: list[Finding] = []
        for mountpoint, mrows in _current_filesystems(
            store, window, _has_bytes
        ).items():
            last = mrows[-1]
            used, total, capacity = last["used"], last["total"], usable_bytes(last)
            if capacity <= 0:
                continue
            used_pct = used / capacity * 100.0
            writable = not _column(last, "read_only")
            forecast, hours = _forecast(
                mrows, window, "used", capacity, self.min_trend_hours
            )

            if forecast is not None and forecast.days_left > self.warning_days:
                forecast = None
            by_level = writable and used_pct >= self.warning_pct
            if not (by_level or forecast):
                continue
            critical = (writable and used_pct >= self.critical_pct) or (
                forecast is not None and forecast.days_left <= self.critical_days
            )

            free = capacity - used
            evidence = {
                "mountpoint": mountpoint,
                "days_left": forecast.days_left if forecast else None,
                "daily_growth_bytes": forecast.per_day if forecast else None,
                "used_pct": round(used_pct, 1),
                "used_bytes": used,
                "avail_bytes": free,
                "total_bytes": total,
                "used_gb": used / GiB,
                "avail_gb": free / GiB,
                "total_gb": total / GiB,
                "avail_recorded": _column(last, "avail") is not None,
                "warning_pct": self.warning_pct,
                "critical_pct": self.critical_pct,
                "critical": critical,
            }

            parts = []
            if by_level:
                parts.append(f"使用率 {used_pct:.1f}% (空き {free / GiB:.1f} GB)")
            if forecast is not None:
                parts.append(
                    f"残り {forecast.days_left:.1f}日で満杯"
                    f" ({forecast.per_day / GiB:.2f} GB/日の増加)"
                )

            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=Severity.CRITICAL if critical else Severity.WARNING,
                    subject=mountpoint,
                    summary=f"{mountpoint}: " + "、".join(parts),
                    evidence=evidence,
                    detected_at=(window.start_ts, window.end_ts),
                    # A measured level is certain; a forecast only as its span.
                    confidence=(
                        Confidence.HIGH
                        if by_level
                        else confidence_from_span(hours, len(mrows))
                    ),
                )
            )

        return findings

    def pending_parts(self, store: "Storage", window: Window) -> list[SkippedRule]:
        """The mounts whose days-to-full forecast waits for more recording."""
        mounts = _current_filesystems(store, window, _has_bytes)
        return _forecast_pending(self, mounts, window)


# A filesystem whose size changes by more than this from one sample to the
# next is another one: a different USB stick or card at the same mount point,
# or a resized partition.  A ZFS dataset's size drifts as the pool fills, a
# little at a time.
_SAME_SIZE_TOLERANCE = 0.01


def _same_size(a: Optional[int], b: Optional[int]) -> bool:
    return (
        a is not None
        and b is not None
        and abs(a - b) <= _SAME_SIZE_TOLERANCE * max(a, b)
    )


def _one_filesystem(rows: list) -> list:
    """The last of a mount's *rows*: those of the filesystem it has now."""
    first = len(rows) - 1
    while first > 0 and _same_size(rows[first - 1]["total"], rows[first]["total"]):
        first -= 1
    return rows[first:]


def _has_bytes(row) -> bool:
    return row["used"] is not None and row["total"] is not None


def _has_inodes(row) -> bool:
    return bool(row["inodes_total"]) and row["inodes_used"] is not None


def _current_filesystems(store: "Storage", window: Window, usable) -> dict[str, list]:
    """Each mount's ``disk_samples`` rows of the filesystem it has now.

    Only the rows *usable* accepts are looked at.
    """
    by_mount: dict[str, list] = {}
    rows = store.rows("disk_samples", since=window.start_ts, until=window.end_ts)
    for row in rows:
        if usable(row):
            by_mount.setdefault(row["mountpoint"], []).append(row)
    return {mount: _one_filesystem(rows) for mount, rows in by_mount.items()}


def _column(row, name: str):
    """``row[name]``, or None for a column an older DB does not have."""
    return row[name] if name in row.keys() else None


def usable_bytes(row) -> int:
    """What a non-root process can fill on a ``disk_samples`` *row*.

    ``used + avail``, as df counts it; ``total`` for a row recorded before
    ``avail`` existed.
    """
    avail = _column(row, "avail")
    return row["used"] + avail if avail is not None else row["total"]


def _forecast(
    rows: list, window: Window, column: str, capacity: float, min_hours: float
) -> tuple[Optional[Forecast], float]:
    """The forecast of one filesystem's *column* series, and the hours it covers.

    None unless *rows* cover *min_hours* of recording: a stick filled for a
    few minutes, or one large copy in an hour, is not a trend.
    """
    hours = _recorded_hours(rows, window)
    if not covers(hours, min_hours):
        return None, hours
    series = [(r["ts"], float(r[column])) for r in rows]
    return forecast_full(series, capacity, min_points=3), hours


def _recorded_hours(rows: list, window: Window) -> float:
    return window.observed_hours_between(rows[0]["ts"], rows[-1]["ts"])


_FORECAST_PART = "残り日数の予測"


def _forecast_pending(
    rule: "DiskFullRule | DiskInodeRule", mounts: dict[str, list], window: Window
) -> list[SkippedRule]:
    """One entry for each mount whose forecast the recording is too short for.

    Each says how many more hours the mount's current filesystem needs.  The
    usage level is judged whatever the recording, so only the forecast waits.
    """
    pending = []
    for mount, rows in mounts.items():
        hours = _recorded_hours(rows, window)
        if not covers(hours, rule.min_trend_hours):
            pending.append(
                SkippedRule(
                    rule_id=rule.rule_id,
                    title=rule.title,
                    required_hours=rule.min_trend_hours,
                    remaining_hours=max(rule.min_trend_hours - hours, 0.1),
                    part=_FORECAST_PART,
                    subject=mount,
                )
            )
    return pending


# ---------------------------------------------------------------------------
# IO-BOTTLENECK-01
# ---------------------------------------------------------------------------


@register
class IoBottleneckRule(Rule):
    """Detect sustained I/O pressure by watching ``psi_io_some10``.

    A minute counts when ``psi_io_some10`` was above the threshold for at least
    half of it; the rule fires WARNING for the longest run of at least
    ``min_duration_minutes`` such minutes (see ``episodes``), so seconds of
    pressure and recording gaps are never read as minutes.  The evidence block
    ranks the processes by what they read and wrote during that run, from
    ``proc_samples``; when all per-process I/O counters are NULL (non-root
    execution), the rule falls back to ``cg_samples``; when neither source has
    data, the rule is silent.
    """

    rule_id = "IO-BOTTLENECK-01"
    category = "io_disk"
    title = "I/Oボトルネック"
    default_severity = Severity.WARNING
    min_observation_hours = 0.5
    requires_rows = ("sys_samples", "sys_samples.psi_io_some10")
    requires_observed = (Capability.PSI,)
    what = (
        "psi_io_some10 が閾値を超えていた秒が 1 分の半分以上を占める分を数え、"
        "それが 15分以上連続している場合に発火する。"
        "原因プロセスは、その間の読み書きの量で選ぶ"
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
        sys_rows = store.rows(
            "sys_samples",
            since=window.start_ts,
            until=window.end_ts,
        )
        if not sys_rows:
            return []

        pressure = [
            (
                row["ts"],
                row["psi_io_some10"] is not None
                and row["psi_io_some10"] > self.psi_threshold,
            )
            for row in sys_rows
        ]
        episodes = sustained_minutes(
            pressure, min_minutes=max(1, math.ceil(self.min_duration_minutes))
        )
        if not episodes:
            return []

        ep_start, ep_end = max(episodes, key=lambda ep: ep[1] - ep[0])
        ep_stop = ep_end + MINUTE
        duration_minutes = (ep_stop - ep_start) / MINUTE

        above_values = [
            row["psi_io_some10"]
            for row in sys_rows
            if ep_start <= row["ts"] < ep_stop
            and row["psi_io_some10"] is not None
            and row["psi_io_some10"] > self.psi_threshold
        ]
        psi_avg = sum(above_values) / len(above_values)

        # The culprits are ranked by what they read and wrote while the pressure
        # lasted, not over the whole window.
        proc_rows = store.rows(
            "proc_samples",
            since=window.start_ts,
            until=window.end_ts,
        )
        io_available = any(
            r["io_write_delta"] is not None or r["io_read_delta"] is not None
            for r in proc_rows
        )

        top_writers: list[dict] = []
        top_cg_writers: list[dict] = []
        confidence = Confidence.HIGH

        if io_available:
            episode_rows = [r for r in proc_rows if ep_start <= r["ts"] < ep_stop]
            for proc_id, read, write in _top_io(
                episode_rows, "proc_id", "io_read_delta", "io_write_delta", self.top_n
            ):
                top_writers.append(
                    {"name": store.proc_name(proc_id)} | _io_megabytes(read, write)
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

            episode_cg_rows = [r for r in cg_rows if ep_start <= r["ts"] < ep_stop]
            for cg_id, read, write in _top_io(
                episode_cg_rows,
                "cg_id",
                "io_rbytes_delta",
                "io_wbytes_delta",
                self.top_n,
            ):
                top_cg_writers.append(
                    {"unit_path": store.cg_unit_path(cg_id)}
                    | _io_megabytes(read, write)
                )

        evidence = {
            "psi_io_avg": psi_avg,
            "threshold": self.psi_threshold,
            "duration_minutes": duration_minutes,
            "episode_start": ep_start,
            "episode_end": ep_end,
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
                detected_at=(ep_start, ep_end),
                confidence=confidence,
            )
        ]


def _top_io(
    rows: list, key: str, read_col: str, write_col: str, top_n: int
) -> list[tuple[int, int, int]]:
    """``(id, bytes read, bytes written)`` of the *top_n* ids by read + write."""
    totals: dict[int, list[int]] = {}
    for row in rows:
        read_write = totals.setdefault(row[key], [0, 0])
        read_write[0] += row[read_col] or 0
        read_write[1] += row[write_col] or 0
    ranked = sorted(totals.items(), key=lambda kv: sum(kv[1]), reverse=True)
    return [(i, read, write) for i, (read, write) in ranked[:top_n] if read + write]


def _io_megabytes(read: int, write: int) -> dict[str, float]:
    return {
        "read_mb_total": read / MiB,
        "write_mb_total": write / MiB,
        "io_mb_total": (read + write) / MiB,
    }


# ---------------------------------------------------------------------------
# DISK-GROWTH-01
# ---------------------------------------------------------------------------


@register
class DiskGrowthRule(Rule):
    """Detect rapid directory growth from ``dir_samples``.

    Computes the daily growth rate per ``path`` as ``(last - first) / days``
    over the complete scans: a scan cut short by its time budget
    (``is_estimate``) counted only part of the directory.  A window whose scans
    were all cut short is not evaluated.
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
    requires_rows = ("dir_samples", "dir_samples.size_bytes")
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

    def missing_rows(self, store: "Storage", window: Window) -> tuple[str, ...]:
        """A complete scan, when the window has scans of the watched directories."""
        rows = store.rows("dir_samples", since=window.start_ts, until=window.end_ts)
        if not rows or any(_complete(row) for row in rows):
            return ()
        return ("dir_samples (時間内に終わった走査)",)

    def coverage_notes(self, store: "Storage", window: Window) -> tuple[str, ...]:
        """The directories cut-short scans leave without two complete ones.

        Growth needs two complete scans.  A directory scanned to the end once
        or never in the window is named, however many scans were cut short.
        """
        rows = store.rows("dir_samples", since=window.start_ts, until=window.end_ts)
        done: dict[str, int] = {}
        cut: dict[str, int] = {}
        for row in rows:
            if row["size_bytes"] is None:
                continue
            counts = done if _complete(row) else cut
            counts[row["path"]] = counts.get(row["path"], 0) + 1
        short = [path for path in cut if done.get(path, 0) < 2]
        if not short:
            return ()
        return (
            f"{self.rule_id}: 走査が時間内に終わらず、完了した走査が 2 回に満たず"
            "増分を判定できません: "
            + "、".join(f"{p} (完了 {done.get(p, 0)} 回)" for p in short),
        )

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
            if _complete(row):
                by_path.setdefault(row["path"], []).append(
                    (row["ts"], int(row["size_bytes"]))
                )

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

            threshold_mb = self.threshold_bytes_per_day / MiB
            evidence = {
                "path": path,
                "daily_growth_mb": daily_growth / MiB,
                "daily_growth_bytes": daily_growth,
                "threshold_mb": threshold_mb,
                "observation_days": span_days,
                "start_gb": size_start / GiB,
                "end_gb": size_end / GiB,
                "mountpoint": mountpoint,
            }

            # Subject includes mountpoint when known so engine.fold can match.
            subject = mountpoint if mountpoint is not None else path

            summary = (
                f"{path}: 日次増分 {daily_growth / MiB:.0f} MB/日"
                f" (閾値 {round(threshold_mb, 2):g} MB/日)"
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


def _complete(row) -> bool:
    """Whether a ``dir_samples`` row is a size: read, and not cut short."""
    return row["size_bytes"] is not None and not row["is_estimate"]


# ---------------------------------------------------------------------------
# DISK-JOURNAL-01
# ---------------------------------------------------------------------------


@register
class DiskJournalRule(Rule):
    """Detect an oversized systemd journal directory.

    Queries ``dir_samples`` for paths beginning with ``/var/log/journal`` and
    fires WARNING when the latest size exceeds
    ``config.rules.journal_size_warn_gb``.  The proposal includes the
    ``SystemMaxUse`` journald configuration snippet.  A window with watched
    directories but no measured journal path is not evaluated: the size of
    ``/var/log`` does not tell the journal's.
    """

    rule_id = "DISK-JOURNAL-01"
    category = "io_disk"
    title = "journaldサイズ超過"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0  # single-point check; no trend needed
    requires_rows = ("dir_samples", "dir_samples.size_bytes")
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

    def missing_rows(self, store: "Storage", window: Window) -> tuple[str, ...]:
        """A measured journal path, when the window has watched directories."""
        rows = store.rows("dir_samples", since=window.start_ts, until=window.end_ts)
        if not rows or any(
            row["path"].startswith(self.journal_path_prefix)
            and row["size_bytes"] is not None
            for row in rows
        ):
            return ()
        return (f"dir_samples ({self.journal_path_prefix})",)

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
    per mount-point and extrapolates its growth like DISK-FULL-01 does, from
    ``min_trend_hours`` of the mount's current filesystem.
    Filesystems without a fixed inode table (btrfs) record NULL and are skipped.
    """

    rule_id = "DISK-INODE-01"
    category = "io_disk"
    title = "inode 枯渇"
    default_severity = Severity.WARNING
    min_observation_hours = 0.0  # the current usage alone is decisive
    requires_schema = ("disk_samples.inodes_total", "disk_samples.inodes_used")
    requires_rows = ("disk_samples", "disk_samples.inodes_total")
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
    min_trend_hours: float = _MIN_TREND_HOURS

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run DISK-INODE-01 against the observation window."""
        findings: list[Finding] = []
        for mountpoint, mrows in _current_filesystems(
            store, window, _has_inodes
        ).items():
            last = mrows[-1]
            total, used = last["inodes_total"], last["inodes_used"]
            used_pct = used / total * 100.0
            forecast, _ = _forecast(
                mrows, window, "inodes_used", float(total), self.min_trend_hours
            )
            days_left = forecast.days_left if forecast else None
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

    def pending_parts(self, store: "Storage", window: Window) -> list[SkippedRule]:
        """The mounts whose inode forecast waits for more recording."""
        mounts = _current_filesystems(store, window, _has_inodes)
        return _forecast_pending(self, mounts, window)


# ---------------------------------------------------------------------------
# FS-READONLY-01
# ---------------------------------------------------------------------------


@register
class FsReadOnlyRule(Rule):
    """Detect a filesystem that switched to read-only while recording.

    ext4/xfs remount themselves read-only (``errors=remount-ro``) when they
    hit an I/O or metadata error — every later write then fails although the
    disk is not full.  A rw→ro transition of one filesystem is reported as
    CRITICAL (a write-protected card mounted where a stick was is not one); a
    root filesystem that is read-only for the whole window as WARNING (other
    always-read-only mounts are assumed intentional).
    """

    rule_id = "FS-READONLY-01"
    category = "io_disk"
    title = "ファイルシステムの読み取り専用化"
    default_severity = Severity.CRITICAL
    min_observation_hours = 0.0
    requires_schema = ("disk_samples.read_only",)
    requires_rows = ("disk_samples", "disk_samples.read_only")
    what = "disk_samples の read_only フラグが記録中に 0→1 へ変わったマウントを検出する"
    why = (
        "ファイルシステムはエラーを検出すると自動的に読み取り専用で再マウントされる。"
        "容量に空きがあっても以後の書き込みがすべて失敗する"
    )
    manual_commands = [
        "grep ' ro,' /proc/mounts",
        "journalctl -k -b all | grep -iE 'remount|EXT4-fs error|I/O error'",
    ]

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run FS-READONLY-01 against the observation window."""
        rows = store.rows("disk_samples", since=window.start_ts, until=window.end_ts)

        flags: dict[str, list] = {}
        for row in rows:
            if row["read_only"] is not None:
                flags.setdefault(row["mountpoint"], []).append(row)

        findings: list[Finding] = []
        for mountpoint, series in flags.items():
            switched_at = next(
                (
                    cur["ts"]
                    for prev, cur in zip(series, series[1:])
                    if not prev["read_only"]
                    and cur["read_only"]
                    and _same_size(prev["total"], cur["total"])
                ),
                None,
            )
            still_ro = bool(series[-1]["read_only"])
            if switched_at is not None:
                severity, confidence = Severity.CRITICAL, Confidence.HIGH
                summary = f"{mountpoint}: 記録中に読み取り専用へ切り替わりました"
            elif still_ro and all(r["read_only"] for r in series) and mountpoint == "/":
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
