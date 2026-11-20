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
* Each check body only returns ``(status, detail)``; the ``@_doctor_check``
  decorator attaches its name/label and turns an unexpected exception into a
  result, so one failing check never terminates the doctor run.
* No external dependencies beyond psutil and stdlib.
"""

from __future__ import annotations

import functools
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

import psutil

from . import kernlog
from .config import DEFAULT_CONFIG_PATH, ProcdiagConfig, config_trust_problem
from .core.util import GiB, MiB
from .mounts import unmounted_disk
from .output import read_only_filesystem
from .samplers.thermal import ThermalSampler

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class DoctorCheck:
    """Result of a single observability check."""

    name: str  # "psi", "cgroup_v2", "root", "io_counters", "smaps",
    #            "journal", "sensors", "db_disk", "config_security", "budget"
    label: str  # 日本語表示名
    status: str  # "ok" | "degraded" | "info"
    detail: str  # 日本語。降格時は §2.1 の「降格時の挙動」に相当する影響説明

    def to_dict(self) -> dict[str, str]:
        """The ``doctor --format json`` entry (also written by ``bundle``)."""
        return asdict(self)


# (status, detail) as returned by a check body.
_Outcome = tuple[str, str]

# The checks of the config file, not of what the host can observe: when only
# these are degraded the recording is not missing data.
SETTING_CHECKS = frozenset({"config_security", "budget"})


def _doctor_check(
    name: str, label: str, *, what: str, on_error: str = "degraded"
) -> Callable[[Callable[..., _Outcome]], Callable[..., DoctorCheck]]:
    """Wrap a check body into a function that always returns a DoctorCheck.

    *what* names the check in the message used when the body raises
    unexpectedly; that case is reported with status *on_error* rather than
    propagated.
    """

    def decorate(body: Callable[..., _Outcome]) -> Callable[..., DoctorCheck]:
        @functools.wraps(body)
        def run(*args, **kwargs) -> DoctorCheck:
            try:
                status, detail = body(*args, **kwargs)
            except Exception as exc:
                status = on_error
                detail = f"{what}チェック中に予期しない例外が発生しました: {exc}"
            return DoctorCheck(name=name, label=label, status=status, detail=detail)

        return run

    return decorate


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


@_doctor_check("psi", "PSI (Pressure Stall Information)", what="PSI ")
def _check_psi(proc_root: Path) -> _Outcome:
    """Check whether /proc/pressure/cpu is readable (PSI support)."""
    try:
        (proc_root / "pressure" / "cpu").read_text()
    except OSError:
        return (
            "degraded",
            "PSI 非対応 — スパイクトリガは CPU%/メモリ残量の代替条件で動作します",
        )
    return "ok", "PSI 対応 — スパイクトリガとメモリ圧力監視が有効です"


@_doctor_check("cgroup_v2", "cgroup v2", what="cgroup v2 ")
def _check_cgroup_v2(cgroup_root: Path) -> _Outcome:
    """Check whether cgroup v2 unified tree is present."""
    if (cgroup_root / "cgroup.controllers").exists():
        return (
            "ok",
            "cgroup v2 統一ツリーが利用可能です — サービス視点の記録が有効です",
        )
    return "degraded", "cgroup v2 なし — サービス視点 (Tier2a) の記録はスキップされます"


@_doctor_check("root", "実行権限", what="権限", on_error="info")
def _check_root() -> _Outcome:
    """Report whether the process is running as root."""
    if os.geteuid() == 0:
        return "info", "root: 全プロセスの I/O・PSS を取得可能"
    return "info", "非 root: 他ユーザープロセスの I/O・PSS は欠損として記録されます"


@_doctor_check("io_counters", "プロセス I/O カウンタ", what="I/O カウンタ")
def _check_io_counters() -> _Outcome:
    """Measure the io_counters read rate over every process."""
    total = 0
    success = 0
    for proc in psutil.process_iter(["pid"]):
        total += 1
        try:
            proc.io_counters()
            success += 1
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass  # disappeared between list and access — neutral
        except (psutil.AccessDenied, OSError):
            pass  # cannot read this process's io counters

    if total == 0:
        # No processes found at all — treat as ok (no data to be missing).
        return "ok", "走査対象プロセスがありません"

    rate = success / total
    if rate >= 1.0:
        return "ok", f"I/O カウンタ読取率 100% ({success}/{total} プロセス)"
    miss_pct = round((1.0 - rate) * 100)
    return (
        "degraded",
        f"I/O 欠損率 約 {miss_pct}% (読めないプロセス {total - success}/{total})"
        " — IO-BOTTLENECK-01 の"
        " 確証度が下がります (cgroup 統計で部分代替)",
    )


@_doctor_check("smaps", "smaps_rollup (PSS)", what="smaps_rollup ")
def _check_smaps(proc_root: Path) -> _Outcome:
    """Check whether smaps_rollup is readable for the current process."""
    try:
        (proc_root / str(os.getpid()) / "smaps_rollup").read_text()
    except OSError:
        return "degraded", "PSS 精査不可 — メモリリークの確証度昇格が無効になります"
    return "ok", "PSS 精査が利用可能です — メモリリーク確証度昇格が有効です"


@_doctor_check("journal", "systemd journal", what="journal ")
def _check_journal() -> _Outcome:
    """Check whether kernel messages can be read from the journal."""
    if kernlog.journal_readable():
        return (
            "ok",
            "カーネルログを読めます — KERNEL-ERROR-01 の記録と"
            " OOM-KILL-01 の確証度昇格が有効です",
        )
    return (
        "degraded",
        "カーネルログを読めません (root または systemd-journal/adm グループが必要)"
        " — KERNEL-ERROR-01 は記録されず、OOM-KILL-01 の確証度昇格も無効になります",
    )


@_doctor_check("sensors", "温度センサ", what="温度センサ", on_error="info")
def _check_sensors() -> _Outcome:
    """Check whether a CPU temperature can be read, as the thermal sampler reads it.

    A host with only drive, GPU or Wi-Fi sensors has temperatures, but not the
    CPU's, and records none.
    """
    if ThermalSampler._read_temp() is not None:
        return "ok", "温度センサが利用可能です"
    return (
        "info",
        "CPU の温度センサなし — THERMAL-01 (サーマルスロットリング検出) は無効になります",
    )


def _db_place(config: ProcdiagConfig) -> _Outcome:
    """Judge the folder of ``db_path`` the way ``record`` will meet it."""
    db_path = Path(os.path.abspath(config.db_path))
    fix = (
        "このままでは record は記録できません。"
        "書き込める場所を --db か config.py の db_path で指定してください"
    )
    if os.path.isdir(db_path):
        return (
            "degraded",
            f"{db_path} はディレクトリです — DB ファイルのパスを指定してください",
        )
    disk = unmounted_disk(db_path)
    if disk is not None:
        return (
            "degraded",
            f"{disk} のディスクがまだマウントされていません (/etc/fstab にあるマウントポイント)"
            " — マウントされるまで record は記録を始めません (終了コード 1。"
            "systemd のユニットは再起動して待ちます)",
        )
    if os.path.islink(db_path) and not os.path.exists(db_path):
        # SQLite creates the file where the link points, in a folder that must exist.
        target = Path(os.path.realpath(db_path))
        if not target.parent.is_dir():
            return (
                "degraded",
                f"{db_path} は {target} を指すリンクですが、そのフォルダがありません — {fix}",
            )
        folder = target.parent
    else:
        folder = db_path.parent
        while not os.path.lexists(folder):
            folder = folder.parent
        if not folder.is_dir():
            return (
                "degraded",
                f"{folder} はフォルダではありません (切れたリンクなど) — {fix}",
            )

    if not os.access(folder, os.W_OK | os.X_OK):
        if read_only_filesystem(folder):
            return (
                "degraded",
                f"{folder} は読み取り専用のファイルシステムにあります — {fix}",
            )
        return "degraded", f"{folder} に書き込めません — {fix}"
    if os.path.exists(db_path) and not os.access(db_path, os.R_OK | os.W_OK):
        return "degraded", f"{db_path} に書き込めません — {fix}"

    free = shutil.disk_usage(folder).free
    min_free_mb = config.retention.min_free_disk_mb

    db_size_str = ""
    if os.path.exists(db_path):
        db_size_str = f"  (既存 DB サイズ: {db_path.stat().st_size / MiB:.1f} MB)"

    if free < min_free_mb * MiB:
        return (
            "degraded",
            f"記録 DB の置き場が逼迫しています (空き {free / GiB:.2f} GB、最低空き容量"
            f" {min_free_mb:.0f} MB 未満) — record は保持期間より早く古い記録を"
            f"削除します{db_size_str}",
        )
    return (
        "ok",
        f"空き {free / GiB:.1f} GB — 記録できます (空きが {min_free_mb:.0f} MB を"
        f"下回ると古い記録から削除します){db_size_str}",
    )


@_doctor_check("db_disk", "記録 DB の置き場", what="DB 置き場")
def _check_db_disk(config: ProcdiagConfig) -> _Outcome:
    """Check that ``record`` can write the DB and has room for it.

    Also warns when the DB is not where the config file says, but where an
    older procdiag kept it (:meth:`ProcdiagConfig.db_path_notice`).  That notice
    comes first and the folder is judged all the same: it is the folder
    ``record`` will write to.
    """
    status, detail = _db_place(config)
    notice = config.db_path_notice()
    if notice:
        return "degraded", f"{notice}。{detail}"
    return status, detail


@_doctor_check(
    "config_security", "設定ファイルの安全性", what="設定ファイル権限", on_error="info"
)
def _check_config_security(config_path: Optional[str]) -> _Outcome:
    """Check that no other user could change what the config file runs.

    ``load_config`` runs ``config.py`` with ``exec``, so write access to that
    file, or to the folder it sits in, is equivalent to code execution under
    whatever runs ``procdiag`` — notably the systemd service.  The rule is the
    one ``load_config`` applies to the default ``./config.py``
    (:func:`~procdiag.config.config_trust_problem`); an explicit ``--config``
    is still executed, but doctor warns about it here.

    The check is advisory: an unexpected error (e.g. an unreadable path) is
    reported as ``info`` rather than failing the doctor run, matching how the
    other checks treat unexpected exceptions.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        return "info", f"設定ファイルが見つかりません ({path}) — 既定値で動作します"
    problem = config_trust_problem(path)
    if problem:
        return (
            "degraded",
            f"{problem}。config.py は exec() で実行されるため、"
            "他のユーザーが書き換えられると任意コード実行のリスクがあります",
        )
    return (
        "ok",
        f"{path} は自分 (または root) の所有で、他のユーザーは書き換えられません",
    )


@_doctor_check("budget", "自動減速の設定", what="自動減速の設定")
def _check_budget(config: ProcdiagConfig) -> _Outcome:
    """Check that ``budget.throttle_factor`` is used as the config file says.

    A factor below the minimum is raised to it, not refused (an unattended
    recording must still start), so this is where the user is told.
    """
    notice = config.budget.throttle_notice()
    if notice:
        return "degraded", notice
    factor = config.budget.throttle_factor
    return (
        "ok",
        f"throttle_factor = {factor:g} — 自己負荷が予算を超えている間、"
        f"Tier-2b の間隔を 1 分ごとに {factor:g} 倍ずつ広げます",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_doctor(
    config: ProcdiagConfig,
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    config_path: Optional[str] = None,
) -> list[DoctorCheck]:
    """Run all observability checks and return a list of :class:`DoctorCheck`.

    Always returns exactly 10 items in a fixed order regardless of errors.
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
        _check_budget(config),
    ]
