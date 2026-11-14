"""Pydantic configuration models for procdiag.

All configuration lives in a single ``config.py`` file (``ProcdiagConfig``).
Pydantic v2 validates every field at construction time; invalid values
produce Japanese error messages to match the operator-friendly convention
of the rest of the tool.

The ``load_config`` function reads a user-supplied ``config.py`` by executing
it in a fresh namespace and extracting the ``config`` variable — the same
approach as netdiag's scenario files.  If the file is absent, a
``ProcdiagConfig()`` with all defaults is returned.  Without an explicit path
the file is ``./config.py``, which is only executed when no other user could
have written it (see :func:`config_trust_problem`).
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .core.util import parse_duration

# ---------------------------------------------------------------------------
# Default path for the configuration file
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("./config.py")

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class _Model(BaseModel):
    """Base of every config model: unknown keys and NaN / inf are errors.

    NaN passes ``v <= 0`` and inf is "positive", so the per-field range checks
    alone let ``system_interval=nan`` through, which makes the recorder spin.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _finite_numbers(self) -> "_Model":
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{name} は有限の数でなければなりません: {value}")
        return self


class WatchDir(_Model):
    """One entry in the ``watch_dirs`` list."""

    path: str
    time_budget_sec: float = 10.0

    @field_validator("time_budget_sec")
    @classmethod
    def _positive_budget(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"time_budget_sec は正の数でなければなりません: {v}")
        return v


class SamplingConfig(_Model):
    """Tier-interval and watch-dir settings."""

    system_interval: float = 1.0  # T1 — seconds
    cgroup_interval: float = 5.0  # T2a
    process_interval: float = 10.0  # T2b
    disk_interval: float = 60.0  # T3 disk_usage
    dir_scan_interval: float = 3600.0  # T3 watch_dirs
    thermal_interval: float = 10.0  # T3 温度 / cpufreq
    service_interval: float = 300.0  # T3 systemd 自動再起動 / failed
    kernel_log_interval: float = 300.0  # T3 カーネルログの異常
    watch_dirs: list[WatchDir] = Field(default_factory=list)
    cmdline_normalize: bool = True
    # PSS pre-check interval (seconds). 0 disables the job.
    pss_precheck_interval: float = 3600.0

    @field_validator(
        "system_interval",
        "cgroup_interval",
        "process_interval",
        "disk_interval",
        "dir_scan_interval",
        "thermal_interval",
        "service_interval",
        "kernel_log_interval",
    )
    @classmethod
    def _positive_interval(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"サンプリング間隔は正の数でなければなりません: {v}")
        return v

    @field_validator("pss_precheck_interval")
    @classmethod
    def _non_negative_pss_interval(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"pss_precheck_interval は0以上でなければなりません: {v}")
        return v


class TriggerConfig(_Model):
    """Spike-trigger thresholds and debounce settings."""

    psi_cpu_some10: float = 20.0
    psi_mem_some10: float = 5.0
    psi_mem_full10: float = 1.0
    psi_io_some10: float = 30.0
    spike_cooldown_sec: int = 30
    spike_storm_threshold: int = 10  # fires / 5 minutes before storm mode

    @field_validator(
        "psi_cpu_some10", "psi_mem_some10", "psi_mem_full10", "psi_io_some10"
    )
    @classmethod
    def _non_negative_psi(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"PSI閾値は0以上でなければなりません: {v}")
        return v

    @field_validator("spike_cooldown_sec", "spike_storm_threshold")
    @classmethod
    def _positive_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"値は正の整数でなければなりません: {v}")
        return v


class BudgetConfig(_Model):
    """Self-throttling overhead budget."""

    overhead_budget_pct: float = 1.0
    throttle_factor: float = 1.5
    throttle_max_interval: float = 60.0
    # Seconds after start excluded from the overhead average (start-up cost).
    warmup_sec: float = 60.0

    @field_validator("overhead_budget_pct", "throttle_factor", "throttle_max_interval")
    @classmethod
    def _positive_float(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"値は正の数でなければなりません: {v}")
        return v

    @field_validator("warmup_sec")
    @classmethod
    def _non_negative_warmup(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"warmup_sec は0以上でなければなりません: {v}")
        return v


