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

# HTML レポートを書き出す (拡張子が .json なら JSON)
procdiag check -o report.html
```

---

## コマンドリファレンス

| コマンド | 目的 |
|---|---|
| `procdiag quick` | 初めての人向け。snapshot・短い記録 (既定 1 時間、`--duration`)・診断を 1 回で行い、記録 DB と HTML レポートを `--out` (既定 `./procdiag-quick-<日時>`) に置く。Ctrl-C で記録を早めに終えても診断する。snapshot か診断に warning/critical があれば exit 1 |
| `procdiag snapshot` | 記録なしで現在のホスト状態を網羅チェックする (warning/critical で exit 1) |
| `procdiag init [DIR]` | `config.py` と `data/` を生成する |
| `procdiag record` | 常駐ループで 3 層サンプリングを実行する |
| `procdiag check` | 記録済み DB に診断ルールを実行し改善提案を出力する。`-o report.html` / `-o report.json` でファイルに書き出す |
| `procdiag compare` | 対策の前後 (1 つの DB の 2 期間、または 2 つの DB) を同じルール・しきい値で比べる |
| `procdiag status` | 記録期間・サンプル数・自己負荷の概況を表示する |
| `procdiag doctor` | 記録開始前にホストの観測能力 (PSI/cgroup v2/権限/I-O読取率/カーネルログの読取/センサ/DB の置き場の書き込み権限と空き容量) を診断する。空き容量は `retention.min_free_disk_mb` と比べる。降格ありで exit 2 |
| `procdiag explain [名前]` | ルールまたはサンプラーの詳細を表示する。引数なしでルールをカテゴリ別、サンプラーを Tier 別に一覧表示する |
| `procdiag export` | 記録データを CSV または JSON でエクスポートする |
| `procdiag prune` | 保持ポリシーを手動実行する |
| `procdiag bundle` | 記録 DB・レポート・snapshot・doctor を 1 つの tar.gz にまとめる (持ち出し解析用) |
| `procdiag service print` | systemd ユニットファイルを標準出力に表示する |
| `procdiag service install` | user systemd ユニットを `~/.config/systemd/user/` に配置する |

### record の主要オプション

```
--duration 24h   # 記録時間 (例: 60s / 1h / 24h)。省略時は無期限
--config FILE    # 設定ファイル (既定: ./config.py)
--db FILE        # DB ファイルパス (config の db_path を上書き)
```

### check の主要オプション

```
--format text|json|html   # 出力フォーマット (既定: --output の拡張子から判断し、それ以外は text)
-o, --output FILE         # ファイルに書き出す (.html/.htm → html、.json → json)
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
| `3` | 観測データなし (DB にサンプルが1件もない＝記録が回っていない、または `--since` / `--until` の期間に記録がない) |
| `4` | 前提条件エラー (DB ファイルが無い、設定ファイルが無い・読み込めない等)。`1` の異常検出と区別される |

CI で info を失敗扱いにしたくない場合は `procdiag check --exit-zero-on-info` を使うと、info のみのときに `0` を返す（`1` / `0` の判定は変わらない）。

### 診断する期間 (check)

`check` は、既定では生データ (既定の保持期間 48 時間) の残っている期間を診断する。1 分集計はそれより長く (既定 90 日) 残るので、それより前の記録があるときは、観測品質の備考にそのことが出る。観測期間の行には、診断した期間の開始と終了の時刻が出る (JSON では `window`)。

`--since` / `--until` で期間を選ぶと、1 分集計しか残っていない時間帯も含めて診断する。期間は `--until` の時刻を含まない。時刻はオフセットなしならこのホストのローカルタイム。期間に記録が 1 件も無いときは、記録のある範囲を表示して終了コード `3` で終わる。

```
procdiag check --since "2026-10-01 00:00"
procdiag check --since "2026-10-01 00:00" --until "2026-10-08 00:00" --format json
```

生データを読むルール (CPU-SATURATION-01 など) は、期間のうち生データのある時間帯だけで評価する。そうなったときは、備考にそのルールの一覧が出る。

期間が長いほど、診断に時間とメモリを使う。合成データでの計測例では、200 プロセスを 7 日分診断すると約 6 秒・0.9 GiB、30 日分では約 31 秒・3.7 GiB だった (生データの期間だけなら約 1.7 秒・0.3 GiB)。現場の PC で長い期間を診断するときは、空きメモリを確かめてから実行する。

