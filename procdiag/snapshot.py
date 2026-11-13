"""``procdiag snapshot`` — an instant, recording-free health check of the host.

``check`` needs hours of recorded data before its trend rules can run.  The
snapshot answers "is anything wrong with this PC *right now*?" in a few
seconds, so it is the first command to run on a production-floor PC: CPU /
memory / I/O pressure, disk space / inodes / read-only mounts, file handles,
zombies and the heaviest processes, failed or crash-restarted services, kernel
errors in the last 24 hours, temperature, clock synchronisation, and whether a
recording is running.

Each check returns :class:`SnapshotItem` s (``ok`` / ``warning`` /
``critical`` / ``info``).  Thresholds reuse the diagnosis rules' values —
including ``config.rules.overrides`` — so ``snapshot`` and ``check`` agree.
A check that cannot run (missing permission, no systemd, no sensors) degrades
to ``info`` instead of failing the snapshot.
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import psutil

from . import kernlog, systemd
from .config import ProcdiagConfig
from .core import Severity, configure_rule
from .core.util import GiB, humanize_bytes, humanize_duration

OK, WARNING, CRITICAL, INFO = "ok", "warning", "critical", "info"
STATUSES = (CRITICAL, WARNING, INFO, OK)  # worst first

# Snapshot-only thresholds (no recorded-data rule covers these directly).
_MEM_AVAIL_WARN_PCT = 10.0
_MEM_AVAIL_CRIT_PCT = 5.0  # same crisis level OOM-KILL-01 uses
_SWAP_WARN_PCT = 80.0
_DISK_WARN_PCT = 90.0
_DISK_CRIT_PCT = 95.0
_FD_WARN_PCT = 80.0
_FD_CRIT_PCT = 90.0
# Newer kernels report fs.file-max as LONG_MAX, i.e. effectively unlimited.
_FD_UNLIMITED = 2**62
_ZOMBIE_WARN = 50
_LOAD_PER_CORE_WARN = 2.0  # fallback judgement on kernels without PSI
_KERNEL_LOOKBACK_SEC = 24 * 3600.0
_RECORDING_FRESH_SEC = 180.0
_TOP_N = 3


@dataclass
class SnapshotItem:
    """One line of the snapshot."""

    category: str  # display group (Japanese)
    name: str  # stable machine key, e.g. "disk:/"
    label: str  # Japanese display label
    status: str  # ok | warning | critical | info
    detail: str  # what was measured (Japanese)
    hint: str = ""  # what to do next (Japanese), empty when nothing to do

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ``item(status, detail, hint="", *, name=..., label=...)`` — see ``_check``.
_Item = Callable[..., SnapshotItem]


def _check(category: str, name: str, label: str) -> Callable:
    """Declare a check: inject its item factory and degrade errors to ``info``.

    The decorated body receives ``item`` as its first argument — a
    :class:`SnapshotItem` factory pre-filled with this check's category, name
    and label (a check producing several items overrides ``name`` / ``label``
    per item) — and callers invoke the check without it.  An exception in the
    body becomes one ``info`` item instead of aborting the snapshot.
    """

    def item(
        status: str,
        detail: str,
        hint: str = "",
        *,
        name: str = name,
        label: str = label,
    ) -> SnapshotItem:
        return SnapshotItem(category, name, label, status, detail, hint)

    def decorate(
        body: Callable[..., List[SnapshotItem]],
    ) -> Callable[..., List[SnapshotItem]]:
        @functools.wraps(body)
        def run(*args: Any, **kwargs: Any) -> List[SnapshotItem]:
            try:
                return body(item, *args, **kwargs)
            except Exception as exc:
                return [item(INFO, f"取得できませんでした: {exc}")]

        return run

    return decorate


def _advice(status: str, hint: str) -> str:
    """*hint* when *status* needs attention, "" when all is well."""
    return "" if status == OK else hint


def _threshold(config: ProcdiagConfig, rule_cls: type, attr: str) -> float:
    """A rule's effective threshold (class default + ``rules.overrides``)."""
    rule, _error = configure_rule(rule_cls, config)  # the CLI reports bad overrides
    return float(getattr(rule, attr))


def _read_psi_avg60(proc_root: Path, resource: str) -> Optional[Dict[str, float]]:
    from .samplers.system import parse_psi

    try:
        text = (proc_root / "pressure" / resource).read_text()
    except OSError:
        return None
    return parse_psi(text, keys=frozenset({"avg60"}))


