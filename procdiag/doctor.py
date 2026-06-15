"""``procdiag doctor`` — pre-flight check of the host's observability capabilities.

Implements EXT-09 from the procdiag extension proposal.  Before starting a
recording session, ``doctor`` inspects each observable capability and reports
whether it is fully available (``ok``), operating in degraded mode
(``degraded``), or informational (``info``).

The design mirrors §2.1's demotion matrix: every data source that can be absent
or restricted has a corresponding check here so that operators know *before*
running ``record`` what gaps they should expect in the collected data.

Key design choices:

* Each check is a standalone function that accepts injected path roots
  (``proc_root``, ``cgroup_root``).  This makes every check independently
  unit-testable without touching the real filesystem.
* All checks are wrapped in ``try/except`` so that an unexpected failure in
  one check never terminates the doctor run — the failure itself becomes a
  ``degraded`` result.
* No external dependencies beyond psutil and stdlib; the check for
  ``io_counters`` walks real processes so it is bounded by ``_IO_SAMPLE_MAX``.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil

from .config import DEFAULT_CONFIG_PATH, ProcdiagConfig

# Maximum number of processes to sample when probing io_counters read-rate.
_IO_SAMPLE_MAX = 80


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class DoctorCheck:
    """Result of a single observability check."""

    name: str  # "psi", "cgroup_v2", "root", "io_counters", "smaps",
    #            "journal", "sensors", "db_disk"
    label: str  # 日本語表示名
    status: str  # "ok" | "degraded" | "info"
    detail: str  # 日本語。降格時は §2.1 の「降格時の挙動」に相当する影響説明


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_psi(proc_root: Path) -> DoctorCheck:
    """Check whether /proc/pressure/cpu is readable (PSI support)."""
    try:
        (proc_root / "pressure" / "cpu").read_text()
        return DoctorCheck(
            name="psi",
            label="PSI (Pressure Stall Information)",
            status="ok",
            detail="PSI 対応 — スパイクトリガとメモリ圧力監視が有効です",
        )
    except OSError:
        return DoctorCheck(
            name="psi",
            label="PSI (Pressure Stall Information)",
            status="degraded",
            detail=(
                "PSI 非対応 — スパイクトリガは CPU%/メモリ残量の代替条件で動作します"
            ),
        )
    except Exception as exc:
        return DoctorCheck(
            name="psi",
            label="PSI (Pressure Stall Information)",
            status="degraded",
            detail=f"PSI チェック中に予期しない例外が発生しました: {exc}",
        )


def _check_cgroup_v2(cgroup_root: Path) -> DoctorCheck:
    """Check whether cgroup v2 unified tree is present."""
    try:
        if (cgroup_root / "cgroup.controllers").exists():
            return DoctorCheck(
                name="cgroup_v2",
                label="cgroup v2",
                status="ok",
                detail="cgroup v2 統一ツリーが利用可能です — サービス視点の記録が有効です",
            )
        return DoctorCheck(
            name="cgroup_v2",
            label="cgroup v2",
            status="degraded",
            detail=("cgroup v2 なし — サービス視点 (Tier2a) の記録はスキップされます"),
        )
    except Exception as exc:
        return DoctorCheck(
            name="cgroup_v2",
            label="cgroup v2",
            status="degraded",
            detail=f"cgroup v2 チェック中に予期しない例外が発生しました: {exc}",
        )


def _check_root() -> DoctorCheck:
    """Report whether the process is running as root."""
    try:
        if os.geteuid() == 0:
            detail = "root: 全プロセスの I/O・PSS を取得可能"
        else:
            detail = "非 root: 他ユーザープロセスの I/O・PSS は欠損として記録されます"
        return DoctorCheck(
            name="root",
            label="実行権限",
            status="info",
            detail=detail,
        )
    except Exception as exc:
        return DoctorCheck(
            name="root",
            label="実行権限",
            status="info",
            detail=f"権限チェック中に予期しない例外が発生しました: {exc}",
        )


def _check_io_counters() -> DoctorCheck:
    """Sample up to _IO_SAMPLE_MAX processes and measure io_counters read rate."""
    try:
        procs = psutil.process_iter(["pid"])
        total = 0
        success = 0
        for proc in procs:
            if total >= _IO_SAMPLE_MAX:
                break
            total += 1
            try:
                proc.io_counters()
                success += 1
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                # Process disappeared between list and access — neutral.
                pass
            except (psutil.AccessDenied, OSError):
                # Cannot read this process's io counters.
                pass

        if total == 0:
            # No processes found at all — treat as ok (no data to be missing).
            return DoctorCheck(
                name="io_counters",
                label="プロセス I/O カウンタ",
                status="ok",
                detail="走査対象プロセスがありません",
            )

        rate = success / total
        if rate >= 1.0:
            return DoctorCheck(
                name="io_counters",
                label="プロセス I/O カウンタ",
                status="ok",
                detail=f"I/O カウンタ読取率 100% ({success}/{total} プロセス)",
            )
        miss_pct = round((1.0 - rate) * 100)
        return DoctorCheck(
            name="io_counters",
            label="プロセス I/O カウンタ",
            status="degraded",
            detail=(
                f"I/O 欠損率 約 {miss_pct}% — IO-BOTTLENECK-01 の"
                " 確証度が下がります (cgroup 統計で部分代替)"
            ),
        )
    except Exception as exc:
        return DoctorCheck(
            name="io_counters",
            label="プロセス I/O カウンタ",
            status="degraded",
            detail=f"I/O カウンタチェック中に予期しない例外が発生しました: {exc}",
        )


def _check_smaps(proc_root: Path) -> DoctorCheck:
    """Check whether smaps_rollup is readable for the current process."""
    try:
        pid = os.getpid()
        smaps = proc_root / str(pid) / "smaps_rollup"
        smaps.read_text()
        return DoctorCheck(
            name="smaps",
            label="smaps_rollup (PSS)",
            status="ok",
            detail="PSS 精査が利用可能です — メモリリーク確証度昇格が有効です",
        )
    except OSError:
        return DoctorCheck(
            name="smaps",
            label="smaps_rollup (PSS)",
            status="degraded",
            detail=("PSS 精査不可 — メモリリークの確証度昇格が無効になります"),
        )
    except Exception as exc:
        return DoctorCheck(
            name="smaps",
            label="smaps_rollup (PSS)",
            status="degraded",
            detail=f"smaps_rollup チェック中に予期しない例外が発生しました: {exc}",
        )


def _check_journal() -> DoctorCheck:
    """Check whether /var/log/journal is readable."""
    try:
        journal_path = Path("/var/log/journal")
        if journal_path.exists() and os.access(str(journal_path), os.R_OK):
            return DoctorCheck(
                name="journal",
                label="systemd journal",
                status="ok",
                detail="journal 読取可能 — OOM 確証度昇格と DISK-JOURNAL-01 が有効です",
            )
        return DoctorCheck(
            name="journal",
            label="systemd journal",
            status="degraded",
            detail=(
                "journal 読取不可 — DISK-JOURNAL-01 と OOM 確証度昇格が制限されます"
            ),
        )
    except Exception as exc:
        return DoctorCheck(
            name="journal",
            label="systemd journal",
            status="degraded",
            detail=f"journal チェック中に予期しない例外が発生しました: {exc}",
        )


def _check_sensors() -> DoctorCheck:
    """Check whether temperature sensors are available via psutil."""
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            return DoctorCheck(
                name="sensors",
                label="温度センサ",
                status="ok",
                detail="温度センサが利用可能です",
            )
        return DoctorCheck(
            name="sensors",
            label="温度センサ",
            status="info",
            detail=("温度センサなし (将来のハードウェア健全性層は降格動作)"),
        )
    except Exception as exc:
        return DoctorCheck(
            name="sensors",
            label="温度センサ",
            status="info",
            detail=f"温度センサチェック中に予期しない例外が発生しました: {exc}",
        )


def _check_db_disk(config: ProcdiagConfig) -> DoctorCheck:
    """Check free space on the partition that will hold the recording DB."""
    try:
        db_path = Path(config.db_path)
        parent = db_path.parent

        # Ensure the parent directory exists so statvfs can be called.
        parent.mkdir(parents=True, exist_ok=True)

        usage = shutil.disk_usage(str(parent))
        free_bytes = usage.free
        free_gb = free_bytes / (1024**3)

        # Collect existing DB size if present.
        db_size_str = ""
        if db_path.exists():
            db_size_bytes = db_path.stat().st_size
            db_size_mb = db_size_bytes / (1024**2)
            db_size_str = f"  (既存 DB サイズ: {db_size_mb:.1f} MB)"

        if free_gb < 1.0:
            return DoctorCheck(
                name="db_disk",
                label="DB 置き場の空き容量",
                status="degraded",
                detail=(
                    f"記録 DB の置き場が逼迫しています"
                    f" (空き {free_gb:.2f} GB){db_size_str}"
                ),
            )
        return DoctorCheck(
            name="db_disk",
            label="DB 置き場の空き容量",
            status="ok",
            detail=(f"空き {free_gb:.1f} GB — 記録に十分な容量があります{db_size_str}"),
        )
    except Exception as exc:
        return DoctorCheck(
            name="db_disk",
            label="DB 置き場の空き容量",
            status="degraded",
            detail=f"ディスク容量チェック中に予期しない例外が発生しました: {exc}",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _check_config_security(config_path: Optional[str]) -> DoctorCheck:
    """Check that the config file (executed via ``exec``) is not world-writable.

    ``load_config`` runs ``config.py`` with ``exec``, so write access to that
    file is equivalent to code execution under whatever runs ``procdiag`` —
    notably the systemd service.  A world-writable config is therefore a
    privilege-escalation risk worth surfacing before a recording starts.

    The check is advisory: an unexpected error (e.g. an unreadable path) is
    reported as ``info`` rather than failing the doctor run, matching how the
    other checks treat unexpected exceptions.
    """
    try:
        path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        if not path.exists():
            return DoctorCheck(
                name="config_security",
                label="設定ファイルの安全性",
                status="info",
                detail=f"設定ファイルが見つかりません ({path}) — 既定値で動作します",
            )
        if path.stat().st_mode & 0o002:
            return DoctorCheck(
                name="config_security",
                label="設定ファイルの安全性",
                status="degraded",
                detail=(
                    f"{path} が誰でも書き込み可能 (world-writable) です。"
                    "config.py は exec() で実行されるため任意コード実行のリスクがあります"
                    " — chmod o-w で権限を絞ってください"
                ),
            )
        return DoctorCheck(
            name="config_security",
            label="設定ファイルの安全性",
            status="ok",
            detail=f"{path} の権限は安全です (world-writable ではありません)",
        )
    except Exception as exc:
        return DoctorCheck(
            name="config_security",
            label="設定ファイルの安全性",
            status="info",
            detail=f"設定ファイル権限チェック中に予期しない例外が発生しました: {exc}",
        )


def run_doctor(
    config: ProcdiagConfig,
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    config_path: Optional[str] = None,
) -> list[DoctorCheck]:
    """Run all observability checks and return a list of :class:`DoctorCheck`.

    Always returns exactly 9 items in a fixed order regardless of errors.
    Each check is wrapped so a single failure cannot propagate.

    Parameters
    ----------
    config:
        Active procdiag configuration.  Used for ``db_path`` (disk check).
    proc_root:
        Override for ``/proc``; injected by tests.
    cgroup_root:
        Override for ``/sys/fs/cgroup``; injected by tests.
    config_path:
        Path to the config file to permission-check; falls back to the default
        ``./config.py`` when ``None``.
    """
    return [
        _check_psi(proc_root),
        _check_cgroup_v2(cgroup_root),
        _check_root(),
        _check_io_counters(),
        _check_smaps(proc_root),
        _check_journal(),
        _check_sensors(),
        _check_db_disk(config),
        _check_config_security(config_path),
    ]
