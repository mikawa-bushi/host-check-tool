# host-check-tool

This package ships **`procdiag`** — a local-first Linux host-resource diagnosis
tool. **「なぜこの PC が重い・落ちる・ディスクが満杯か」**に答える。

CPU・メモリ・ディスクの使用状況を一定期間低負荷で記録し、**決定論的な検出ルール**に
基づいて運用レベルの改善提案を日本語レポートとして出力する。リアルタイム監視
ダッシュボードではなく**「記録 → 診断 → 提案」の事後診断ワークフロー**であり、
工場のエッジ PC・ラインサーバーなど IT 人材が常駐しない環境を主用途とする。

- **オフライン・閉域網ファースト** — 外部通信ゼロ。レポートは CDN 参照なしの単一 HTML
- **まず 1 コマンドで診断** — `procdiag quick` が、いまの状態の網羅チェック (CPU・メモリ・
  ディスク (容量/inode/読み取り専用化)・サービス・カーネル異常・温度など)、1 時間の記録、
  その記録の診断を続けて行う。記録なしの網羅チェックだけなら `procdiag snapshot` (数秒)
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

# これだけで診断: いまの状態の確認 → 1 時間の記録 → 診断
# 結果は画面に出る。記録の診断は ./procdiag-quick-<日時>/report.html にも書き出す。
# Ctrl-C で記録を早めに終えても診断する
procdiag quick
```

1 時間の記録で評価できるのは、短い記録で判断できるルールだけ (リーク系は 6 時間、
慢性メモリ圧迫は 24 時間の記録が必要)。未評価のルールと、あと何時間の記録が必要かは
診断結果に表示される。長期の傾向まで診断するには、記録を続ける:

```bash
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

# 提案どおり対策したら記録を続け、対策の前後を数値で比べる
procdiag compare --at "2026-10-28 09:00"

# HTML レポートを書き出す (拡張子が .json なら JSON)
procdiag check -o report.html

# 記録 DB・レポート・現在の状態を 1 つの tar.gz に (USB で持ち出して解析)
procdiag bundle
```

## コマンドリファレンス

| コマンド | 内容 |
|---|---|
| `procdiag quick` | 初めての人向け。snapshot・短い記録 (既定 1 時間)・診断を 1 回で行い、HTML レポートを書き出す (Ctrl-C で記録を早めに終えても診断する) |
| `procdiag snapshot` | 記録なしで現在のホスト状態を網羅チェック (warning/critical で exit 1) |
| `procdiag init [DIR]` | `config.py` と `data/` を生成 |
| `procdiag record` | 常駐ループで CPU/メモリ/ディスクを記録 |
| `procdiag check` | 記録済みデータに診断ルールを実行し改善提案を出力 (`-o report.html` / `-o report.json` でファイルに) |
| `procdiag compare` | 対策の前後 (1 つの DB の 2 期間、または 2 つの DB) を同じルール・しきい値で比べる |
| `procdiag status` | 記録期間・サンプル数・自己負荷を表示 |
| `procdiag doctor` | 記録開始前にホストの観測能力 (PSI/cgroup/権限/センサ等) を診断 |
| `procdiag explain [RULE\|sampler]` | ルールまたはサンプラーの詳細を表示 (引数なしでルールをカテゴリ別、サンプラーを Tier 別に一覧表示) |
| `procdiag export` | 記録データを CSV または JSON でエクスポート |
| `procdiag prune` | 保持ポリシーの手動実行 |
| `procdiag bundle` | 記録 DB・レポート・snapshot・doctor を 1 つの tar.gz に (コマンドラインは既定で削除) |
| `procdiag service print` | systemd ユニットファイルを表示 |
| `procdiag service install` | user systemd ユニットを配置 |

主要オプション:

```
procdiag record --duration 24h --config config.py --db data/procdiag.db
procdiag check  -o findings.json   # 拡張子で形式を決める (.html/.htm → HTML、.json → JSON。--format が優先)
procdiag check  --only MEM-LEAK-01,DISK-FULL-01
procdiag check  --since "2026-10-01 00:00"   # 期間を選ぶ (既定は生データの残っている期間)
procdiag compare --at "2026-10-28 09:00" --format json
procdiag status --config /etc/procdiag/config.py   # DB の場所は config の db_path から
```

`check` / `status` / `export` / `prune` / `bundle` は `--config` を受け取り、
DB の場所 (`db_path`) と保持期間などを設定ファイルから解決する (`--db` 指定が優先)。

記録時間が短いと、長期の観測が必要なルール (リーク系は 6 時間、慢性メモリ圧迫は 24 時間)
は評価されない。`check` はその場合「評価範囲: 20 ルール中 N 件のみ評価」と未評価の
ルール・必要な残り時間を表示する (JSON では `skipped_rules`)。生データの保持期間 (既定 48 時間)
を過ぎた期間や古い版の DB のように、ルールが読む表・列・行が無い場合も「記録なしで未評価」
として理由とともに表示する (JSON では `uncovered_rules`)。

## 対策の効果を確かめる (compare)

`check` の提案どおりに対策したら、記録を続けたうえで `procdiag compare` で前後を比べる。
両方の期間に同じルール・同じしきい値 (`--config` の `rules.overrides` を含む) を当て、検出ごとに
解消 / 改善 / 変化なし / 悪化 / 新規 / 判定不能 を示す。リーク系のルール (MEM-LEAK-01 /
FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01) は検出が消えた期間でも同じ計算で値を測るので、
前後の数値が並ぶ。

```
# 1 つの DB を対策した時刻で分ける (修正前 = 修正後と同じ長さの直前の期間。最低 24 時間)
procdiag compare --at "2026-10-28 09:00"