def _level(value: float, warn: float, crit: Optional[float] = None) -> str:
    if crit is not None and value >= crit:
        return CRITICAL
    return WARNING if value >= warn else OK


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@_check("システム", "uptime", "稼働時間")
def _check_uptime(item: _Item) -> List[SnapshotItem]:
    boot = psutil.boot_time()
    up = time.time() - boot
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(boot))
    hint = (
        "直近に再起動しています。意図しない再起動なら原因を確認してください"
        if up < 3600
        else ""
    )
    return [item(INFO, f"{humanize_duration(up)} (起動 {started})", hint)]


@_check("システム", "time_sync", "時刻同期")
def _check_time_sync(item: _Item) -> List[SnapshotItem]:
    synced = systemd.ntp_synchronized()
    if synced is None:
        return [item(INFO, "確認できません (timedatectl なし)")]
    if synced:
        return [item(OK, "NTP で同期済み")]
    return [
        item(
            WARNING,
            "時刻が同期されていません",
            "ログや生産データの時刻がずれます。`timedatectl` で NTP の設定を確認してください",
        )
    ]


@_check("システム", "recording", "記録状況")
def _check_recording(item: _Item, config: ProcdiagConfig) -> List[SnapshotItem]:
    from .storage import Storage

    db = Path(config.db_path)
    if not db.exists():
        return [
            item(
                INFO,
                "未記録",
                "リーク・周期的な負荷など長期傾向の診断には procdiag record で記録してください",
            )
        ]
    with Storage(db, read_only=True) as store:
        bounds = store.time_bounds("sys_samples")
    if bounds is None:
        return [item(INFO, f"DB はありますがサンプルがありません ({db})")]
    age = time.time() - bounds[1]
    if age <= _RECORDING_FRESH_SEC:
        return [
            item(
                OK,
                f"記録中 (記録期間 {humanize_duration(bounds[1] - bounds[0])})",
                "長期傾向の診断は procdiag check で確認できます",
            )
        ]
    return [item(INFO, f"記録は停止中 (最終記録 {humanize_duration(age)}前)")]


@_check("CPU", "cpu", "CPU")
def _check_cpu(
    item: _Item, config: ProcdiagConfig, per_core: List[float], proc_root: Path
) -> List[SnapshotItem]:
    from .diagnosis.cpu import CpuSaturationRule

    total = sum(per_core) / len(per_core) if per_core else 0.0
    peak = max(per_core) if per_core else 0.0
    detail = f"使用率 {total:.0f}% (最大コア {peak:.0f}%)"
    psi = _read_psi_avg60(proc_root, "cpu")
    if psi and "some_avg60" in psi:
        threshold = _threshold(config, CpuSaturationRule, "psi_threshold")
        value = psi["some_avg60"]
        status = _level(value, threshold)
        detail += f" / CPU 待ち (PSI 60秒平均) {value:.1f}%"
    else:
        ncpu = psutil.cpu_count() or 1
        load5 = os.getloadavg()[1]
        status = _level(load5 / ncpu, _LOAD_PER_CORE_WARN)
        detail += f" / ロードアベレージ(5分) {load5:.1f} ({ncpu} コア)"
    hint = "CPU が詰まっています。下の「CPU 使用率の上位」を確認してください"
    return [item(status, detail, _advice(status, hint))]


@_check("メモリ", "memory", "メモリ")
def _check_memory(
    item: _Item, config: ProcdiagConfig, proc_root: Path
) -> List[SnapshotItem]:
    vm = psutil.virtual_memory()
    avail_pct = vm.available / vm.total * 100.0 if vm.total else 100.0
    if avail_pct < _MEM_AVAIL_CRIT_PCT:
        status = CRITICAL
    elif avail_pct < _MEM_AVAIL_WARN_PCT:
        status = WARNING
    else:
        status = OK
    detail = f"空き {humanize_bytes(vm.available)} / {humanize_bytes(vm.total)} ({avail_pct:.0f}%)"
    psi = _read_psi_avg60(proc_root, "memory")
    if psi and psi.get("full_avg60", 0.0) >= config.trigger.psi_mem_full10:
        if status == OK:
            # Memory stalls despite free memory (fragmentation, cgroup limits).
            status = WARNING
        detail += f" / 全停止 (PSI full 60秒平均) {psi['full_avg60']:.1f}%"
    hint = "メモリ不足です。下の「メモリ使用量の上位」を確認してください"
    items = [item(status, detail, _advice(status, hint))]

    sw = psutil.swap_memory()
    if sw.total:
        items.append(
            item(
                _level(sw.percent, _SWAP_WARN_PCT),
                f"使用 {humanize_bytes(sw.used)} / {humanize_bytes(sw.total)} ({sw.percent:.0f}%)",
                name="swap",
                label="スワップ",
            )
        )
    return items


