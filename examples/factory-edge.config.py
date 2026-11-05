# procdiag 設定例 — 工場エッジPC / ラインサーバー向け
#
# 使い方: このファイルを記録対象マシンに config.py として置き、
#   procdiag record --config config.py
# 変更したい項目だけ上書きしてください。それ以外は既定値が使われます。

from procdiag.config import (
    BudgetConfig,
    ProcdiagConfig,
    ReportConfig,
    RetentionConfig,
    RulesConfig,
    SamplingConfig,
    TriggerConfig,
    WatchDir,
)

config = ProcdiagConfig(
    # 記録先DB(絶対パスを推奨。SDカード摩耗を避けるなら HDD/SSD 側へ)
    db_path="/var/lib/procdiag/procdiag.db",
    sampling=SamplingConfig(
        system_interval=1.0,  # Tier1: システム全体 + PSI (秒)
        cgroup_interval=5.0,  # Tier2a: cgroup統計 (秒)
        process_interval=10.0,  # Tier2b: プロセス走査 (秒)
        disk_interval=60.0,  # Tier3: ディスク使用量 (秒)
        dir_scan_interval=3600.0,  # Tier3: ディレクトリサイズ走査 (秒)
        watch_dirs=[
            WatchDir(path="/var/log", time_budget_sec=10),
            WatchDir(path="/var/log/journal"),  # DISK-JOURNAL-01 用
            WatchDir(path="/opt/myapp/data"),  # 業務アプリのデータ置き場
        ],
        cmdline_normalize=True,  # 数字引数をプレースホルダに置換してプロセス台帳爆発を防ぐ
    ),
    trigger=TriggerConfig(
        psi_cpu_some10=20.0,  # CPU PSI閾値 (%)
        psi_mem_some10=5.0,  # メモリ PSI閾値 (%)
        psi_mem_full10=1.0,  # メモリ full PSI閾値 (%)
        psi_io_some10=30.0,  # I/O PSI閾値 (%)
        spike_cooldown_sec=30,  # スパイク再発火の最短間隔 (秒)
        spike_storm_threshold=10,  # ストーム判定: 回/5分
    ),
    budget=BudgetConfig(
        overhead_budget_pct=1.0,  # 自己CPUオーバーヘッド上限 (%)
        throttle_factor=1.5,  # 超過時のTier2b間隔拡大係数
        throttle_max_interval=60.0,  # Tier2b間隔の上限 (秒)
    ),
    retention=RetentionConfig(
        raw="48h",  # 生サンプルの保持期間
        rollup="90d",  # 1分ロールアップの保持期間
    ),
    rules=RulesConfig(
        disabled=[],  # 無効化するルールID例: ["ZOMBIE-01"]
        extra_rules=[],  # ユーザー定義ルールのimportパス
        min_observation_hours=6.0,  # ルール発火に必要な最低観測時間
        journal_size_warn_gb=4.0,  # journald警告閾値 (GB)
    ),
    report=ReportConfig(
        lang="ja",
        hostname_label="ライン制御PC",  # レポート表題用ラベル
    ),
)