### compare — 対策の効果を確かめる

「記録 → 診断 → 対策 → 効果の確認」の最後の段。2 つの期間をそれぞれ `check` と同じエンジン・同じ設定で診断し、(ルール, 対象) ごとに前後を突き合わせる。

```
procdiag compare --at "2026-10-28 09:00"                        # 1 つの DB を対策の時刻で分ける
procdiag compare --before 2026-10-20..2026-10-27 --after 2026-10-28..
procdiag compare before.db after.db                              # 2 つの DB (既定はそれぞれの全期間)
--only / --skip / --config / --format text|json / --output / --verbose / --no-color / --exit-zero-on-info
```

- **期間**: `START..END` は半開区間 (END を含まない)。オフセットの無い時刻はこのホストのローカルタイム。`--at` の修正前の期間は修正後と同じ長さで、全ルールの最低観測時間 (既定 24 時間) より短くはしない。期間は記録のある範囲 (生データと 1 分ロールアップの和) に切り詰める。1 つの DB で修正前と修正後の期間が重なる指定はエラー (exit 4)。
- **判定**: 修正前に検出 → 修正後に検出なしなら「解消」、新たに出れば「新規」、両方で検出なら重大度、同じ重大度なら値の 20% 以上の変化で「改善」「悪化」「変化なし」を決める。確信度は使わない (journal の照合や起動時刻などホスト依存の読み取りに左右されるため)。
- **検出が無いこと**: 値を持たないルール (リーク系以外) では、検出が無い期間がもう一方の期間と同じ長さ以上のときだけ、検出が無いことを「問題が無い」と扱う。1 日に数回起きる問題は、短い期間ではたまたま起きないことがあるため。修正後が修正前より短ければ、修正前の検出は「解消」ではなく「判定不能」(`too_short`、あと何時間必要かを示す)。修正前が修正後より短ければ、修正後の検出は「新規」ではなく「判定不能」。
- **測定ルール**: MEM-LEAK-01 / FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01 は「全対象の値を測る (observe)」と「しきい値で判定する (judge)」に分かれており、検出しなかった期間にも値が残る。修正後の値がしきい値の半分以下で「解消」、それより上で修正前から 20% 以上下がっていれば「改善 (しきい値未満)」(値がしきい値以上なら「改善 (しきい値以上)」)、そうでなければ値どうしを比べる (「変化なし」「悪化」)。値がしきい値以上でも R² が低いと judge は検出しないが、compare はこれを「問題が残っている」と扱う。対象プロセスは (名前, コマンドラインのハッシュ) で突き合わせ、残りが名前ごとに 1 対 1 のときだけ名前で突き合わせる。名前だけで突き合わせた行は、両方の値を示したうえで「判定不能」(`paired_by_name`) とする。コマンドラインが違えば別のプログラムかもしれないため。
- **判定不能**: 片側で記録時間が足りない (ルールの最低観測時間、または上の期間の長さ)・ルールの読む表の行が期間内に無い (生データの保持期間外など)・DB の版が古い・ルールの実行エラー・対象のプロセスが測れない・名前だけで突き合わせた場合。理由は JSON の `why` (例: `too_short`, `not_recorded`, `schema_missing`, `subject_not_observed`, `paired_by_name`) と日本語の説明で示す。
- **注意書き**: 記録ホスト・DB の版・記録時の設定の違い、期間の一部にしか生データが無いこと。
- HTML 出力は未対応 (`text` / `json` のみ)。

### 終了コード (compare)

| コード | 意味 |
|---|---|
| `0` | 修正後に critical / warning が残っておらず、修正前の critical / warning はすべて「解消」か「改善 (しきい値未満)」と判定できた |
| `1` | 修正後に critical / warning が残っている (修正後も検出された、または値が「変化なし」「悪化」「改善 (しきい値以上)」)、または新たに出た |
| `2` | 修正後に info のみ残る (`--exit-zero-on-info` で 0 に畳める) |
| `3` | どちらかの期間に記録が無い、または修正前の critical / warning が修正後に判定不能 (理由は行ごとに出る。記録時間不足なら、記録を続ければ判定できる) |
| `4` | 前提条件エラー (期間指定の誤り・1 つの DB で期間が重なる・DB が無い・DB の版が新しすぎる・設定ファイルが無い・読み込めない) |