@_check("ディスク", "io", "ディスク I/O")
def _check_io(
    item: _Item, config: ProcdiagConfig, proc_root: Path
) -> List[SnapshotItem]:
    from .diagnosis.io_disk import IoBottleneckRule

    psi = _read_psi_avg60(proc_root, "io")
    if not psi or "some_avg60" not in psi:
        return [item(INFO, "PSI 非対応のため判定できません")]
    value = psi["some_avg60"]
    status = _level(value, _threshold(config, IoBottleneckRule, "psi_threshold"))
    hint = "ディスクが詰まっています。`iotop -ao` で書き込みの多いプロセスを確認してください"
    return [
        item(status, f"I/O 待ち (PSI 60秒平均) {value:.1f}%", _advice(status, hint))
    ]


@_check("ディスク", "disks", "ディスク")
def _check_disks(item: _Item, config: ProcdiagConfig) -> List[SnapshotItem]:
    from .diagnosis.io_disk import DiskInodeRule
    from .samplers.disk import _PSEUDO_FS_TYPES, _inode_usage, is_read_only

    inode_warn = _threshold(config, DiskInodeRule, "warning_pct")
    inode_crit = _threshold(config, DiskInodeRule, "critical_pct")
    items: List[SnapshotItem] = []
    for part in psutil.disk_partitions(all=False):
        if not part.fstype or part.fstype.lower() in _PSEUDO_FS_TYPES:
            continue
        mp = part.mountpoint
        try:
            usage = psutil.disk_usage(mp)
        except OSError:
            continue
        status = _level(usage.percent, _DISK_WARN_PCT, _DISK_CRIT_PCT)
        hint = (
            f"`du -sh {mp}/* | sort -rh | head` で大きいディレクトリを確認してください"
        )
        items.append(
            item(
                status,
                f"{usage.percent:.0f}% 使用 (空き {usage.free / GiB:.1f} GB)",
                _advice(status, hint),
                name=f"disk:{mp}",
                label=f"{mp} 容量",
            )
        )

        total, used = _inode_usage(mp)
        if total:
            pct = used / total * 100.0
            status = _level(pct, inode_warn, inode_crit)
            hint = (
                "小さなファイルが大量にあります。`df -i` と "
                f"`sudo du --inodes -x {mp} | sort -n | tail` で確認してください"
            )
            items.append(
                item(
                    status,
                    f"{pct:.0f}% 使用",
                    _advice(status, hint),
                    name=f"inode:{mp}",
                    label=f"{mp} inode",
                )
            )

        if is_read_only(part.opts):
            if mp == "/":
                status = CRITICAL
                hint = (
                    "ファイルシステムのエラーで自動的に読み取り専用化した可能性があります。"
                    "`journalctl -k | grep -iE 'EXT4-fs error|I/O error'` を確認してください"
                )
            else:
                status = INFO
                hint = "意図した設定でなければ `journalctl -k` を確認してください"
            items.append(
                item(
                    status,
                    "読み取り専用でマウントされています",
                    hint,
                    name=f"ro:{mp}",
                    label=f"{mp} 読み取り専用",
                )
            )
    return items


@_check("プロセス", "fds", "ファイルハンドル (全体)")
def _check_fds(item: _Item, proc_root: Path) -> List[SnapshotItem]:
    allocated, _unused, maximum = (
        int(v) for v in (proc_root / "sys" / "fs" / "file-nr").read_text().split()[:3]
    )
    if maximum >= _FD_UNLIMITED:
        return [item(OK, f"{allocated:,} 個使用 (上限なし)")]
    pct = allocated / maximum * 100.0 if maximum else 0.0
    status = _level(pct, _FD_WARN_PCT, _FD_CRIT_PCT)
    hint = (
        "`sudo lsof | awk '{print $1}' | sort | uniq -c | sort -rn | head` で"
        "多いプロセスを確認してください"
    )
    return [
        item(status, f"{allocated:,} / {maximum:,} ({pct:.0f}%)", _advice(status, hint))
    ]


