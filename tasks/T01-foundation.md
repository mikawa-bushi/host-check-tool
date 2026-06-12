# T01: 基盤 — プロジェクト骨格と契約層

フェーズ: P1(直列。本タスクの成果物に P2 の7タスクが全員依存する)
参照: `tasks/README.md`(共通規約)を先に読むこと。
設計書 §2.1, §5, §6.1, §8 / 実装計画 §3, §4, §6, §8 / netdiag 参照:
`/tmp/network-check-tool/netdiag/core/probe.py`, `core/result.py`, `storage.py`, `plugins.py`, `pyproject.toml`

## 目的

procdiag パッケージの骨格と「契約層」(全タスクが import する型・基底クラス・ストレージAPI)を
作る。**ここで定義するシグネチャ・列順は後続7タスクの前提**なので、本書の契約を一字一句守る。
迷ったら設計書と本書を正とし、実装の自由はモジュール内部に限る。

## 所有ファイル(これ以外に触れない)

```
pyproject.toml
.gitignore
procdiag/__init__.py
procdiag/core/__init__.py
procdiag/core/result.py
procdiag/core/rule.py
procdiag/core/sampler.py
procdiag/core/util.py
procdiag/config.py
procdiag/storage.py
procdiag/plugins.py
procdiag/samplers/__init__.py      # 空 (docstringのみ。eager importはT09が書く)
procdiag/diagnosis/__init__.py     # 空 (同上)
tests/conftest.py                  # 最小限 (tmp_db fixture程度)
tests/test_core.py
tests/test_config.py
tests/test_storage.py
tests/test_retention.py
```

## 成果物仕様

### 1. pyproject.toml
実装計画 §8 のスケッチをそのまま使う(Poetry / poetry-core / scripts:
`procdiag = "procdiag.cli:main"` / black 設定込み)。作成後に1回だけ実行:
`.venv/bin/pip install -e . --no-deps`
注意: `procdiag/cli.py` は T08 が作るため、この時点で import できなくてよい
(scripts のエントリポイントは遅延解決なので editable install は成功する)。

### 2. procdiag/core/result.py — 診断の出力型
```python
class Severity(str, Enum):    # netdiag Status と同じ str mix-in 方式
    INFO = "info"; WARNING = "warning"; CRITICAL = "critical"
    # .symbol プロパティ: INFO="ℹ", WARNING="⚠", CRITICAL="✘"

class Confidence(str, Enum):
    LOW = "low"; MEDIUM = "medium"; HIGH = "high"

@dataclass
class Window:                 # ルール評価の対象期間
    start_ts: float
    end_ts: float
    # .span_hours プロパティ

@dataclass
class Finding:                # 設計書 §6.1 + 実装計画 §4.1
    rule_id: str              # 例 "MEM-LEAK-01"
    severity: Severity
    subject: str              # プロセス名/ユニット名/マウントポイント
    summary: str              # 1行サマリ (日本語)
    evidence: dict            # JSON化可能な根拠データ
    detected_at: tuple[float, float]
    confidence: Confidence
    related: list[str] = field(default_factory=list)   # 折りたたんだ関連Finding ID
    proposal: str = ""        # 5部構成の提案文 (Jinja2レンダリング結果。T06が埋める)
    def to_dict(self) -> dict: ...   # Enumは .value に展開

@dataclass
class ObservationQuality:     # レポート「観測品質」節 (設計書 §7.2)
    span_hours: float = 0.0
    gap_seconds: float = 0.0
    missing_io_pct: float = 0.0          # io_counters欠損率
    self_cpu_avg: float = 0.0
    self_cpu_max: float = 0.0
    throttle_events: int = 0
    notes: list[str] = field(default_factory=list)   # 降格理由など
    def to_dict(self) -> dict: ...

@dataclass
class Diagnosis:              # netdiag Diagnosis と同形
    findings: list[Finding]
    healthy: bool
    reasoning: list[str] = field(default_factory=list)
    quality: ObservationQuality = field(default_factory=ObservationQuality)
    @property
    def primary(self) -> Finding | None: ...   # findings[0] or None
```

### 3. procdiag/core/rule.py — Rule 基底とレジストリ
netdiag `core/probe.py` の Probe + registry をルール用に写像(実装計画 §4.3):
```python
class Rule:
    rule_id: str = ""
    category: str = ""             # "memory" | "cpu" | "io_disk" | "leak"
    title: str = ""
    default_severity = Severity.WARNING
    min_observation_hours: float = 6.0
    what: str = ""                 # explain用: 何を検出するか (日本語)
    why: str = ""                  # explain用: なぜその判定法か (日本語)
    manual_commands: list[str] = []

    def evaluate(self, store, window) -> list[Finding]:   # 抽象。storeはStorage
        raise NotImplementedError

# レジストリ (netdiagと同一パターン): _REGISTRY / @register / get_rule / all_rules
```

