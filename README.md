# host-check-tool

This package ships **`procdiag`** — a local-first Linux host-resource diagnosis
tool. **「なぜこの PC が重い・落ちる・ディスクが満杯か」**に答える。

CPU・メモリ・ディスクの使用状況を一定期間低負荷で記録し、**決定論的な検出ルール**に
基づいて運用レベルの改善提案を日本語レポートとして出力する。リアルタイム監視
ダッシュボードではなく**「記録 → 診断 → 提案」の事後診断ワークフロー**であり、
工場のエッジ PC・ラインサーバーなど IT 人材が常駐しない環境を主用途とする。

- **オフライン・閉域網ファースト** — 外部通信ゼロ。レポートは CDN 参照なしの単一 HTML
- **まず 1 コマンドで現状確認** — `procdiag snapshot` が記録なしで数秒のうちに
  CPU・メモリ・ディスク (容量/inode/読み取り専用化)・サービス・カーネル異常・温度などを網羅チェック
- **3 コマンドで傾向診断** — `pip install` → `procdiag init` → `procdiag record`
- **決定論的な診断** — 20 の検出ルール × テンプレートで提案を生成 (LLM 非依存)。
  出力は機械可読 (JSON) で、外部での活用は利用者に委ねる
- **観測者効果の最小化と透明性** — 自身のオーバーヘッドを計測・報告し、
  予算超過時は自動減速する

![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-yellow)

## Quick Start

```bash
# インストール (閉域網の PC へは docs/offline-install.md の手順で)
pip install host-check-tool

# 記録なしで、いまの状態をその場でチェック (数秒)
procdiag snapshot

# 設定と data/ ディレクトリを生成
procdiag init

# 記録開始前にホストの観測能力 (PSI/cgroup/権限/容量) を確認
procdiag doctor

# 記録を開始 (Ctrl-C で安全に停止)
procdiag record

# 別のターミナルから — または記録停止後に実行
procdiag check

# HTML + JSON レポートを生成
procdiag report --out report.html --json report.json

# 記録 DB・レポート・現在の状態を 1 つの tar.gz に (USB で持ち出して解析)
procdiag bundle
```

## コマンドリファレンス

| コマンド | 内容 |
|---|---|
| `procdiag snapshot` | 記録なしで現在のホスト状態を網羅チェック (warning/critical で exit 1) |
| `procdiag init [DIR]` | `config.py` と `data/` を生成 |
| `procdiag record` | 常駐ループで CPU/メモリ/ディスクを記録 |
| `procdiag check` | 記録済みデータに診断ルールを実行し改善提案を出力 |
| `procdiag report` | HTML + JSON レポートを一括生成 |
| `procdiag status` | 記録期間・サンプル数・自己負荷を表示 |
| `procdiag doctor` | 記録開始前にホストの観測能力 (PSI/cgroup/権限/センサ等) を診断 |
| `procdiag explain [RULE\|sampler]` | ルールまたはサンプラーの詳細を表示 |
| `procdiag list-rules` | 登録済みルールを一覧表示 |
| `procdiag list-samplers` | 登録済みサンプラーを Tier 別に一覧表示 |
| `procdiag export` | 記録データを CSV または JSON でエクスポート |
| `procdiag prune` | 保持ポリシーの手動実行 |
| `procdiag bundle` | 記録 DB・レポート・snapshot・doctor を 1 つの tar.gz に (コマンドラインは既定で削除) |
| `procdiag service print` | systemd ユニットファイルを表示 |
| `procdiag service install` | user systemd ユニットを配置 |

主要オプション:

```
procdiag record --duration 24h --config config.py --db data/procdiag.db
procdiag check  --format json --output findings.json
procdiag check  --only MEM-LEAK-01,DISK-FULL-01
procdiag status --config /etc/procdiag/config.py   # DB の場所は config の db_path から
```

`check` / `report` / `status` / `export` / `prune` / `bundle` は `--config` を受け取り、
DB の場所 (`db_path`) と保持期間などを設定ファイルから解決する (`--db` 指定が優先)。

記録時間が短いと、長期の観測が必要なルール (リーク系は 6 時間、慢性メモリ圧迫は 24 時間)
は評価されない。`check` はその場合「評価範囲: 20 ルール中 N 件のみ評価」と未評価の
ルール・必要な残り時間を表示する (JSON では `skipped_rules`)。

## 終了コード

`procdiag check` (alias: `procdiag advise`) はスクリプト・CI に組み込めるよう
終了コードで結果を返す:

| コード | 意味 |
|---|---|
| `0` | Finding なし (正常) |
| `1` | critical / warning の Finding あり |
| `2` | info レベルの Finding のみ (`--exit-zero-on-info` で 0 に畳める) |
| `3` | 観測データなし (DB にサンプルが1件もない＝記録が回っていない) |
| `4` | 前提条件エラー (DB ファイルが無い等)。`1` の異常検出と区別される |

## systemd 運用

```bash
# ユーザー systemd として配置
procdiag service install --config /etc/procdiag/config.py

# 有効化
systemctl --user daemon-reload
systemctl --user enable --now procdiag-record.service

# ログアウト後も継続する場合
sudo loginctl enable-linger $USER
```

system ユニット (root) として配置する場合:

```bash
procdiag service print --system | sudo tee /etc/systemd/system/procdiag-record.service
```

## 設定

`procdiag init` で生成される `config.py` を編集する。
Pydantic モデルで検証されるため、タイポや型ミスは起動時にエラーになる。

```python
config = ProcdiagConfig(
    db_path="./data/procdiag.db",
    sampling=SamplingConfig(
        system_interval=1.0,    # Tier1: PSI + CPU/Mem/IO (秒)
        cgroup_interval=5.0,    # Tier2a: cgroup v2 統計
        process_interval=10.0,  # Tier2b: プロセス走査
        disk_interval=60.0,     # Tier3: ディスク使用量
        watch_dirs=[
            WatchDir(path="/var/log", time_budget_sec=10),
        ],
    ),
    retention=RetentionConfig(raw="48h", rollup="90d"),
)
```

現場ごとに判定のしきい値を変えたい場合は `rules.overrides` で上書きできる。
調整できる項目と既定値は `procdiag explain <RULE-ID>` で確認できる。存在しない
ルール ID や項目名を書くと起動時にエラーになる。

```python
rules=RulesConfig(
    overrides={
        "CPU-SATURATION-01": {"psi_threshold": 30.0},  # CPU 待ち 30% 超を飽和とみなす
        "DISK-FULL-01": {"warning_days": 14},          # 満杯 14 日前から警告
    },
),
```

## 依存関係

- Python 3.10+
- psutil — OS メトリクス取得
- pydantic — 設定バリデーション
- jinja2 — 診断の提案文テンプレートのレンダリング (HTML レポート本体は標準ライブラリのみで生成)
- click — CLI フレームワーク
- 標準ライブラリのみ (sqlite3, signal, threading, …)

numpy・pandas・rich は**不要**。外部ネットワーク通信ゼロ。

## 詳細ドキュメント

設計・アーキテクチャ・ルールカタログは [docs/procdiag.md](https://github.com/mikawa-bushi/host-check-tool/blob/main/docs/procdiag.md) を参照。

## ライセンス

MIT