優先順位は 1 > 3 > 2 > 0。CI では `3` を「まだ判定できない (記録を続ける)」として扱える。修正前が info だけの検出は判定不能でも `3` にしない。

---

## セキュリティ運用上の注意（設定ファイル）

`config.py` は読み込み時に **`exec()` で実行される**（netdiag 由来の設計で、設定に Python 式を書けるのが利点）。
これは、`config.py` への書き込み権限が、`procdiag` を実行するユーザー（特に systemd 常駐サービス）の
権限での **任意コード実行** と等価であることを意味する。次を守ること:

- `config.py` は **信頼できる場所** に置き、**root 所有・非 world-writable** にする（推奨 `chmod 0600`、複数ユーザーで読む必要がある場合のみ `0644`）。`doctor` が点検するのは world-writable ビットのみである点に注意。
- 誰でも書き込める場所（`/tmp` や 0777 の共有ディレクトリ等）には置かない。
- 配置後に `procdiag doctor` を実行すると、`設定ファイルの安全性` チェックが world-writable な `config.py` を
  `degraded` として警告する。

---

## アーキテクチャマップ

```
host-check-tool/
├── procdiag/
│   ├── cli.py            # Click CLI (record / check / status / ...)
│   ├── config.py         # Pydantic 設定モデル (ProcdiagConfig)
│   ├── recorder.py       # 常駐ループ (Recorder) — サンプラーを束ねる
│   ├── scheduler.py      # TierScheduler — 多間隔ジョブの軽量実行
│   ├── storage.py        # SQLite 時系列ストア (WAL/バッファ/ロールアップ)。make_row で名前付き行を生成
│   ├── spikes.py         # spike_events の JSON ペイロード契約 (記録側と読取側で共有)
│   ├── plugins.py        # entry_points ベースのプラグイン読み込み
│   ├── doctor.py         # 記録前の観測能力チェック (procdiag doctor)
│   ├── snapshot.py       # 記録なしの即時ヘルスチェック (procdiag snapshot)
│   ├── bundle.py         # 持ち出し用バンドル (procdiag bundle)
│   ├── status.py         # 記録 DB の概況 (status / bundle で共用)
│   ├── systemd.py        # systemctl / timedatectl / journalctl の読み取り専用ラッパー
│   ├── kernlog.py        # カーネルログ (journalctl -k) の異常行の分類
│   ├── export.py         # CSV / JSON エクスポート
│   ├── service.py        # systemd ユニット生成
│   ├── report.py         # terminal / JSON / HTML+SVG レポート生成
│   ├── samplers/
│   │   ├── __init__.py   # eager import → @register_sampler 登録
│   │   ├── system.py     # Tier-1: PSI / CPU / Mem / IO / Load
│   │   ├── cgroup.py     # Tier-2a: cgroup v2 サービス統計
│   │   ├── process.py    # Tier-2b: 全プロセス走査 + PID キャッシュ
│   │   ├── disk.py       # Tier-3: ディスク使用量 / ディレクトリサイズ
│   │   ├── thermal.py    # Tier-3: CPU 温度 / クロック (センサ有りのみ)
│   │   ├── service.py    # Tier-3: systemd の自動再起動・failed (systemd 有りのみ)
│   │   ├── kernel_log.py # Tier-3: カーネルログの異常 (journal 読取可のみ)
│   │   ├── trigger.py    # SpikeTrigger — PSI スパイク検出・デバウンス
│   │   └── selfmeter.py  # SelfMeter — 自己オーバーヘッド計測・自動減速
│   ├── core/
│   │   ├── sampler.py    # Sampler 基底クラス・レジストリ・RunContext
│   │   ├── rule.py       # Rule 基底クラス (configure / evaluate)・レジストリ
│   │   ├── result.py     # Finding / Diagnosis / ObservationQuality 型
│   │   └── util.py       # parse_duration / CounterDelta / バイト単位 / ...
│   └── diagnosis/
│       ├── engine.py     # Engine — ルールを順に実行し Diagnosis を組み立てる
│       ├── trend.py      # 線形回帰・外挿・周期検出・系列ヘルパ (純関数群)
│       ├── cpu.py        # CPU-PERIODIC-01 / CPU-SINGLECORE-01 / CPU-SATURATION-01
│       ├── memory.py     # MEM-LEAK-01 / MEM-PRESSURE-01 / SWAP-THRASH-01 (+ PSS 事前選定)
│       ├── io_disk.py    # DISK-FULL-01 / IO-BOTTLENECK-01 / DISK-GROWTH-01 / DISK-JOURNAL-01 / DISK-INODE-01 / FS-READONLY-01
│       ├── leak.py       # FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01
│       ├── crash.py      # OOM-KILL-01 / UNCLEAN-SHUTDOWN-01 / SERVICE-RESTART-01 / KERNEL-ERROR-01
│       └── thermal.py    # THERMAL-01
└── tests/
    ├── test_e2e.py       # E2E: init→record→check の通し検証
    ├── test_recorder.py  # Recorder / TierScheduler の単体テスト
    ├── test_sampler_*.py # 各サンプラーの単体テスト
    └── fixtures/         # /proc・/sys/fs/cgroup のモックツリー
```