### 4. procdiag/core/sampler.py — Sampler 基底・RunContext・共有型
実装計画 §4.2。**ここの型は T02〜T05 の結合点**:
```python
@dataclass
class SampleBatch:
    table: str                # storage.TABLE_COLUMNS のキー
    rows: list[tuple]         # TABLE_COLUMNS[table] の列順に一致するタプル列

@dataclass
class SystemSnapshot:         # T1の型付きビュー (トリガ判定・自己計測が消費)
    ts: float
    cpu_total: float          # 0-100
    cpu_max_core: float       # 0-100
    mem_total: int            # bytes
    mem_available: int        # bytes
    swap_out_delta: int       # 前回collectからのスワップアウト増分 (bytes)
    psi: dict[str, float] | None
    # psiキー: "cpu_some10","mem_some10","mem_full10","io_some10","io_full10"
    # PSI非対応カーネルでは None

class RunContext:
    """1回のrecordセッションで全サンプラーが共有する状態。"""
    def __init__(self, config, *, proc_root=Path("/proc"),
                 cgroup_root=Path("/sys/fs/cgroup")) -> None:
        self.config = config
        self.proc_root = proc_root        # テストで擬似ツリーに差替え可能
        self.cgroup_root = cgroup_root
        self.capabilities: dict[str, bool] = {}  # "psi","cgroup_v2","root" 等。setup()群が埋める
        self.notes: list[str] = []        # 降格理由 (観測品質へ転記される)
        self.selfmeter = None             # SelfMeter | None。recorderが装着、T1サンプラーが参照

class Sampler:
    name: str = ""
    title: str = ""
    tier: str = ""                # "T1" | "T2a" | "T2b" | "T3"
    interval_key: str = ""        # SamplingConfig の属性名 (例 "system_interval")
    requires_root: bool = False
    what: str = ""; why: str = ""; manual_commands: list[str] = []

    def setup(self, ctx: RunContext) -> None: ...       # 既定: 何もしない
    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        raise NotImplementedError
    def run(self, ctx: RunContext) -> list[SampleBatch]:
        # netdiag Probe.run と同じ防護壁: 例外を捕捉し ctx.notes に
        # f"{self.name}: collect失敗 ({e})" を追記して [] を返す

# レジストリ: rule.py と同一パターン (@register_sampler / get_sampler / all_samplers)
# ※ rule側と関数名が衝突しないよう register_sampler とする。core/__init__ で再エクスポート
```

### 5. procdiag/core/util.py
```python
def parse_duration(s: str) -> float        # "60s"/"24h"/"90d"/"1.5h" → 秒。不正は ValueError(日本語)
def humanize_bytes(n: int | None) -> str   # 1234567 → "1.2MB"
def humanize_duration(sec: float) -> str   # 93784 → "1日2時間"
def normalize_cmdline(cmdline: list[str], *, replace_digits: bool = True) -> tuple[str, str]
    # 戻り: (sha1先頭12桁hash, 表示用サンプル文字列)。設計書 §4.4:
    # replace_digits時は連続数字列を "#" に置換してからhash (一時引数による台帳爆発防止)
```

### 6. procdiag/config.py — Pydantic 設定モデル
設計書 §8 のモデル群をそのまま実装: `ProcdiagConfig`, `SamplingConfig`, `WatchDir`,
`TriggerConfig`, `BudgetConfig`, `RetentionConfig`, `RulesConfig`, `ReportConfig`。
全フィールドに設計書の既定値。検証エラーは日本語で分かるよう
`model_config = ConfigDict(...)` + バリデータで補う(間隔は正数、retentionは
parse_duration で検証等)。加えて:
```python
DEFAULT_CONFIG_PATH = Path("./config.py")
CONFIG_TEMPLATE: str        # `procdiag init` が書き出す雛形 (設計書§8の例+日本語コメント)
def load_config(path: Path | None = None) -> ProcdiagConfig
    # path省略: ./config.py があれば読む、なければ既定値のProcdiagConfig()
    # 読み方: ファイルを exec し、名前空間の `config` 変数を取得 (設計書§8の書式)
    # 失敗時: ValueError に日本語メッセージ
```

### 7. procdiag/storage.py — スキーマ・バッチライタ・ロールアップ・保持
netdiag `storage.py` の流儀(stdlib sqlite3・`_SCHEMA` 定数・context manager)で、
設計書 §5.2 の DDL を**一字一句**使う(`procdiag_design.md` からコピーする。
sys_rollup_1m / proc_rollup_1m に加え cg_rollup_1m も同様に定義し、列は
`ts_min, cg_id, cpu_usec_sum, mem_avg, mem_max, io_rbytes_sum, io_wbytes_sum` とする)。