class RetentionConfig(_Model):
    """Data-retention policy."""

    raw: str = "48h"
    rollup: str = "90d"
    # DB size guard. None disables the size check.
    db_max_size_mb: Optional[float] = 2048.0
    # Minimum free disk space before the guard triggers (MB).
    min_free_disk_mb: float = 512.0

    @field_validator("raw", "rollup")
    @classmethod
    def _valid_duration(cls, v: str) -> str:
        try:
            parse_duration(v)
        except ValueError as e:
            raise ValueError(f"保持期間の形式が不正です: {e}") from e
        return v

    @field_validator("db_max_size_mb")
    @classmethod
    def _positive_or_none_max_size(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError(f"db_max_size_mb は正の数でなければなりません: {v}")
        return v

    @field_validator("min_free_disk_mb")
    @classmethod
    def _positive_min_free(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"min_free_disk_mb は正の数でなければなりません: {v}")
        return v


class RulesConfig(_Model):
    """Rule-engine settings."""

    disabled: list[str] = Field(default_factory=list)
    extra_rules: list[str] = Field(default_factory=list)  # importlib paths
    min_observation_hours: float = 6.0
    journal_size_warn_gb: float = 4.0
    # Per-rule threshold overrides: {"CPU-SATURATION-01": {"psi_threshold": 30}}.
    # Keys are checked against each rule's tunables at start-up.
    overrides: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _no_nan_threshold(cls, v: dict[str, dict[str, float]]) -> dict:
        # inf stays legal: an unbounded threshold (CPU-PERIODIC-01 tol_sec) is
        # meaningful, while NaN compares false both ways and silently disables
        # the rule.
        for rule_id, values in v.items():
            for name, number in values.items():
                if math.isnan(number):
                    raise ValueError(
                        f"overrides の {rule_id} の {name} が NaN です。"
                        "しきい値には数を書いてください"
                    )
        return v

    @field_validator("min_observation_hours")
    @classmethod
    def _non_negative_hours(cls, v: float) -> float:
        # 0 = no minimum: instantaneous rules (and tests) may waive the guard.
        if v < 0:
            raise ValueError(f"min_observation_hours は0以上でなければなりません: {v}")
        return v

    @field_validator("journal_size_warn_gb")
    @classmethod
    def _positive_float(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"値は正の数でなければなりません: {v}")
        return v


class ReportConfig(_Model):
    """Report-generation settings."""

    lang: str = "ja"
    hostname_label: Optional[str] = None  # display name for the report title


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------


class ProcdiagConfig(_Model):
    """Root configuration for procdiag.

    Instantiate with no arguments to use all defaults.  Pass keyword
    arguments or load from a ``config.py`` file with :func:`load_config`.
    """

    db_path: str = "./procdiag.db"
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)

    @field_validator("db_path")
    @classmethod
    def _expand_home(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("db_path が空です。DB ファイルのパスを指定してください")
        return os.path.expanduser(v)


# ---------------------------------------------------------------------------
# Config template
# ---------------------------------------------------------------------------

CONFIG_TEMPLATE: str = """\
# procdiag 設定ファイル
# 変更したい項目だけ上書きしてください。それ以外は既定値が使われます。

from procdiag.config import (
    ProcdiagConfig,
    SamplingConfig,
    WatchDir,
    TriggerConfig,
    BudgetConfig,
    RetentionConfig,
    RulesConfig,
    ReportConfig,
)

config = ProcdiagConfig(
    # 記録先DB。相対パスはこの config.py のあるフォルダからの位置 (~ はホームフォルダ)
    db_path="./data/procdiag.db",

    sampling=SamplingConfig(
        system_interval=1.0,          # Tier1: システム全体 + PSI (秒)
        cgroup_interval=5.0,          # Tier2a: cgroup統計 (秒)
        process_interval=10.0,        # Tier2b: プロセス走査 (秒)
        disk_interval=60.0,           # Tier3: ディスク使用量 (秒)
        dir_scan_interval=3600.0,     # Tier3: ディレクトリサイズ走査 (秒)
        thermal_interval=10.0,        # Tier3: CPU温度・クロック (秒)
        service_interval=300.0,       # Tier3: systemd サービスの自動再起動・failed (秒)
        kernel_log_interval=300.0,    # Tier3: カーネルログの異常 (秒)
        watch_dirs=[
            # WatchDir(path="/var/log", time_budget_sec=10),
            # WatchDir(path="/opt/myapp/data"),
        ],
        cmdline_normalize=True,       # 数字引数をプレースホルダに置換してプロセス台帳爆発を防ぐ
        pss_precheck_interval=3600.0, # PSS精査候補の事前チェック間隔 (秒)。0で無効
    ),

    trigger=TriggerConfig(
        psi_cpu_some10=20.0,       # CPU PSI閾値 (%)
        psi_mem_some10=5.0,        # メモリ PSI閾値 (%)
        psi_mem_full10=1.0,        # メモリ full PSI閾値 (%)
        psi_io_some10=30.0,        # I/O PSI閾値 (%)
        spike_cooldown_sec=30,     # スパイク再発火の最短間隔 (秒)
        spike_storm_threshold=10,  # ストーム判定: 回/5分
    ),

    budget=BudgetConfig(
        overhead_budget_pct=1.0,   # 自己CPUオーバーヘッド上限 (%)
        throttle_factor=1.5,       # 超過時のTier2b間隔拡大係数
        throttle_max_interval=60.0, # Tier2b間隔の上限 (秒)
        warmup_sec=60.0,           # 起動直後この秒数は自己負荷の判定から除外
    ),

    retention=RetentionConfig(
        raw="48h",             # 生サンプルの保持期間
        rollup="90d",          # 1分ロールアップの保持期間
        db_max_size_mb=2048.0, # DB最大サイズ (MB)。None で無効
        min_free_disk_mb=512.0, # 最低空きディスク容量 (MB)
    ),

    rules=RulesConfig(
        disabled=[],                # 無効化するルールID例: ["ZOMBIE-01"]
        extra_rules=[],             # ユーザー定義ルールのimportパス
        min_observation_hours=6.0,  # ルール発火に必要な最低観測時間
        journal_size_warn_gb=4.0,   # journald警告閾値 (GB)
        # ルールごとのしきい値上書き。調整可能な項目は procdiag explain <RULE-ID> で確認
        overrides={
            # "CPU-SATURATION-01": {"psi_threshold": 30.0},
            # "DISK-FULL-01": {"warning_days": 14},
        },
    ),

    report=ReportConfig(
        lang="ja",
        hostname_label=None,        # レポート表題用ラベル 例: "ライン制御PC"
    ),
)
"""

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def config_trust_problem(path: Path) -> Optional[str]:
    """Why another user could change what *path* does, or ``None`` when none can.

    ``load_config`` executes the file, so whoever can rewrite it, or swap it
    for another file, runs code as the user running procdiag.  That is the
    file's owner unless it is the current user or root, anyone when the file
    is world-writable, and anyone when its folder is world-writable without
    the sticky bit (or with a sticky bit but another user's ownership).
    """
    me = os.geteuid()
    info = path.stat()
    if info.st_uid not in (me, 0):
        return (
            f"{path} は他のユーザー (uid {info.st_uid}) の所有です。"
            "自分の所有にするか (chown)、別の場所に置いてください"
        )
    if info.st_mode & 0o002:
        return (
            f"{path} が誰でも書き込み可能 (world-writable) です。"
            "chmod o-w で権限を絞ってください"
        )
    folders = {Path(os.path.abspath(path)).parent, path.resolve().parent}
    for folder in sorted(folders):
        info = folder.stat()
        sticky_and_trusted = info.st_mode & 0o1000 and info.st_uid in (me, 0)
        if info.st_mode & 0o002 and not sticky_and_trusted:
            return (
                f"{path} のあるフォルダ {folder} は誰でも書き込めるため、"
                "他のユーザーがファイルを差し替えられます "
                "(sticky ビットが無い、または所有者が自分・root 以外です)。"
                "chmod +t で sticky ビットを付けるか、別のフォルダに置いてください"
            )
    return None


def load_config(path: Optional[Path] = None) -> ProcdiagConfig:
    """Load a ``ProcdiagConfig`` from *path*.

    Resolution order:

    1. If *path* is given explicitly, read that file.
    2. If *path* is ``None`` and ``./config.py`` exists, read it, unless
       :func:`config_trust_problem` says another user could have written it.
    3. Otherwise return a ``ProcdiagConfig()`` with all defaults.

    The file is executed in a fresh namespace; the ``config`` variable in
    that namespace is returned.  This is the same ``exec``-based loading used
    by netdiag for scenario files — it lets operators use full Python
    expressions in the config without a custom parser.

    A relative ``db_path`` read from a file is relative to that file's folder,
    so one config names one DB wherever procdiag runs; with no file it stays
    relative to the current folder.

    Raises :class:`ValueError` with a Japanese message on any loading error.
    """
    implicit = path is None
    if path is None:
        candidate = DEFAULT_CONFIG_PATH
        if not candidate.exists():
            return ProcdiagConfig()
        path = candidate

    if not Path(path).exists():
        raise ValueError(f"設定ファイルが見つかりません: {path}")

    if implicit:
        try:
            problem = config_trust_problem(Path(path))
        except OSError as e:
            raise ValueError(
                f"設定ファイルの所有者を確認できません: {path} ({e})"
            ) from e
        if problem:
            raise ValueError(
                f"カレントフォルダの {path} は実行しません。{problem}\n"
                f"このファイルを使うときは --config {path} と指定してください"
            )

    hint = (
        "\n(--config を省いたので、カレントフォルダの config.py を読みました。"
        "別のファイルは --config で指定してください)"
        if implicit
        else ""
    )
    namespace: dict[str, Any] = {}
    try:
        # Bytes: Python then honours a BOM or a "coding:" line, as for any .py.
        source = Path(path).read_bytes()
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    except Exception as e:
        raise ValueError(
            f"設定ファイルの読み込みに失敗しました: {path}\n{e}{hint}"
        ) from e

    if "config" not in namespace:
        raise ValueError(
            f"設定ファイルに 'config' 変数が定義されていません: {path}{hint}"
        )

    cfg = namespace["config"]
    if not isinstance(cfg, ProcdiagConfig):
        raise ValueError(
            "'config' 変数が ProcdiagConfig のインスタンスではありません: "
            f"{type(cfg)}{hint}"
        )
    if not Path(cfg.db_path).is_absolute():
        db = Path(os.path.abspath(path)).parent / cfg.db_path
        cfg = cfg.model_copy(update={"db_path": str(db)})
    return cfg