---

## 3 層 + トリガの説明

プロセス名・コマンドライン・マウント先・cgroup のユニット名に UTF-8 でないバイトがあっても
(Shift_JIS の引数、カーネルが 15 バイトで切った日本語のプロセス名など)、そのまま記録する。
そのバイトは `\x91` のような 16 進の表記になる (例: `1号機温度\xe7\x9b`)。

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

- `collect()`: 60 秒毎にパーティション使用量・inode 使用量・読み取り専用フラグを記録 (`disk_samples`)
- `collect_dirs()`: 1 時間毎に `watch_dirs` を走査 (`dir_samples`)。
  時間予算内に完了しない場合は `is_estimate=1` で打ち切り結果を記録。

### Tier-3 — CPU 温度・クロック (10 秒)

`ThermalSampler` が CPU 温度と現在 / 最大クロックを `thermal_samples` に記録する:

- 温度 (`temp_max_c`) は CPU 自身のセンサー (coretemp・k10temp・zenpower・cpu_thermal など) の
  最高値。NVMe・GPU・Wi-Fi など CPU 以外のセンサーは、どれほど熱くても使わない。
- CPU のセンサーが無いときは、PC では ACPI の温度ゾーン (acpitz)、ARM ボードでは SoC の
  温度ゾーン (soc_thermal) を使う。どれも無ければ温度は空になり、THERMAL-01 は高温を判定できない。
  `snapshot` の「CPU 温度」も同じ値を表示する。
- 0.6.0 までの版で記録した DB の `temp_max_c` は、CPU 以外も含むすべてのセンサーの最高値。

### Tier-3 — サービス / カーネルログ (5 分)

- `ServiceSampler`: systemd が数える自動再起動回数 (`NRestarts`) の増加と failed 遷移だけを
  `service_events` に記録する (cgroup の作り直しは timer 起動の oneshot でも起きるため使わない)。
- `KernelLogSampler`: `journalctl -k` を前回の続き (cursor) から読み、I/O エラー・FS エラー・
  ハングタスク・MCE・segfault・USB 切断・温度制限の行だけを `kernel_events` に記録する。
  読み取りには root または systemd-journal / adm グループが必要。

### 記録セッション

`record` の各実行は `sessions` に開始時刻・boot 時刻を記録し、60 秒毎に生存時刻を更新、
正常停止 (SIGTERM / SIGINT / SIGHUP / `--duration` 満了) で停止時刻を記録する。
停止記録のないまま別の boot の記録が始まっていれば、電源断などの異常停止
(UNCLEAN-SHUTDOWN-01) と判定できる。

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
6. **入力データの明示**: 各ルールは必要な表・列 (`requires_schema`) と期間内に行が必要な表 (`requires_rows`) を宣言し、満たさないルールはエンジンが実行せず「記録なしで未評価」(JSON `uncovered_rules`) として示す。古い版の DB や生データが削除された期間で「問題なし」と読まれるのを防ぐ。センサや監視ディレクトリが無く一度も記録されない表 (`thermal_samples` / `dir_samples`) は対象外

---

## ルールカタログ (組み込みルール)