```python
SCHEMA_VERSION = 1
TABLE_COLUMNS: dict[str, list[str]]
# 全テーブルの列名リスト (DDLの列順そのまま)。SampleBatch.rows のタプル順契約として
# 他タスクが import して参照する

class Storage:
    def __init__(self, path, *, read_only: bool = False): ...
        # WALモード, synchronous=NORMAL。新規作成時は meta に schema_version を記録
        # read_only時: sqlite3 URI mode=ro で開く
    # --- 書き込み (recorder専用) ---
    def buffer(self, table: str, rows: list[tuple]) -> None      # メモリリングバッファに蓄積
    def flush(self, *, reason: str = "interval") -> int          # 1トランザクションで全バッファ書込
        # 戻り: 書いた行数。reason "spike"/"signal" は ops_events にも flush イベントを記録
    def rollup_incremental(self) -> None
        # meta["rollup_watermark"] 以降の完了済み「分」を sys/proc/cg_rollup_1m に集計し
        # watermark を進める。p95はPython側でソートして算出 (1分=最大60サンプルで軽量)
    def retention(self, raw: str = "48h", rollup: str = "90d") -> dict
        # core.util.parse_duration で解釈し、生サンプル(sys/cg/proc_samples)と
        # ロールアップ+disk/dir_samplesの期限切れをDELETE。戻り {"raw_deleted": n, "rollup_deleted": m}
    def record_event(self, kind: str, detail: str = "") -> None  # ops_eventsへ即時INSERT
    def set_meta(self, key: str, value: str) -> None
    # --- 読み出し ---
    def meta(self) -> dict[str, str]
    def series(self, table: str, column: str, *, subject_col: str | None = None,
               subject=None, since: float | None = None,
               until: float | None = None) -> list[tuple[float, float]]
        # (ts列, column) のペア列。tsの列名は sys_rollup_1m だけ "ts_min" なので内部で吸収
    def rows(self, table: str, *, since=None, until=None,
             where: str | None = None, params: tuple = ()) -> list[sqlite3.Row]
    def counts(self) -> dict      # テーブル別行数 + "db_bytes" (ファイルサイズ)
    def close(self) / __enter__ / __exit__
```
プロセス・cgroup台帳のupsertヘルパも提供する(T03が使う):
```python
def proc_id_for(self, name: str, cmdline_hash: str, cmdline_sample: str, ts: float) -> int
    # proc_registry に UNIQUE(name,cmdline_hash) でupsertし、last_seen更新、proc_id を返す
def cg_id_for(self, unit_path: str, ts: float) -> int   # 同様に cgroup_registry
```
台帳lookupは内部dictキャッシュで毎回SQLを打たないこと(10秒毎に全プロセス分呼ばれる)。

### 8. procdiag/plugins.py
netdiag `plugins.py` をグループ名 `procdiag.rules` に変えてほぼ流用。加えて
`load_extra_rules(import_paths: list[str])`(config の `rules.extra_rules` 用、
importlib で import するだけ)を追加。

### 9. procdiag/__init__.py
`__version__ = "0.1.0"` のみ(重い import をしない)。
core/__init__.py は result/rule/sampler の公開名を再エクスポート
(netdiag `core/__init__.py` 参照)。

## テスト要求

- `test_core.py`: Severity/Finding.to_dict のJSON化、Sampler.run の防護壁(例外→notes+[])、
  レジストリ登録、util群(parse_duration 正常/異常、normalize_cmdline の数字置換と安定性)
- `test_config.py`: 既定値で検証が通る、不正値(負の間隔等)が日本語エラーで落ちる、
  load_config がファイル無し→既定値/雛形ファイル→読める(tmp_pathにCONFIG_TEMPLATEを書いて読む)
- `test_storage.py`: buffer→flush で行が入る(全テーブル最低1回)、TABLE_COLUMNSと
  実テーブルのPRAGMA table_info が全テーブルで一致、series/rows/counts/meta、
  proc_id_for のupsert挙動(同一キー再呼出で同ID・last_seen更新)
- `test_retention.py`: 合成データ(数時間分のsys_samples+proc_samples)を入れ、
  rollup_incremental で1分集計(avg/max/p95)が正しい、retention("1h","2h") で期限切れだけ消える

## 完了条件
`.venv/bin/python -m pytest tests/ -q` 全緑 + `pip install -e . --no-deps` 済み
+ black 適用 + 最終報告(共通規約のフォーマット)。