@_check("プロセス", "processes", "プロセス")
def _check_processes(item: _Item, procs: List[Dict[str, Any]]) -> List[SnapshotItem]:
    zombies = [p for p in procs if p["status"] == psutil.STATUS_ZOMBIE]
    z_status = WARNING if len(zombies) >= _ZOMBIE_WARN else (INFO if zombies else OK)
    z_hint = (
        "親プロセスが子プロセスを回収していません。`ps -eo pid,ppid,stat,comm | grep ' Z'` で"
        "親 (PPID) を確認してください"
    )

    def fmt(p: Dict[str, Any], value: str) -> str:
        return f"{p['name']}(pid {p['pid']}) {value}"

    by_cpu = sorted(procs, key=lambda p: p["cpu"] or 0.0, reverse=True)[:_TOP_N]
    by_rss = sorted(procs, key=lambda p: p["rss"] or 0, reverse=True)[:_TOP_N]
    return [
        item(
            z_status,
            f"{len(zombies)} 個 (全 {len(procs)} プロセス)",
            _advice(z_status, z_hint),
            name="zombies",
            label="ゾンビプロセス",
        ),
        item(
            INFO,
            " / ".join(fmt(p, f"{p['cpu'] or 0:.0f}%") for p in by_cpu),
            name="top_cpu",
            label="CPU 使用率の上位",
        ),
        item(
            INFO,
            " / ".join(fmt(p, humanize_bytes(p["rss"])) for p in by_rss),
            name="top_rss",
            label="メモリ使用量の上位",
        ),
    ]


@_check("サービス", "services", "サービス")
def _check_services(item: _Item, config: ProcdiagConfig) -> List[SnapshotItem]:
    from .diagnosis.crash import ServiceRestartRule

    if not systemd.available():
        return [item(INFO, "systemd がないため確認できません")]

    failed = systemd.failed_units() or []
    f_status = WARNING if failed else OK
    f_hint = "`systemctl status <unit>` と `journalctl -u <unit> -n 50` で原因を確認してください"

    info = systemd.show_units(systemd.list_service_units() or []) or {}
    counts = {unit: systemd.n_restarts(props) or 0 for unit, props in info.items()}
    restarted = sorted(
        ((unit, n) for unit, n in counts.items() if n > 0), key=lambda x: -x[1]
    )
    # NRestarts counts since the unit was last started by hand; one or two over
    # days is a blip, the rule's warn_restarts is "repeatedly".
    warn_at = _threshold(config, ServiceRestartRule, "warn_restarts")
    worst = restarted[0][1] if restarted else 0
    r_status = WARNING if worst >= warn_at else (INFO if restarted else OK)
    r_hint = (
        "異常終了して systemd が自動再起動しています。"
        "`journalctl -u <unit>` でエラーを確認してください"
    )
    return [
        item(
            f_status,
            ", ".join(failed) if failed else "なし",
            _advice(f_status, f_hint),
            name="failed_units",
            label="failed のユニット",
        ),
        item(
            r_status,
            (
                ", ".join(f"{u} ({n}回)" for u, n in restarted[:5])
                if restarted
                else "なし"
            ),
            _advice(r_status, r_hint),
            name="restarted_units",
            label="自動再起動したサービス",
        ),
    ]


@_check("カーネル", "kernel_log", "カーネルログ (24時間)")
def _check_kernel_log(item: _Item) -> List[SnapshotItem]:
    from .diagnosis.crash import KERNEL_SEVERITY

    events, _ = kernlog.read_events(since=time.time() - _KERNEL_LOOKBACK_SEC)
    if events is None:
        return [
            item(
                INFO, "読み取れません (root または systemd-journal/adm グループが必要)"
            )
        ]
    if not events:
        return [item(OK, "異常なし")]
    counts = Counter(e.kind for e in events)
    # Graded like KERNEL-ERROR-01's critical kinds; anything else is a warning.
    severe = any(KERNEL_SEVERITY.get(k) is Severity.CRITICAL for k in counts)
    detail = ", ".join(f"{kernlog.LABELS.get(k, k)} {n}件" for k, n in counts.items())
    return [
        item(
            CRITICAL if severe else WARNING,
            detail,
            "`journalctl -k --since '24 hours ago' -p warning` で内容を確認してください",
        )
    ]


