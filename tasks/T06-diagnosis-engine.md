# T06: diagnosis/ — エンジン・トレンド統計・優先4ルール・提案テンプレート

フェーズ: P2(並列)/ 依存: T01
参照: `tasks/README.md` → 設計書 §6 全節, §7.2 / 実装計画 §4.3〜§4.5
netdiag 参照: `/tmp/network-check-tool/netdiag/diagnosis/engine.py`, `rules.py`,
`baseline.py`, `changepoint.py`(エンジンと統計の流儀の手本)

## 所有ファイル(これ以外に触れない)

```
procdiag/diagnosis/engine.py
procdiag/diagnosis/trend.py
procdiag/diagnosis/memory.py
procdiag/diagnosis/cpu.py
procdiag/diagnosis/io_disk.py
procdiag/diagnosis/leak.py            # 空スタブ (docstring + 将来ルールのプレースホルダ)
procdiag/diagnosis/templates/*.j2
tests/test_trend.py
tests/test_engine.py
tests/synth.py                        # 合成DBビルダ (P4の3タスクも再利用する共有資産)
```

注意: `procdiag/diagnosis/__init__.py` は T01 が空で作成済み。**本タスクで上書きし**、
engine/trend の公開名 re-export と、memory/cpu/io_disk/leak の eager import
(ルール登録のため)を書く。P4 はこのファイルに触れない前提なので、4モジュール
すべてを import しておくこと(leak.py が空でも import 可能に)。

## 契約(T01 提供 — 変更禁止)

- `procdiag.core`: `Rule`, `register`, `all_rules`, `Finding`, `Severity`, `Confidence`,
  `Window`, `Diagnosis`, `ObservationQuality`
- `procdiag.storage.Storage`: `series()`, `rows()`, `meta()`, `counts()`
- Engine の外部契約(T08 の cli が遅延importで呼ぶ):
  ```python
  class Engine:
      def __init__(self, config: ProcdiagConfig, *, verbose: bool = False): ...
      def select(self, only: list[str] | None = None,
                 skip: list[str] | None = None) -> list[type[Rule]]
      def run(self, store: Storage, only=None, skip=None) -> Diagnosis
  ```

## 実装要求

### trend.py — 純関数統計(stdlib statistics のみ)
実装計画 §4.5 の4関数+結果dataclass。netdiag baseline/changepoint と同じ
「データ不足は None で沈黙」規約:
```python
@dataclass
class TrendFit:
    slope_per_hour: float; r2: float; n: int; span_hours: float

def fit_linear(series: list[tuple[float, float]], *, min_points: int = 12,
               min_span_hours: float = 6.0) -> TrendFit | None
    # 最小二乗。R²も算出。点数/期間不足→None
def extrapolate_full(series, capacity: float, *, min_points=12) -> float | None
    # used系列を線形外挿し capacity 到達までの残り日数。傾き<=0→None。
    # 直近7日分のデータがあればそちらを優先して傾きを取る (設計書 DISK-FULL-01)
def detect_periodicity(timestamps: list[float], *, tol_sec: float = 120.0,
                       min_events: int = 3) -> Period | None
    # @dataclass Period: kind ("daily"|"hourly"), anchor (代表時刻文字列), count
    # 日次: 時刻(時:分)が±tol内に集まる日が3日以上 / 時次: 分が±tol内で3時間以上
def detect_recurrence(events_by_day: dict[str, bool]) -> float
    # 該当日割合 (MEM-PRESSURE用の単純ヘルパ)
```

### engine.py
netdiag Engine の写像(実装計画 §4.4。ルールは独立なのでトポソート不要):
- `select`: `all_rules()` − `config.rules.disabled` を適用、only/skip はrule_id指定
- `run(store)`:
  1. Window を DBの sys_samples min/max ts(無ければ rollup)から決定
  2. `config.rules.min_observation_hours` 未満なら findings 空 + reasoning に
     「観測時間不足(X.Xh < 6.0h)」を載せ healthy=True で返す
  3. 各ルール `evaluate(store, window)` を try/except で実行(失敗は reasoning に記録、
     診断は続行 — netdiag の防護壁)。verbose 時は各ルールの判定値を reasoning へ
  4. **折りたたみ**: DISK-GROWTH-01 の Finding と同一パーティションの DISK-FULL-01 が
     共存したら、GROWTH を FULL の `related` に入れ findings から除く(汎用の
     `fold(findings) -> findings` 関数として実装。対応表は定数辞書で拡張可能に)
  5. ソート: severity(critical→info)→ confidence(high→low)
  6. ObservationQuality を組み立てる: span / ops_events の gap 合計 /
     sys_samples.self_cpu の avg・max / throttle系イベント数 / meta・notes
  7. healthy = findings(warning以上)なし
