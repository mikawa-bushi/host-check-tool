"""Pydantic configuration models for procdiag.

All configuration lives in a single ``config.py`` file (``ProcdiagConfig``).
Pydantic v2 validates every field at construction time; invalid values
produce Japanese error messages to match the operator-friendly convention
of the rest of the tool.

The ``load_config`` function reads a user-supplied ``config.py`` by executing
it in a fresh namespace and extracting the ``config`` variable — the same
approach as netdiag's scenario files.  If the file is absent, a
``ProcdiagConfig()`` with all defaults is returned.
"""

from __future__ import annotations

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


class WatchDir(BaseModel):
    """One entry in the ``watch_dirs`` list."""

    model_config = ConfigDict(extra="forbid")

    path: str
    time_budget_sec: float = 10.0

    @field_validator("time_budget_sec")
    @classmethod
    def _positive_budget(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"time_budget_sec は正の数でなければなりません: {v}")
        return v


class SamplingConfig(BaseModel):
    """Tier-interval and watch-dir settings."""

    model_config = ConfigDict(extra="forbid")

    system_interval: float = 1.0  # T1 — seconds
    cgroup_interval: float = 5.0  # T2a
    process_interval: float = 10.0  # T2b
    disk_interval: float = 60.0  # T3 disk_usage
    dir_scan_interval: float = 3600.0  # T3 watch_dirs
    thermal_interval: float = 10.0  # T3 温度 / cpufreq
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


class TriggerConfig(BaseModel):
    """Spike-trigger thresholds and debounce settings."""

    model_config = ConfigDict(extra="forbid")

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


class BudgetConfig(BaseModel):
    """Self-throttling overhead budget."""

    model_config = ConfigDict(extra="forbid")

    overhead_budget_pct: float = 1.0
    throttle_factor: float = 1.5
    throttle_max_interval: float = 60.0

    @field_validator("overhead_budget_pct", "throttle_factor", "throttle_max_interval")
    @classmethod
    def _positive_float(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"値は正の数でなければなりません: {v}")
        return v


class RetentionConfig(BaseModel):
    """Data-retention policy."""

    model_config = ConfigDict(extra="forbid")

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


class RulesConfig(BaseModel):
    """Rule-engine settings."""

    model_config = ConfigDict(extra="forbid")

    disabled: list[str] = Field(default_factory=list)
    extra_rules: list[str] = Field(default_factory=list)  # importlib paths
    min_observation_hours: float = 6.0
    journal_size_warn_gb: float = 4.0

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


class ReportConfig(BaseModel):
    """Report-generation settings."""

    model_config = ConfigDict(extra="forbid")

    lang: str = "ja"
    hostname_label: Optional[str] = None  # display name for the report title


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------


class ProcdiagConfig(BaseModel):
    """Root configuration for procdiag.

    Instantiate with no arguments to use all defaults.  Pass keyword
    arguments or load from a ``config.py`` file with :func:`load_config`.
    """

    model_config = ConfigDict(extra="forbid")

    db_path: str = "./procdiag.db"
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)


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
    # 記録先DB(絶対パスを推奨)
    db_path="./procdiag.db",

    sampling=SamplingConfig(
        system_interval=1.0,          # Tier1: システム全体 + PSI (秒)
        cgroup_interval=5.0,          # Tier2a: cgroup統計 (秒)
        process_interval=10.0,        # Tier2b: プロセス走査 (秒)
        disk_interval=60.0,           # Tier3: ディスク使用量 (秒)
        dir_scan_interval=3600.0,     # Tier3: ディレクトリサイズ走査 (秒)
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
    ),

    report=ReportConfig(
        lang="ja",
        hostname_label=None,        # レポート表題用ラベル 例: "L3ライン制御PC"
    ),
)
"""

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_config(path: Optional[Path] = None) -> ProcdiagConfig:
    """Load a ``ProcdiagConfig`` from *path*.

    Resolution order:

    1. If *path* is given explicitly, read that file.
    2. If *path* is ``None`` and ``./config.py`` exists, read it.
    3. Otherwise return a ``ProcdiagConfig()`` with all defaults.

    The file is executed in a fresh namespace; the ``config`` variable in
    that namespace is returned.  This is the same ``exec``-based loading used
    by netdiag for scenario files — it lets operators use full Python
    expressions in the config without a custom parser.

    Raises :class:`ValueError` with a Japanese message on any loading error.
    """
    if path is None:
        candidate = DEFAULT_CONFIG_PATH
        if not candidate.exists():
            return ProcdiagConfig()
        path = candidate

    if not Path(path).exists():
        raise ValueError(f"設定ファイルが見つかりません: {path}")

    namespace: dict[str, Any] = {}
    try:
        source = Path(path).read_text(encoding="utf-8")
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    except Exception as e:
        raise ValueError(f"設定ファイルの読み込みに失敗しました: {path}\n{e}") from e

    if "config" not in namespace:
        raise ValueError(f"設定ファイルに 'config' 変数が定義されていません: {path}")

    cfg = namespace["config"]
    if not isinstance(cfg, ProcdiagConfig):
        raise ValueError(
            f"'config' 変数が ProcdiagConfig のインスタンスではありません: {type(cfg)}"
        )
    return cfg
