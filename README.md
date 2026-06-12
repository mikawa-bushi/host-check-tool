# host-check-tool

This package ships **`procdiag`** — a local-first Linux host-resource diagnosis
tool that is the sibling of
[network-check-tool / netdiag](https://github.com/mikawa-bushi/network-check-tool).

| | network-check-tool | **host-check-tool** |
|---|---|---|
| 配布物名 (PyPI) | `network-check-tool` | `host-check-tool` |
| 主役 CLI | `netdiag` | **`procdiag`** |
| 問いの分担 | 「どの層が壊れていて、なぜか」 | 「なぜこの PC が重い・落ちる・ディスクが満杯か」 |
| 根拠 | ネットワーク層のプローブ | CPU/メモリ/ディスクの時系列記録 |

`netdiag` がその場で測るのに対して、`procdiag` は**一定期間かけて記録し、
後から診断する**。ファクトリ・エッジ PC など IT 人材が常駐しない環境での
事後診断・改善提案を主用途とする。

![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-yellow)

## Quick Start

```bash
# インストール
pip install host-check-tool

# 設定と data/ ディレクトリを生成
procdiag init

# 記録を開始 (Ctrl-C で安全に停止)
procdiag record

# 別のターミナルから — または記録停止後に実行
procdiag check

# HTML + JSON レポートを生成
procdiag report --out report.html --json report.json
```

## コマンドリファレンス

| コマンド | 内容 |
|---|---|
| `procdiag init [DIR]` | `config.py` と `data/` を生成 |
| `procdiag record` | 常駐ループで CPU/メモリ/ディスクを記録 |
| `procdiag check` | 記録済みデータに診断ルールを実行し改善提案を出力 |
| `procdiag report` | HTML + JSON レポートを一括生成 |
| `procdiag status` | 記録期間・サンプル数・自己負荷を表示 |
| `procdiag explain [RULE\|sampler]` | ルールまたはサンプラーの詳細を表示 |
| `procdiag list-rules` | 登録済みルールを一覧表示 |
| `procdiag list-samplers` | 登録済みサンプラーを Tier 別に一覧表示 |
| `procdiag export` | 記録データを CSV または JSON でエクスポート |
| `procdiag prune` | 保持ポリシーの手動実行 |
| `procdiag service print` | systemd ユニットファイルを表示 |
| `procdiag service install` | user systemd ユニットを配置 |

主要オプション:

```
procdiag record --duration 24h --config config.py --db data/procdiag.db
procdiag check  --format json --output findings.json
procdiag check  --only MEM-LEAK-01,DISK-FULL-01
```

## 終了コード

`procdiag check` (alias: `procdiag advise`) の終了コードは netdiag と統一:

| コード | 意味 |
|---|---|
| `0` | Finding なし (正常) |
| `1` | critical / warning の Finding あり |
| `2` | info レベルの Finding のみ |

## systemd 運用

```bash
# ユーザー systemd として配置
procdiag service install --config /etc/procdiag/config.py

# 有効化
systemctl --user daemon-reload
systemctl --user enable --now procdiag.service

# ログアウト後も継続する場合
sudo loginctl enable-linger $USER
```

system ユニット (root) として配置する場合:

```bash
procdiag service print --system | sudo tee /etc/systemd/system/procdiag.service
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

## 依存関係

- Python 3.10+
- psutil — OS メトリクス取得
- pydantic — 設定バリデーション
- jinja2 — HTML レポートテンプレート
- click — CLI フレームワーク
- 標準ライブラリのみ (sqlite3, signal, threading, …)

numpy・pandas・rich は**不要**。外部ネットワーク通信ゼロ。

## 詳細ドキュメント

設計・アーキテクチャ・ルールカタログは [docs/procdiag.md](docs/procdiag.md) を参照。

## ライセンス

MIT