- 各Finding の `proposal` を Jinja2 で描画して埋める(下記テンプレート)

### 提案テンプレート (templates/<RULE-ID>.j2)
設計書 §6.1 の固定5部構成。Environment は `PackageLoader` でなく
`FileSystemLoader(Path(__file__).parent / "templates")` を使う(editable install対応)。
書式(プレーンテキスト、レポート/ターミナル共用):
```
[検出] {{...}}
[根拠] {{...}}
[暫定対処] {{...}}
[恒久対処] {{...}}
[影響予測] {{...}}
```
evidence dict の値を差し込む。4ルール分 + 共通マクロは不要(独立ファイル4枚)。

### 優先4ルール(設計書 §6.2 の判定ロジックを忠実に)
- **memory.py / MEM-LEAK-01**: proc_rollup_1m の rss_avg を proc_id ごとに series →
  fit_linear。slope > 1MB/h(目安・定数化)かつ R²>0.9 かつ span≥min_observation_hours
  → WARNING(slope大きければ CRITICAL)。subject はプロセス名(proc_registry から)。
  confidence: R²と観測時間で high/medium
- **io_disk.py / DISK-FULL-01**: disk_samples を mountpoint ごとに series(used)→
  extrapolate_full(capacity=total)。残り日数 <30 で WARNING、<7 で CRITICAL。
  evidence に days_left / daily_growth_bytes
- **cpu.py / CPU-PERIODIC-01**: spike_events(trigger_type cpu/storm)の ts を
  detect_periodicity → Period があれば INFO/WARNING。evidence に周期と代表時刻、
  proposal に cron/systemd timer 突合の手順
- **cpu.py / CPU-SINGLECORE-01**: sys_rollup_1m で cpu_max_core_max≥95 かつ
  cpu_avg<50 の分が連続15分以上 → 該当時間帯の proc_rollup_1m cpu_max 上位を突合。
  subject は上位プロセス名。プロセス名が python 系なら proposal に py-spy 提案を付記
  (設計書 §6.2)

しきい値はモジュール定数にし、テストから注入可能に(クラス属性でも可)。

### tests/synth.py — 合成DBビルダ(P4再利用の共有資産。設計書 §10-3)
```python
def make_db(tmp_path, *, hours: float = 24.0) -> Storage      # 空スキーマ+meta
def add_sys_baseline(store, *, hours, cpu=10.0, noise=2.0)    # 1分rollup+生を適量
def add_proc_series(store, name: str, *, rss_start, rss_slope_per_h, hours,
                    cpu_pattern=None, fds_slope=0, threads_slope=0)
def add_disk_series(store, mountpoint, *, total, used_start, daily_growth, days)
def add_spike_events(store, trigger_type, times: list[float])
def add_dir_series(store, path, *, start, daily_growth, days)
```
rollupテーブルへ直接行を入れてよい(Storage.rows/seriesで読める形なら実装自由)。
docstringに「P4のルールタスクが共有する。**追記は可・既存シグネチャ変更は不可**」と明記。

## テスト要求

- test_trend.py: fit_linear(きれいな線形+ノイズ/横ばい/データ不足None)、
  extrapolate_full(成長あり/減少None/7日優先)、detect_periodicity(日次/時次/不規則None)
- test_engine.py: synth.py で「リークあり」「リークなし」両側、DISK-FULL の
  残日数境界、PERIODIC/SINGLECORE の検出・非検出、折りたたみ(FULL+GROWTHは
  GROWTH不在のためfold関数の単体テストはフェイクFindingで)、観測時間不足ガード、
  壊れたルール(evaluateがraise)でも診断完走、ObservationQuality が埋まる、
  Finding.proposal に5部構成見出しが全部含まれる

## 完了条件
`.venv/bin/python -m pytest tests/test_trend.py tests/test_engine.py -q` 全緑
+ black + 最終報告。