| Rule ID | カテゴリ | 概要 |
|---|---|---|
| `MEM-LEAK-01` | memory | proc_rollup_1m の rss_avg に線形回帰をかけ、メモリリークの疑いを検出 |
| `MEM-PRESSURE-01` | memory | psi_mem の閾値超過時間帯が日次で反復し swap_out を伴う慢性メモリ不足を検出 |
| `SWAP-THRASH-01` | memory | swap_in/out の同時発生 + psi_mem_full>0 のスラッシングを検出 (瞬時型) |
| `CPU-SATURATION-01` | cpu | psi_cpu_some10>20 の継続を検出し、spike_events から犯人プロセスを列挙 |
| `CPU-SINGLECORE-01` | cpu | cpu_max_core_max ≥ 95% かつ cpu_avg < 50% のボトルネックコアを検出 |
| `CPU-PERIODIC-01` | cpu | spike_events を負荷の始まりごとにまとめ、日次・時次の定時処理をホストのローカル時刻で検出 (記録した日・時間の半分以上で同じ時刻 ±2 分に始まり、偶然の一致の確率が 1% 未満のときだけ。偶然の確率は、その時刻の前の 1 時間・後の 1 時間・全体のうち負荷の始まりがいちばん多い頻度で見積もるので、勤務時間や昼休みに偏る負荷も周期にしない。1 回の長い負荷も周期にしない) |
| `IO-BOTTLENECK-01` | io_disk | psi_io_some10>30 の継続 + I/O 上位プロセス特定 (io 欠損時は cgroup で代替) |
| `DISK-FULL-01` | io_disk | disk_samples を線形外挿し、容量到達までの残り日数を算出 |
| `DISK-GROWTH-01` | io_disk | dir_samples の増分ランキングで増加源を特定 (DISK-FULL に折りたたみ) |
| `DISK-JOURNAL-01` | io_disk | journald のサイズ超過を検出し SystemMaxUse 設定を提案 |
| `OOM-KILL-01` | crash | プロセス台帳の途絶×直前のメモリ危機の相関で OOM kill を検出。journal 読取可なら確証度を昇格 |
| `FD-LEAK-01` | leak | fds_max の線形回帰で fd リークと ulimit 到達予測日数を算出 |
| `THREAD-LEAK-01` | leak | threads_max の単調増加を検出 |
| `ZOMBIE-01` | leak | プロセス走査ごとのゾンビ数の増加傾向を検出 (v4 より前の DB は近似、confidence=low) |
| `DISK-INODE-01` | io_disk | inode 使用率の高止まり・増加を検出 (容量に空きがあっても書けない状態) |
| `FS-READONLY-01` | io_disk | 記録中にファイルシステムが読み取り専用へ切り替わったことを検出 |
| `UNCLEAN-SHUTDOWN-01` | crash | 記録セッション履歴から、停止処理なしの再起動 (電源断・パニック) を検出 |
| `SERVICE-RESTART-01` | crash | systemd の自動再起動 (NRestarts) の増加・failed 遷移からクラッシュループを検出 |
| `KERNEL-ERROR-01` | crash | カーネルログの I/O エラー・FS エラー・ハングタスク・MCE・segfault・USB 切断を集計 |
| `THERMAL-01` | cpu | 高温下での継続的なクロック低下 (サーマルスロットリング) を検出。同じ時刻の CPU 温度とクロック比を突き合わせ、80°C 以上かつ最大の 85% 未満の記録が 3 回以上続いた期間を数える |

最低観測時間はルールごとに異なる (瞬時型 0h 〜 日次反復型 24h)。線形回帰系ルールの
既定 6 時間は `config.rules.min_observation_hours` で一括調整できる (0 で無効)。
観測時間 (最初と最後の記録の間隔) は記録した時間より最大 2 分近く短くなる (1 分集計は
分の始まりの時刻を持ち、停止したときの途中の 1 分は集計されない) ため、最低観測時間に
2 分以内で届かない場合も足りているとみなす。
観測時間不足で評価できなかったルールは、診断結果に「評価範囲」として必ず表示される。

各ルールのしきい値 (クラス属性の数値) は `config.rules.overrides[<RULE-ID>]` で上書きでき、
一覧と既定値は `procdiag explain <RULE-ID>` で確認できる。

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