# 期間を明示する (START..END は END を含まない。片側は省略可)
procdiag compare --before 2026-10-20..2026-10-27 --after "2026-10-28 09:00.."

# 2 つの DB (対策前に持ち出した DB と、対策後に記録し直した DB。既定はそれぞれの全期間)
procdiag compare before.db after.db --format json --output compare.json
```

出力例:

```
修正前: 2026-10-21 09:00 〜 2026-10-28 08:59  168.0 時間 (うち生データ 0.0 時間)
修正後: 2026-10-28 09:00 〜 2026-11-04 08:59  168.0 時間 (うち生データ 48.0 時間)
時刻はこのホストのローカルタイム (JST +0900)

✔ 解消  [MEM-LEAK-01]      myapp            RSS 増加率 38.0 → 0.2 MB/h

修正前の未評価:
  CPU-SATURATION-01, IO-BOTTLENECK-01, SWAP-THRASH-01: 期間内に sys_samples の記録がありません
結果: 解消 1 件。修正前に検出された問題はすべて解消しました。
```

- 時刻はオフセットなしならこのホストのローカルタイム、`+09:00` や `Z` 付きならその時刻帯。
- 修正後の値がしきい値の半分以下なら「解消」。しきい値は下回ったがそこまで下がっていないときは
  「改善 (しきい値未満)」とし、修正後の値と R² を示す (0.9 MB/h でも直線的に増え続けていれば
  リークは残っている)。修正前から 20% 以上下がっていなければ「変化なし」または「悪化」。
  修正後の値がしきい値以上なのに R² が低くて検出されなかったときは、20% 以上下がっていても
  「改善 (しきい値以上)」とする。「変化なし」「悪化」「改善 (しきい値以上)」は、修正後も問題が
  残っているものとして終了コード 1 にする。
- リーク系以外のルール (サービスの再起動、カーネルのエラーなど) で検出が消えたときは、
  修正後の期間が修正前の期間と同じ長さ以上のときだけ「解消」とする。1 日に数回しか起きない
  問題は、短い期間ではたまたま起きないことがあるため。修正後が短いときは「判定不能」とし、
  あと何時間記録すれば比べられるかを示す。`--at` の修正前は修正後と同じ長さ (最低 24 時間) なので、
  修正後を 24 時間記録すれば比べられる。2 つの DB では `--before` で修正前を修正後と同じ長さに
  絞れる。修正前の方が短く、修正後にだけ検出されたものも「新規」ではなく「判定不能」とする。
- リーク系のルールは対象のプロセスを (名前, コマンドライン) で突き合わせる。修正前と同じ
  コマンドラインのプロセスが修正後に無く、同じ名前のプロセスが 1 つだけあるときは、その値を
  並べて示すが、別のプログラムかもしれないので「判定不能」とする (JSON の `why` は `paired_by_name`)。
- 記録時間不足・生データの保持期間外・DB の版が古い・対象のプロセスが動いていない、など
  片側で判定できないものは「判定不能」とし理由を示す。「解消」とは表示しない。
- 記録ホスト・DB の版・記録時の設定が 2 つの DB で違うときは「注意:」で知らせる。
- `--format` は `text` と `json`。JSON は `"format": "procdiag-compare/1"` を持ち、キー名は固定。

## 終了コード

`procdiag check` はスクリプト・CI に組み込めるよう
終了コードで結果を返す:

| コード | 意味 |
|---|---|
| `0` | Finding なし (正常) |
| `1` | critical / warning の Finding あり |
| `2` | info レベルの Finding のみ (`--exit-zero-on-info` で 0 に畳める) |
| `3` | 観測データなし (DB にサンプルが1件もない＝記録が回っていない、または `--since` / `--until` の期間に記録がない) |
| `4` | 前提条件エラー (DB ファイルが無い・開けない、設定ファイルが無い・読み込めない、出力先に書けない、コマンドラインの誤り等)。`1` の異常検出と区別される |

`procdiag quick` も同じ番号を返す。診断のほか、最初の snapshot に warning / critical があるときも `1` になる。
`--duration` が不正なとき、`--out` が空でないディレクトリのとき、出力先を使えないとき、レポートを書けなかったときは `4`。レポートを書けなかったときは、診断の結果より `4` を優先する。

`procdiag compare` も同じ番号を使う (優先順位は 1 > 3 > 2 > 0):

| コード | 意味 |
|---|---|
| `0` | 修正後に critical / warning が残っておらず、修正前の critical / warning はすべて「解消」か「改善 (しきい値未満)」と判定できた |
| `1` | 修正後に critical / warning が残っている (修正後も検出された、または値が「変化なし」「悪化」「改善 (しきい値以上)」)、または新たに出た |
| `2` | 修正後に info のみ残る (`--exit-zero-on-info` で 0 に畳める) |
| `3` | どちらかの期間に記録が無い、または修正前の critical / warning が修正後に判定不能 (理由は行ごとに出る。記録時間不足なら、記録を続ければ判定できる) |
| `4` | 前提条件エラー (期間指定の誤り・1 つの DB で期間が重なる・DB が無い・DB の版が新しすぎる・DB が壊れている・出力先に書けない・コマンドラインの誤り・設定ファイルが無い・読み込めない) |

`status` / `export` / `bundle` / `prune` / `record` / `init` も、前提を満たせないときは理由を 1 行で示して `4` で終わる。
DB が開けない (新しい版・壊れている・SQLite でない・ディレクトリ)、`-o` / `--output` / `--out` の出力先が無い・書けない・ディレクトリ、
`prune` の `--raw` / `--rollup` の形式が違う、`init` の作成先に作れない、などである。
コマンドラインの書き間違い (未知のコマンドやオプション、値の抜け、`--format` の誤り、余分な引数) も `4` で、
`check` の `2` (info のみ) とは区別される。出力先は診断の前に確かめる。
出力ファイルは書き終えてから置くので、途中で失敗しても壊れたファイルは残らない。

記録を止めた後の DB は、書き込めないフォルダ (root が記録した DB・読み取り専用の USB) にあっても `check` / `status` / `export` / `bundle` で読める。
記録が動いている間は、DB のあるフォルダに書き込めるユーザー (記録と同じユーザー) で実行する。
`procdiag export | head` のように読み手が先に閉じたときは、何も表示せず `141` (シェルが SIGPIPE で終えたときの値) で終わる。

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

`rules.disabled` も同じで、存在しないルール ID を書くと起動時にエラーになる。
`check` / `compare` の `--only` / `--skip` では、ルール ID を `procdiag explain` の一覧どおり
大文字で書く。存在しない ID を書いたとき、`rules.disabled` で無効にしたルールを `--only` に
書いたとき、実行するルールが 1 つも残らないときは、診断せずに終了コード 4 で終わる。

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
