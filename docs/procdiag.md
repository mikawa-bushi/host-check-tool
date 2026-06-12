# procdiag — local-first Linux ホスト資源診断ツール

`procdiag` は**事後診断**ツール。リアルタイムダッシュボードではなく、
**一定期間かけて記録し、決定論的なルールで改善提案を生成する**。

答える問いは「**なぜこの PC が重い・落ちる・ディスクが満杯か**」。
Glances/netdata 等のリアルタイム監視とも、py-spy 等のコードプロファイラとも異なり、
運用レベル (プロセス・サービス・ディスク) の原因切り分けと改善提案に徹する。

```bash
# インストール
pip install host-check-tool

# 3コマンドで診断
procdiag init
procdiag record --duration 24h
procdiag check
```

---

## Design in one picture

```
samplers/          storage/            diagnosis/          report/
(事実の記録)         (時系列DB)           (解釈)               (提示)

SystemSampler ─┐
CgroupSampler ─┤   SQLite WAL        ルールエンジン        結論ファースト text
ProcessSampler─┼──▶ バッファ書き  ──▶  (Finding生成)   ──▶ JSON (scripts/CI)
DiskSampler   ─┘   増分ロールアップ    Finding: 原因/       自己完結HTML (offline)
                   保持ポリシー        提案/確信度
              ↑
        SpikeTrigger
        (PSIスパイク検出
         → Tier2a/2b即時起動)
```

3層は意図的に疎結合:

- **Samplers** はOSから「事実」を記録するだけで解釈しない。
  失敗は1ティックの欠損として降格し、全体を止めない。
- **Storage** は WAL + バッファ書き + 増分ロールアップで自身の I/O フットプリントを最小化する。
- **Diagnosis** は Storage に問い合わせて決定論的ルールで Finding を生成する。
  `min_observation_hours` 未満の記録には発火しない。
- **Report** は HTML を完全自己完結で生成する (CDN 参照ゼロ、閉域網対応)。

---

## Quick start

```bash
# カレントディレクトリに config.py と data/ を生成
procdiag init

# config.py を必要に応じて編集してから記録を開始
procdiag record --duration 24h

# 別ターミナルで状況確認
procdiag status

# 記録終了後に診断
procdiag check

# HTML + JSON レポートを一括生成
procdiag report --out report.html --json report.json
```

---

## コマンドリファレンス

| コマンド | 目的 |
|---|---|
| `procdiag init [DIR]` | `config.py` と `data/` を生成する |
| `procdiag record` | 常駐ループで 3 層サンプリングを実行する |
| `procdiag check` | 記録済み DB に診断ルールを実行し改善提案を出力する |
| `procdiag report` | HTML + JSON レポートを一括生成する (`check` のラッパー) |
| `procdiag status` | 記録期間・サンプル数・自己負荷の概況を表示する |
| `procdiag doctor` | 記録開始前にホストの観測能力 (PSI/cgroup v2/権限/I-O読取率/journal/センサ/DB空き) を診断する。降格ありで exit 2 |
| `procdiag explain [名前]` | ルールまたはサンプラーの詳細を表示する |
| `procdiag list-rules` | 登録済みルールをカテゴリ別に一覧表示する |
| `procdiag list-samplers` | 登録済みサンプラーを Tier 別に一覧表示する |
| `procdiag export` | 記録データを CSV または JSON でエクスポートする |
| `procdiag prune` | 保持ポリシーを手動実行する |
| `procdiag service print` | systemd ユニットファイルを標準出力に表示する |
| `procdiag service install` | user systemd ユニットを `~/.config/systemd/user/` に配置する |

### record の主要オプション

```
--duration 24h   # 記録時間 (例: 60s / 1h / 24h)。省略時は無期限
--config FILE    # 設定ファイル (既定: ./config.py)
--db FILE        # DB ファイルパス (config の db_path を上書き)
```

### check / report の主要オプション

```
--format text|json|html   # 出力フォーマット
--output FILE             # ファイルに書き出す
--only MEM-LEAK-01,...    # 指定ルールのみ実行
--skip ZOMBIE-01,...      # 指定ルールをスキップ
--verbose                 # 推論過程を表示
```

### 終了コード (check)

| コード | 意味 |
|---|---|
| `0` | Finding なし (正常) |
| `1` | critical / warning の Finding あり |
| `2` | info レベルの Finding のみ |

---

## アーキテクチャマップ

