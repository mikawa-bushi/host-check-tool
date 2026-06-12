# procdiag 実装案 — netdiag (network-check-tool) アナロジー版

対象: `procdiag_design.md` (Draft 1.0) を実装に落とすための構成案
制約: [mikawa-bushi/network-check-tool](https://github.com/mikawa-bushi/network-check-tool) と
**リポジトリ構成・パッケージ構造・コマンド体系・語彙にアナロジーが成立する**こと

ステータス: 提案 (2026-06-11)。サンプリング設計(§4)、DDL(§5)、ルールカタログ(§6.2)、
config仕様(§8)は設計書のまま変更しない。本書は「どういう骨格で実装するか」と
「設計書からの変更点」だけを定義する。

---

## 1. 基本方針

network-check-tool は1つの配布物で2つのCLIを出荷している:
`network-checker`(監視・ダッシュボード)と `netdiag`(診断)。本体は `netdiag` であり、

> probes(事実の収集)→ diagnosis(解釈)→ report(提示)

の3層分離、`check` を中心とした結論ファーストCLI、stdlib sqlite3 の時系列ストア、
エントリポイントによるプラグイン拡張、が設計の核になっている。

procdiag はこの骨格をそのまま「ホスト資源診断」に写像する:

> samplers(事実の記録)→ storage(履歴)→ diagnosis(解釈)→ report(提示)

netdiag との本質的な違いは1点だけ: netdiag の probe は**その場で**測るが、
procdiag の sampler は**期間をかけて**記録し、診断は記録(DB)に対して走る。
それ以外の構造・命名・ユーザー体験はすべて netdiag に揃える。

リポジトリの関係も相似にする:

| | network-check-tool | host-check-tool(本リポジトリ) |
|---|---|---|
| 配布物名 (PyPI) | `network-check-tool` | `host-check-tool`(未登録を確認済み) |
| 主役CLI | `netdiag` | `procdiag`(PyPI `procdiag` も未登録 → import名/コマンド名として安全) |
| 問いの分担 | 「どの層が壊れていて、なぜか」 | 「なぜこのPCが重い・落ちる・満杯か」 |
| 接点 | `netdiag/probes/system.py` がOS資源を一瞥する | procdiag はその領域を本業として深掘りする |

設計書 §12 の「ツール名の最終決定(PyPI重複確認)」はこれで解決:
**配布名 = host-check-tool、パッケージ名/コマンド名 = procdiag**。

---

## 2. アナロジー対応表

### 2.1 概念・語彙

| netdiag | procdiag | 備考 |
|---|---|---|
| Probe(1つの狭い問いに答える) | **Sampler**(1つの対象を記録する) | 解釈しない・1責務、は共通 |
| `layer`: L1/L2/L3/L4/L7/SYSTEM | `tier`: T1/T2a/T2b/T3 | クラス属性。`list-samplers` の分類軸 |
| CheckResult(事実) | サンプル行(DBの事実)+ 欠損記録 | procdiagの「事実」は時系列なのでDBが担う |
| Hypothesis(原因仮説) | **Finding**(設計書 §6.1) | confidence 語彙 high/medium/low を共有 |
| Diagnosis(results+hypotheses+healthy+reasoning) | **Diagnosis**(findings+healthy+reasoning+quality) | `primary` プロパティも同形 |
| RUNBOOKS(原因→意味+手順) | 提案テンプレート(5部構成 Jinja2) | 「データとして分離し局所編集可能に」も共通 |
| `depends_on` による因果折りたたみ | Finding間の関連付け(例: DISK-GROWTH→DISK-FULL) | 「結果を原因と競わせない」原則を継承 |
| SKIP + 理由(権限・ツール欠如で降格) | 欠損として記録 + 欠損率をレポート明記 | 設計書 §2.1 降格マトリクスがそのまま対応 |
| baseline.py / changepoint.py(統計) | trend.py(回帰・外挿・周期検出) | stdlib `statistics` のみ、純関数群 |
| StateMachine(デバウンス/フラップ抑制) | SpikeTrigger(デバウンス/ストーム集約) | §4.6 そのまま。閾値状態機械として同形 |
| alerts.jsonl / state.json | spike_events / ops_events テーブル | 「静かな定常+変化時のみ記録」が共通 |
| entry point `netdiag.probes` | entry point `procdiag.rules` | plugins.py の実装をほぼ流用 |

### 2.2 コマンド

| netdiag | procdiag | 内容 |
|---|---|---|
| `network-checker init` | `procdiag init [DIR]` | 雛形生成(config.py + データdir) |
| `netdiag watch`(常駐・軽量ループ) | `procdiag record`(常駐・3層サンプリング) | SIGTERM/SIGINTで安全終了、systemd と整合 |
| `netdiag check`(結論ファースト診断) | `procdiag check`(記録に対する診断) | **設計書の `advise` を改名**(エイリアス維持)。exit 0/1/2 も同一 |
| `netdiag check --format html --output f` | `procdiag report` | HTML+JSON一括生成の便宜コマンドとして残す |
| `netdiag status` | `procdiag status` | 記録の概況(期間・サンプル数・自己負荷・DB容量) |
| `netdiag explain [probe]` | `procdiag explain [rule\|sampler]` | **追加**。教育モード: 何を・なぜ・手動コマンド |
| `netdiag list-probes` | `procdiag list-rules` / `list-samplers` | **追加**。tier / カテゴリ別に列挙 |
| `netdiag prune` | `procdiag prune` | **追加**。保持ポリシーの手動実行(自動でも走る) |
| `netdiag service print/install` | `procdiag service print/install` | **設計書の `install-service` を改組**。CPUQuota=2% 付与 |
| `netdiag history` | `procdiag export [--format csv\|json] [--rollup 1min]` | 履歴の外部活用(monitor-app連携) |
| `netdiag snapshot save/diff` | (v2候補: システム構成スナップショット) | 今回は対象外。将来の相似拡張として記録 |

CLI仕様の細部も合わせる:

- **終了コード**: `check` は 0=Finding無し / 1=critical・warning あり / 2=info のみ。
  (netdiag check の 0=健全 / 1=原因あり / 2=警告のみ、と同義に揃える)
- **共通フラグ**: `--format text|json|html`、`--output FILE`、`--verbose`(推論過程の表示)、
  `--no-color`。`record` は `--duration 24h --config config.py --db data.db`(設計書どおり)
- **ターミナル出力の語彙**: ✔/⚠/✘/— のステータス記号、`━━━ 診断 (Diagnosis) ━━━` 見出し、
  確信度の 高/中/低 表記を netdiag からそのまま借用(スイートとしての見た目の統一)

```
$ procdiag check
✘ [T2b] mysqld        RSS +38MB/h で単調増加 (R²=0.97, 18h観測)
⚠ [T3]  /var          残り 9.2 日で満杯 (1.1GB/day)
— [T2b] io_counters   他ユーザープロセスのio読取不可 (非root) — 欠損率 34%

━━━ 診断 (Diagnosis) ━━━
1. 推定原因: MEM-LEAK-01 — mysqld にメモリリークの疑い [確信度: 高]
     根拠: rss_avg の線形回帰 slope=+38MB/h, R²=0.97, 観測18h
     ...(5部構成: 検出→根拠→暫定対処→恒久対処→影響予測)
```

---

## 3. パッケージ構成

netdiag のフラットレイアウト(リポジトリ直下にパッケージ、src/ なし)に揃える。
右コメントは netdiag の対応物。

```
host-check-tool/
├── pyproject.toml              # Poetry。netdiag と同形式 (§9)
├── README.md                   # 2ツール構成の説明スタイルを踏襲
├── docs/
│   └── procdiag.md             # ← docs/netdiag.md (設計+コマンドリファレンス)
├── examples/
│   └── factory-edge.config.py  # ← examples/office.yaml (現場向け設定例)
├── procdiag/
│   ├── __init__.py             # __version__
│   ├── cli.py                  # ← netdiag/cli.py (click。コマンド本体は薄く)
│   ├── config.py               # Pydanticモデル群 (設計書§8)。netdiagのscenario相当
│   ├── core/                   # ← netdiag/core
│   │   ├── __init__.py         #    再エクスポート (Finding, Rule, Sampler, ...)
│   │   ├── result.py           #    ← core/result.py: Finding, Severity, Confidence, Diagnosis
│   │   ├── rule.py             #    ← core/probe.py: Rule基底 + @register + レジストリ
│   │   ├── sampler.py          #    Sampler基底 + レジストリ (同じ書式でもう1系統)
│   │   └── util.py             #    ← core/util.py: bytes/期間の整形, cmdline正規化, 期間文字列パース
│   ├── samplers/               # ← netdiag/probes: 事実の収集。解釈はしない
│   │   ├── __init__.py         #    importで組込みサンプラーを登録 (netdiagと同じ手口)
│   │   ├── system.py           #    T1: psutil全体値 + PSIパーサ (設計書 tier1_system)
│   │   ├── cgroup.py           #    T2a: cgroup v2リーダー (tier2a_cgroup)
│   │   ├── process.py          #    T2b: Processキャッシュ + PSS精査 (tier2b_process)
│   │   ├── disk.py             #    T3: statvfs + watch_dirs走査 (tier3_disk)
│   │   ├── trigger.py          #    PSI閾値監視・デバウンス・ストーム集約 (← alerting.pyのStateMachine相当)
│   │   └── selfmeter.py        #    自己計測・自動減速 (§4.8)
│   ├── recorder.py             # ← netdiag/watch.py: 常駐本体。samplers+trigger+storageの結線、シグナル処理
│   ├── scheduler.py            # ← netdiag/scheduler.py: monotonic基準の多重間隔ループ (§4.7)
│   ├── storage.py              # ← netdiag/storage.py: スキーマ+バッチライタ+ロールアップ+保持 (§5)
│   ├── diagnosis/              # ← netdiag/diagnosis
│   │   ├── __init__.py
│   │   ├── engine.py           #    ← engine.py: 登録ルールをDBに対して実行 → Diagnosis
│   │   ├── trend.py            #    ← baseline.py + changepoint.py: 線形回帰/外挿/周期検出
│   │   ├── memory.py           #    MEM-*, SWAP-*   (設計書 rules/memory.py)
│   │   ├── cpu.py              #    CPU-*
│   │   ├── io_disk.py          #    IO-*, DISK-*
│   │   ├── leak.py             #    FD-*, THREAD-*, ZOMBIE-*
│   │   └── templates/          #    提案文Jinja2 (ルールIDごと、5部構成) ← RUNBOOKS相当
│   ├── report.py               # ← netdiag/report.py: render_terminal / render_json / render_html
│   ├── export.py               # CSV/JSONエクスポート
│   ├── service.py              # systemdユニット生成 (netdiagはcli内、量が多いので分離)
│   └── plugins.py              # ← netdiag/plugins.py: entry point "procdiag.rules" 読込み
└── tests/                      # netdiag同様フラット + fixtures/
    ├── fixtures/               # 擬似 /proc・/sys/fs/cgroup ツリー、合成DB生成ヘルパ
    ├── test_trend.py           # ← test_timeseries.py (回帰・外挿・周期の純関数)
    ├── test_samplers.py        # 擬似ツリー注入でのパーサ検証 (PSI, cgroup, cmdline正規化)
    ├── test_trigger.py         # ← test_alerting.py (デバウンス・ストーム集約)
    ├── test_engine.py          # ← test_engine.py (合成DBでルールの検出/非検出 両側)
    ├── test_retention.py       # ← test_retention.py (ロールアップ・保持削除)
    └── test_e2e.py             # record --duration 60s → check/report 完走 + 自己負荷予算内
```

設計書 §9 との差分は「§7 設計書からの変更点」にまとめる。

### モジュール間の依存方向(netdiag と同じ一方向)

```
samplers ──▶ storage ◀── diagnosis ──▶ report
   ▲            ▲            ▲
   └─ recorder ─┘            └─ cli (組立てのみ)
        ▲
     scheduler
```

- samplers は storage の行型だけ知る(診断を知らない)
- diagnosis は storage の読み口だけ知る(サンプラーを知らない)
- report は Diagnosis だけ知る(DBを直接読まない。チャート用系列は engine が併せて返す)
- cli は組立てと入出力のみ。ロジックを持たない(netdiag の cli.py と同じ薄さ)

---

## 4. コアデータモデル(コードスケッチ)

### 4.1 core/result.py — Finding / Diagnosis(← CheckResult / Hypothesis / Diagnosis)

設計書 §6.1 の Finding をベースに、netdiag の語彙・補助APIを合わせる:

```python
class Severity(str, Enum):          # ← netdiag Status と同じ str mix-in 方式
    INFO = "info"; WARNING = "warning"; CRITICAL = "critical"
    @property
    def symbol(self) -> str:        # ✔⚠✘ ではなく深刻度記号: ℹ/⚠/✘

@dataclass
class Finding:                      # 設計書 §6.1 のフィールドは全維持
    rule_id: str                    # "MEM-LEAK-01"
    severity: Severity
    subject: str                    # プロセス名 / ユニット名 / マウントポイント
    summary: str                    # 1行サマリ (ターミナルのヘッドライン用)
    evidence: dict                  # 根拠数値 (JSON化可能)
    detected_at: tuple[float, float]
    confidence: str                 # high/medium/low — netdiagと同語彙
    related: list[str] = field(default_factory=list)  # ← Hypothesis.consequences 相当
    def to_dict(self) -> dict: ...

@dataclass
class Diagnosis:                    # ← netdiag Diagnosis と同形
    findings: list[Finding]
    healthy: bool
    reasoning: list[str]            # --verbose 用の推論過程
    quality: ObservationQuality     # 観測品質 (§7.2): 欠損率・gap・自己負荷・減速履歴
    @property
    def primary(self) -> Finding | None: ...
```

### 4.2 core/sampler.py — Sampler 基底(← core/probe.py の Probe)

```python
class Sampler:
    name = "system"                  # ← Probe.name
    title = "システム全体"             # ← Probe.title
    tier = "T1"                      # ← Probe.layer (T1/T2a/T2b/T3)
    interval_key = "system_interval" # SamplingConfig のどの間隔で走るか
    requires_root = False            # ← Probe.requires_root (降格の説明に使用)
    what = "CPU/メモリ/スワップ/PSI を1秒ごとに記録する"   # ← explain 用 3点セット
    why  = "PSI は『誰かが資源を待った時間』そのもので、飽和検出の一次情報になるため"
    manual_commands = ["cat /proc/pressure/cpu", "vmstat 1"]

    def setup(self, ctx: RunContext) -> None: ...       # record開始時1回 (能力検出・降格判定)
    def collect(self, ctx: RunContext) -> list[Row]: ...# 1周期分の行を返す。解釈しない

    def run(self, ctx) -> list[Row]:                    # ← Probe.run と同じ防護壁:
        ...                                             # 例外は欠損記録に降格し、ループを殺さない
```

netdiag との意図的な相違: probe は実行ごとに使い捨てだが、**sampler インスタンスは
record 中ずっと生きる**(Tier2b の Process キャッシュ、cgroup 台帳、PSI 累積値の差分計算が
インスタンス状態を要するため)。`RunContext` が config・`proc_root`/`cgroup_root`
(テスト時に擬似ツリーへ差替え)・台帳キャッシュを運ぶ — netdiag の `Context` に対応。

### 4.3 core/rule.py — Rule 基底 + レジストリ(← Probe のレジストリ機構)

```python
class Rule:
    rule_id = "MEM-LEAK-01"          # ← Probe.name に相当する一意キー
    category = "memory"              # list-rules の分類軸 (← layer)
    title = "メモリリーク疑い"
    default_severity = Severity.WARNING
    min_observation_hours = 6.0      # 誤検知抑制 (§6.3)。config で上書き可
    what = "RSSの1分ロールアップに線形回帰をかけ、増加傾向と決定係数で判定する"
    why  = "リークは『右肩上がりが続く』こと自体が証拠であり、瞬時値では判定できないため"
    manual_commands = ["watch -n 60 'ps -o rss=,vsz= -p <PID>'", "smem -P <name>"]

    def evaluate(self, store: Storage, window: Window) -> list[Finding]: ...

_REGISTRY: dict[str, type[Rule]] = {}
def register(cls): ...               # ← core/probe.py の @register と同一パターン
def get_rule(rule_id): ...
def all_rules(): ...
```

`plugins.py` は netdiag のものをグループ名だけ変えて流用
(`[project.entry-points."procdiag.rules"]`)。config の `rules.extra_rules` は
import パス指定によるローカル拡張で、両方とも「import すれば @register が効く」方式。

### 4.4 diagnosis/engine.py(← netdiag diagnosis/engine.py)

netdiag の Engine は probe を依存順に走らせるが、procdiag のルールは互いに独立なので
トポロジカルソートは不要。代わりに netdiag の以下の規約を引き継ぐ:

- `select(only, skip)` — config の `rules.disabled` とCLIの `--only/--skip` で絞り込み
- 1ルールの例外は捕捉して warning 化し、診断全体を殺さない(← Probe.run の防護壁)
- **因果の折りたたみ**: 同一パーティションの DISK-GROWTH-01 を DISK-FULL-01 の
  `related` に畳み、ヘッドラインを1つにする(← root cause / consequences の写像)。
  CPU-SINGLECORE-01 の py-spy 提案付記も同じ仕組みで実装
- findings は severity → confidence → 影響量の順でソートし、`primary` が先頭
- `--verbose` で reasoning(各ルールの判定値: slope, R², 観測時間)を蓄積

### 4.5 diagnosis/trend.py(← baseline.py + changepoint.py)

stdlib `statistics` のみ・純関数・dataclass 戻り値、という netdiag の流儀を踏襲:

```python
@dataclass
class TrendFit:                      # ← Baseline / ChangePoint に相当する結果型
    slope_per_hour: float
    r2: float
    n: int
    span_hours: float

def fit_linear(series) -> TrendFit | None          # MEM-LEAK / FD-LEAK / THREAD-LEAK
def extrapolate_full(series, capacity) -> float | None   # DISK-FULL: 残り日数
def detect_periodicity(timestamps, tol_sec=120) -> Period | None  # CPU-PERIODIC: 日次/時次反復
def detect_recurrence(daily_windows) -> float      # MEM-PRESSURE: 日次反復率
```

最低サンプル数未満は None(発火しない)— `compute_baseline` が n<5 で None を返すのと同じ
「データ不足は沈黙」規約。

---

## 5. 実行フロー

### 5.1 `procdiag record`(← netdiag watch の常駐モデルを多層化)

```
cli.record
 └─ Recorder(config)                       # ← Watcher と同役割
     ├─ Storage(db_path)  … WALモード, バッチライタ
     ├─ samplers = [System(T1,1s), Cgroup(T2a,5s), Process(T2b,10s), Disk(T3,60s/1h)]
     ├─ SpikeTrigger(閾値, cooldown, storm) # T1の結果を毎周期 observe()
     │    └─ 発火 → T2a+T2b を臨時実行 + spike_events へ即時フラッシュ
     │      (← watchの「確定劣化時だけ重いフル診断を1回」と同じ思想)
     ├─ SelfMeter … 自己CPU監視 → 予算超過でT2b間隔を1.5倍 (ops_eventsに記録)
     ├─ Scheduler.run() … monotonic基準 next_t += interval、gap検出 (§4.7)
     │    └─ 60秒ごと: バッチフラッシュ → 増分ロールアップ → (1時間ごと) 保持削除
     │      (← Watcher._maybe_maintain と同じ「常駐が自分でディスクを律する」)
     └─ SIGTERM/SIGINT → フラッシュして exit 0 (systemd stop と整合)
```

### 5.2 `procdiag check` / `report`(← netdiag check)

```
cli.check
 ├─ Storage(db_path, read_only)
 ├─ Engine(config).run(store, only=…, skip=…) → Diagnosis(findings, quality, reasoning)
 ├─ report.render_terminal / render_json / render_html   # --format で選択
 └─ exit 0/1/2
cli.report = check の薄いラッパ: render_html → report.html と render_json → report.json を一括出力
```

---

## 6. storage.py / report.py の形

**storage.py**: スキーマ・ロールアップ仕様は設計書 §5 を一字も変えない。クラスAPIの
形だけ netdiag の Storage に合わせる:

```python
class Storage:                       # ← netdiag Storage と同じ外形
    def __init__(self, path, read_only=False): ...   # スキーマはexecutescript+metaのversion
    def __enter__ / __exit__ / close(): ...
    # 書き込み (recorder専用): バッファに積み、flush()で1トランザクション
    def buffer(self, table: str, rows: list[Row]): ...
    def flush(self, *, reason: str = "interval"): ...     # spike/シグナル時は即時
    def rollup_incremental(self): ...                     # ← downsample() に相当
    def retention(self, raw="48h", rollup="90d") -> dict: # ← retention() と同名同役割
    # 読み出し (diagnosis/report/export用):
    def series(self, table, column, *, subject=None, since=None): ...  # ← series() と同名
    def counts(self) -> dict: ...                         # ← counts() (statusコマンドが使用)
```

**report.py**: 関数名・引数を netdiag と同一にする。

```python
def render_terminal(diag, color=True, verbose=False) -> str   # 結論ファースト+5部構成
def render_json(diag) -> str                                  # report.json (Finding全件+統計+quality)
def render_html(diag, charts: ChartData) -> str               # 単一HTML, インラインCSS+SVG, CDNなし
```

HTML の「観測品質」セクション(§7.2)は netdiag の SKIP 理由表示の発展形として必須実装。
チャートはサーバーサイドSVG生成(設計書 §12 の推奨案を採用。netdiag の「外部資源ゼロ」
原則と一致するため)。

---

## 7. 設計書からの変更点と判断

設計書を正とし、netdiag アナロジーのために変える点だけを列挙する。

| # | 設計書 | 本案 | 理由 |
|---|---|---|---|
| 1 | CLI は argparse(依存最小化) | **click** | netdiag の CLI 規約そのもの(あちらは「clickが唯一のハード依存」)。psutil+pydantic+jinja2 を既に積む procdiag で argparse にする節約効果は小さく、コマンド群・サブグループ(`service`)の書き味を netdiag と完全に揃える価値が勝る |
| 2 | `src/procdiag/` レイアウト | リポジトリ直下 `procdiag/` | netdiag のフラットレイアウトに一致させる |
| 3 | `advise` コマンド | `check`(`advise` はエイリアスで維持) | スイートの中心動詞を `check` に統一。終了コード 0/1/2 の意味も netdiag と同義に |
| 4 | `install-service` | `service print` / `service install` | netdiag のグループ構成に一致。`print`(stdout)があると閉域網で「別ホストへ持っていく」運用が楽になる実利もある |
| 5 | `sampler/` 単数・tier番号ファイル名 | `samplers/` 複数形・対象名ファイル (system/cgroup/process/disk) | `probes/` の命名規約(複数形・対象名)。tier はクラス属性に |
| 6 | `rules/base.py` に基底+Finding | `core/rule.py` + `core/result.py` | netdiag の core/(データモデルと基底)と diagnosis/(解釈ロジック)の分離に一致 |
| 7 | `storage/db.py` + `storage/rollup.py` | 単一 `storage.py` | netdiag は storage.py 1ファイル。スキーマ定数+ライタ+ロールアップで肥大したら(目安600行超)パッケージ化に切替えてよい |
| 8 | `report/html.py`+`json_out.py` | 単一 `report.py`(render_* 3関数) | netdiag report.py と同形。Jinja2テンプレートは `diagnosis/templates/`(提案文)と `report.py` 内HTML雛形に整理 |
| 9 | テストを unit/fixtures/integration に分割 | フラット `tests/test_*.py` + `fixtures/` | netdiag のテスト命名(test_engine/test_retention/test_alerting/test_timeseries)に対応させる。区分は pytest マーカー(`@pytest.mark.e2e`)で表現 |
| 10 | (コマンドなし) | `explain` / `list-rules` / `list-samplers` / `prune` を追加 | netdiag の教育モード・一覧・手動保持に対応。explain 用の what/why/manual_commands を Sampler/Rule の基底属性として持つ(Probe と同じ場所) |
| 11 | パッケージ名 procdiag(PyPI要確認) | 配布名 `host-check-tool` / import・コマンド名 `procdiag` | network-check-tool ↔ netdiag の関係を再現。両名ともPyPI未登録を確認済み(2026-06-11) |
| 12 | ビルド指定なし | Poetry(poetry-core)+ black(line-length 88, py310)+ black-formatter CI | netdiag リポジトリの開発規約一式を踏襲 |

変更しないもの(設計書のまま): サンプリング3層+トリガの全仕様(§4)、DDL・バッチ書込・
ロールアップ・保持(§5)、13ルールのカタログと判定ロジック(§6.2)、誤検知抑制(§6.3)、
レポート内容と観測品質(§7)、config.py の Pydantic 仕様(§8)、非目標(§1.4)。

---

## 8. pyproject.toml スケッチ

```toml
[tool.poetry]
name = "host-check-tool"
version = "0.1.0"
description = "Local-first Linux host resource diagnosis (procdiag): record CPU/memory/disk over time, then explain what to fix."
license = "MIT"
packages = [{ include = "procdiag" }]
include = ["procdiag/diagnosis/templates/*", "procdiag/report/templates/*"]

[tool.poetry.dependencies]
python = "^3.10"
psutil = "^7.0"
pydantic = "^2.7"
jinja2 = "^3.1"
click = "^8.1"

[tool.poetry.scripts]
procdiag = "procdiag.cli:main"      # ← netdiag = "netdiag.cli:main"

[tool.poetry.group.dev.dependencies]
pytest = "^8.3"
black = "^24.0"

[tool.black]
line-length = 88
target-version = ["py310"]
```

---

## 9. マイルストーン(設計書 §11 を本構成に合わせて改訂)

| M | 内容 | 完了条件(各M独立にテスト可能) |
|---|---|---|
| M1 | 骨格: pyproject(Poetry)、`cli.py` 全コマンドのスタブ、`config.py`(Pydantic)、`storage.py` スキーマ、`init` | `procdiag init` が config 雛形を生成し、`--help` が全コマンドを示す |
| M2 | 記録の核: `scheduler.py` + `samplers/system.py`(PSI込み)+ バッチライタ + `selfmeter` + `recorder.py` | `record --duration 60s` が完走し `status` で概況が出る |
| M3 | 多層化: `samplers/cgroup.py`・`process.py`(台帳+キャッシュ)+ `trigger.py`(スパイク) | 擬似ツリーfixtureで T2a/T2b パーサと発火/デバウンスのテストが通る |
| M4 | ディスクと保持: `samplers/disk.py` + 増分ロールアップ + 保持削除 + `prune` | `test_retention.py` が通り、48h相当の合成データでDB容量が見積り内 |
| M5 | 診断: `core/rule.py`・`result.py` + `diagnosis/engine.py`・`trend.py` + 優先4ルール + 5部構成テンプレ + `check`(text/json, exit code)+ `explain`/`list-rules` | 合成DB(リークあり/なし)で検出・非検出の両側テストが通る |
| M6 | レポート: `report.py` の render_html(SVGチャート)+ 観測品質 + `report` コマンド | 単一HTMLがオフラインで開け、外部参照ゼロを機械的に検査 |
| M7 | 完成度: 残り9ルール + `export` + `service print/install` + `plugins.py` | 13ルール全件のテスト、entry pointでのルール追加が動く |
| M8 | 仕上げ: 500プロセス負荷テスト、README(2ツール構成の体裁)、`docs/procdiag.md`、black CI | 自己負荷が予算(1%)内、ドキュメントがコマンドリファレンスを網羅 |

---

## 10. 残る判断点(実装中に決める・軽微)

- [ ] `check` のデフォルトDBパス: 設計書は `--db data.db`。カレントの `./procdiag.db` →
      なければ `config.py` の値、の順で解決する案(netdiag の `~/.netdiag/history.db` 固定とは
      用途が違うため、config 優先を維持)
- [ ] `explain` の対象名前空間: ルールID(大文字)とサンプラー名(小文字)で衝突しないため
      単一コマンドで両対応にできる見込み。一覧表示時は2セクションに分ける
- [ ] cgroup v1: 設計書どおり v1.0 はスキップ+警告(ops_events に記録し観測品質に表示)
- [ ] `network-check-tool` の docker/ 相当(コンテナ配布)は需要が出てから