@_check("温度", "temperature", "CPU 温度")
def _check_temperature(item: _Item, config: ProcdiagConfig) -> List[SnapshotItem]:
    from .diagnosis.thermal import ThermalThrottleRule
    from .samplers.thermal import ThermalSampler

    temp = ThermalSampler._read_temp()
    if temp is None:
        return [item(INFO, "CPU の温度センサがありません")]
    status = _level(temp, _threshold(config, ThermalThrottleRule, "temp_hot_c"))
    cur, mx = ThermalSampler._read_freq()
    detail = f"{temp:.0f}°C"
    if cur and mx:
        detail += f" / クロック {cur:.0f}/{mx:.0f} MHz ({cur / mx * 100:.0f}%)"
    hint = "高温です。ファン・ヒートシンクの埃詰まりや設置環境の温度を確認してください"
    return [item(status, detail, _advice(status, hint))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _sample_processes(sample_sec: float) -> tuple[List[float], List[Dict[str, Any]]]:
    """Measure per-core CPU and per-process CPU% / RSS over *sample_sec*."""
    procs = list(psutil.process_iter(["pid", "name", "status", "memory_info"]))
    for p in procs:
        try:
            p.cpu_percent(None)  # prime the per-process baseline
        except psutil.Error:
            pass
    per_core = psutil.cpu_percent(interval=sample_sec, percpu=True)
    out: List[Dict[str, Any]] = []
    for p in procs:
        try:
            cpu = p.cpu_percent(None)
        except psutil.Error:
            cpu = None
        mem = p.info.get("memory_info")
        out.append(
            {
                "pid": p.info["pid"],
                "name": p.info.get("name") or "?",
                "status": p.info.get("status"),
                "cpu": cpu,
                "rss": mem.rss if mem else 0,
            }
        )
    return per_core, out


def run_snapshot(
    config: ProcdiagConfig,
    *,
    proc_root: Path = Path("/proc"),
    sample_sec: float = 1.0,
) -> List[SnapshotItem]:
    """Run every check and return the items in display order."""
    per_core, procs = _sample_processes(sample_sec)
    items: List[SnapshotItem] = []
    for check in (
        _check_uptime,
        _check_time_sync,
        functools.partial(_check_recording, config),
        functools.partial(_check_cpu, config, per_core, proc_root),
        functools.partial(_check_memory, config, proc_root),
        functools.partial(_check_io, config, proc_root),
        functools.partial(_check_disks, config),
        functools.partial(_check_fds, proc_root),
        functools.partial(_check_processes, procs),
        functools.partial(_check_services, config),
        _check_kernel_log,
        functools.partial(_check_temperature, config),
    ):
        items.extend(check())
    return items


def exit_code(items: List[SnapshotItem]) -> int:
    """1 when anything is warning/critical, else 0 (info never fails)."""
    return 1 if any(i.status in (WARNING, CRITICAL) for i in items) else 0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_SYMBOLS = {
    False: {OK: "✔", WARNING: "⚠", CRITICAL: "✘", INFO: "ℹ"},
    True: {OK: "[OK]", WARNING: "[WARN]", CRITICAL: "[CRIT]", INFO: "[INFO]"},
}
_COLORS = {WARNING: "\033[33m", CRITICAL: "\033[31m"}


def _summary(items: List[SnapshotItem]) -> Dict[str, int]:
    """Number of items per status, worst status first."""
    return {s: sum(1 for i in items if i.status == s) for s in STATUSES}


def render_text(
    items: List[SnapshotItem],
    *,
    hostname: str,
    color: bool,
    ascii_only: bool,
    next_step: str = "長期傾向は procdiag record で記録してから procdiag check",
) -> str:
    """Terminal output: one line per item grouped by category, summary last.

    *next_step* is shown in parentheses after the summary; empty omits it.
    """
    symbols = _SYMBOLS[ascii_only]
    reset = "\033[0m" if color else ""
    now = time.strftime("%Y-%m-%d %H:%M")
    lines = [f"procdiag snapshot — {hostname} ({now})"]
    category = None
    for item in items:
        if item.category != category:
            category = item.category
            lines.append(f"\n[{category}]")
        col = _COLORS.get(item.status, "") if color else ""
        lines.append(
            f"  {col}{symbols[item.status]}{reset} {item.label}: {item.detail}"
        )
        if item.hint:
            lines.append(f"      → {item.hint}")
    n = _summary(items)
    lines.append(
        f"\n結果: 重大 {n[CRITICAL]} 件 / 警告 {n[WARNING]} 件 / 正常 {n[OK]} 件"
        + (f" ({next_step})" if next_step else "")
    )
    return "\n".join(lines)


def render_json(items: List[SnapshotItem], *, hostname: str) -> str:
    """Machine-readable output (stable keys)."""
    return json.dumps(
        {
            "hostname": hostname,
            "generated_at": time.time(),
            "items": [i.to_dict() for i in items],
            "summary": _summary(items),
        },
        ensure_ascii=False,
        indent=2,
    )