```
host-check-tool/
├── procdiag/
│   ├── cli.py            # Click CLI (record / check / report / status / ...)
│   ├── config.py         # Pydantic 設定モデル (ProcdiagConfig)
│   ├── recorder.py       # 常駐ループ (Recorder) — サンプラーを束ねる
│   ├── scheduler.py      # TierScheduler — 多間隔ジョブの軽量実行
│   ├── storage.py        # SQLite 時系列ストア (WAL/バッファ/ロールアップ)
│   ├── plugins.py        # entry_points ベースのプラグイン読み込み
│   ├── export.py         # CSV / JSON エクスポート
│   ├── service.py        # systemd ユニット生成
│   ├── report.py         # terminal / JSON / HTML+SVG レポート生成
│   ├── samplers/
│   │   ├── __init__.py   # eager import → @register_sampler 登録
│   │   ├── system.py     # Tier-1: PSI / CPU / Mem / IO / Load
│   │   ├── cgroup.py     # Tier-2a: cgroup v2 サービス統計
│   │   ├── process.py    # Tier-2b: 全プロセス走査 + PID キャッシュ
│   │   ├── disk.py       # Tier-3: ディスク使用量 / ディレクトリサイズ
│   │   ├── trigger.py    # SpikeTrigger — PSI スパイク検出・デバウンス
│   │   └── selfmeter.py  # SelfMeter — 自己オーバーヘッド計測・自動減速
│   ├── core/
│   │   ├── sampler.py    # Sampler 基底クラス・レジストリ・RunContext
│   │   ├── rule.py       # Rule 基底クラス・レジストリ
│   │   ├── result.py     # Finding / Diagnosis / ObservationQuality 型
│   │   └── util.py       # parse_duration / normalize_cmdline / ...
│   └── diagnosis/
│       ├── engine.py     # Engine — ルールを順に実行し Diagnosis を組み立てる
│       ├── trend.py      # 線形回帰・外挿・周期検出 (純関数群)
│       ├── cpu.py        # CPU-SINGLECORE-01 / CPU-PERIODIC-01
│       ├── memory.py     # MEM-LEAK-01
│       ├── io_disk.py    # DISK-FULL-01
│       └── leak.py       # FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01 (T12)
└── tests/
    ├── test_e2e.py       # E2E: init→record→check→report の通し検証
    ├── test_recorder.py  # Recorder / TierScheduler の単体テスト
    ├── test_sampler_*.py # 各サンプラーの単体テスト
    └── fixtures/         # /proc・/sys/fs/cgroup のモックツリー
```

---

## 3 層 + トリガの説明

### Tier-1 — システム全体 (1 秒)

`SystemSampler` が毎秒取得する:

| 列 | 内容 |
|---|---|
| `cpu_total`, `cpu_max_core` | 全コア平均 / 最大コア使用率 |
| `mem_used`, `mem_available` | 使用メモリ / 利用可能メモリ |
| `swap_used`, `swap_in`, `swap_out` | スワップ使用量・増分 |
| `load1` | `/proc/loadavg` から 1 分ロードアベレージ |
| `psi_cpu_some10` 等 | `/proc/pressure/*` の PSI 値 (カーネル 4.20+ 必須) |
| `disk_read_bytes`, `disk_write_bytes` | 全ディスク I/O 増分 |
| `self_cpu`, `self_rss` | procdiag 自身のオーバーヘッド |

### Tier-2a — cgroup v2 サービス統計 (5 秒)

`CgroupSampler` が `/sys/fs/cgroup/system.slice/*/` 下の `.service` / `.scope` 毎に:

- `cpu.stat` から CPU 使用量 (マイクロ秒、増分)
- `memory.current`, `memory.swap.current` からメモリ使用量
- `io.stat` から I/O バイト数 (増分)
- `pids.current` から PID 数

cgroup v2 が存在しない環境では自動的にスキップされ、demotion note が記録される。

### Tier-2b — プロセス走査 (10 秒)

`ProcessSampler` が psutil で全プロセスを走査:

- PID キャッシュを保持して `cpu_percent()` の差分を正確に計測
- `io_counters()` へのアクセス拒否は欠損として記録 (非 root 環境では一般的)
- `proc_registry` テーブルにプロセス台帳を upsert

### Tier-3 — ディスク (60 秒 / 1 時間)

`DiskSampler` が:

- `collect()`: 60 秒毎にパーティション使用量を記録 (`disk_samples`)
- `collect_dirs()`: 1 時間毎に `watch_dirs` を走査 (`dir_samples`)。
  時間予算内に完了しない場合は `is_estimate=1` で打ち切り結果を記録。

### SpikeTrigger — PSI スパイク検出

Tier-1 の各ティックで `SystemSnapshot` を評価:

- PSI 利用可能: `cpu/mem/io` の `some/full avg10` が閾値を超えたら即発火
- PSI 非対応カーネル: CPU 使用率・メモリ比率・swap_out 増分でフォールバック
- デバウンス: 同種トリガは `spike_cooldown_sec` 秒以内に再発火しない
- ストーム集約: 5 分以内に `spike_storm_threshold` 回を超えたら "storm" 1 件に集約

発火時は Tier-2a/2b を即時起動し `spike_events` テーブルに記録する。

### SelfMeter — 自己計測と自動減速

- 60 秒スライディングウィンドウで procdiag 自身の CPU 使用率を移動平均
- 平均が `overhead_budget_pct` (既定 1%) を超えると Tier-2b 間隔を `throttle_factor` 倍に拡大
- 10 分間連続して予算内に収まると間隔を徐々に復元

---

## 観測品質の思想

procdiag が「観測できなかった事実」を隠さないよう、以下を設計原則とする:

1. **部分降格**: 1 プロセスの `io_counters` 取得に失敗しても他のプロセスの記録は継続する
2. **欠損率の明示**: `io_denied / io_total` の比率をレポートの ObservationQuality 節に表示する
3. **自己透明性**: `self_cpu`・`self_rss` を通常の計測列に含め、診断者が観測コストを把握できる
4. **min_observation_hours**: 十分な観測時間がない状態でルールを発火させない
5. **demotion notes**: PSI 非対応・cgroup v1 環境での降格理由を日本語で記録する

---

## ルールカタログ (組み込みルール)

| Rule ID | カテゴリ | 概要 |
|---|---|---|
| `MEM-LEAK-01` | memory | proc_rollup_1m の rss_avg に線形回帰をかけ、メモリリークの疑いを検出 |
| `MEM-PRESSURE-01` | memory | psi_mem の閾値超過時間帯が日次で反復し swap_out を伴う慢性メモリ不足を検出 |
| `SWAP-THRASH-01` | memory | swap_in/out の同時発生 + psi_mem_full>0 のスラッシングを検出 (瞬時型) |
| `CPU-SATURATION-01` | cpu | psi_cpu_some10>20 の継続を検出し、spike_events から犯人プロセスを列挙 |
| `CPU-SINGLECORE-01` | cpu | cpu_max_core_max ≥ 95% かつ cpu_avg < 50% のボトルネックコアを検出 |
| `CPU-PERIODIC-01` | cpu | spike_events の発火時刻から日次・時次の反復パターンを検出 |
| `IO-BOTTLENECK-01` | io_disk | psi_io_some10>30 の継続 + I/O 上位プロセス特定 (io 欠損時は cgroup で代替) |
| `DISK-FULL-01` | io_disk | disk_samples を線形外挿し、容量到達までの残り日数を算出 |
| `DISK-GROWTH-01` | io_disk | dir_samples の増分ランキングで増加源を特定 (DISK-FULL に折りたたみ) |
| `DISK-JOURNAL-01` | io_disk | journald のサイズ超過を検出し SystemMaxUse 設定を提案 |
| `OOM-KILL-01` | crash | プロセス台帳の途絶×直前のメモリ危機の相関で OOM kill を検出。journal 読取可なら確証度を昇格 |
| `FD-LEAK-01` | leak | fds_max の線形回帰で fd リークと ulimit 到達予測日数を算出 |
| `THREAD-LEAK-01` | leak | threads_max の単調増加を検出 |
| `ZOMBIE-01` | leak | ゾンビ近似プロセス数の増加傾向を検出 (confidence=low の近似検出) |

最低観測時間はルールごとに異なる (瞬時型 0h 〜 日次反復型 24h)。線形回帰系ルールの
既定 6 時間は `config.rules.min_observation_hours` で一括調整できる (0 で無効)。

record 常駐中は毎時の軽量プレチェックがリーク疑いプロセス (最大5件) を選び、
smaps_rollup から PSS を自動精査する — MEM-LEAK-01 は PSS でも増加が確認できた場合に
確証度を high へ昇格し、PSS が横ばいなら「共有メモリの可能性」として low へ下げる。
また記録 DB 自身の容量 (`retention.db_max_size_mb`、既定 2GB) と置き場の空きを監視し、
超過時は保持期間を段階短縮して ops_events に記録する — 診断ツールが
DISK-FULL の原因になることを構造的に防ぐ。

---

## 実測パフォーマンス (M8 負荷テスト)

`bin/load_test.py` による計測。条件: Linux 6.8 / Python 3.10 / psutil 7.2、
ベースライン約 500 プロセスにダミー 500 プロセスを追加した **約 1,000 プロセス環境**
(設計書 §4.1 の想定 200 プロセスの 5 倍)。

| 項目 | 実測値 |
|---|---|
| Tier-2b 全プロセス走査 1 回 | 中央値 82 ms / p95 91 ms (1,002 プロセス) |
| プロセス単価 | **82 µs/プロセス** (設計根拠 75–86 µs と整合) |
| Tier-2b の理論負荷 (10 秒間隔) | 0.82 % |
| 自己 CPU (60 秒 record, 定常) | **1.00 %** (起動過渡込み平均 1.15 % / 最大 18 %) |
| DB 増加 | 約 0.5 MB/分 → 生データ 48h 保持で約 1.4 GB 上限 |

補足:

- 200 プロセス級の現場 PC では proc_samples 量が約 1/5 になるため、自己負荷・DB とも
  設計見積り (負荷 0.5 % 未満 / 48h ≈ 150–200 MB) の範囲に収まる
- 定常負荷が予算 (1 %) を超える環境では SelfMeter が Tier-2b 間隔を自動拡大する
  (×1.5 ずつ、上限 60 秒 — §4.8 の二重安全網)
- チューニング履歴: PID ごとに不変な name/cmdline/proc_id をキャッシュ化し、
  AccessDenied が確定した PID の io_counters 読取をスキップすることで
  101 → 82 µs/プロセス (−19 %) を達成

