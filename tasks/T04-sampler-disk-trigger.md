# T04: samplers/disk.py + trigger.py — Tier3 ディスク層とスパイクトリガ

フェーズ: P2(並列)/ 依存: T01
参照: `tasks/README.md` → 設計書 §4.5, §4.6 / 実装計画 §3, §5.1
netdiag 参照: `/tmp/network-check-tool/netdiag/alerting.py`(デバウンス状態機械の手本)

## 所有ファイル(これ以外に触れない)

```
procdiag/samplers/disk.py
procdiag/samplers/trigger.py
tests/test_sampler_disk.py
tests/test_trigger.py
```

`procdiag/samplers/__init__.py` は触らない。テストは直接 import。

## 契約(T01 提供 — 変更禁止)

- `procdiag.core.sampler`: `Sampler`, `SampleBatch`, `SystemSnapshot`, `RunContext`,
  `register_sampler`
- `procdiag.config`: `TriggerConfig`(psi_cpu_some10=20.0, psi_mem_some10=5.0,
  psi_io_some10=30.0, spike_cooldown_sec=30, spike_storm_threshold=10)
- 列順契約:
  - `disk_samples`: `ts, mountpoint, total, used`
  - `dir_samples`: `ts, path, size_bytes, is_estimate`

## 実装要求

### DiskSampler (disk.py, `@register_sampler`, name="disk", tier="T3", interval_key="disk_interval")
- 60秒系: `psutil.disk_partitions()` → pseudo-fs(tmpfs/proc/sysfs/devtmpfs/squashfs/
  overlay等)除外 → `psutil.disk_usage()` で disk_samples 行
- 1時間系(dir走査)は**同じサンプラー内の別メソッド** `collect_dirs(ctx) -> list[SampleBatch]`
  として実装(recorderが別間隔 `dir_scan_interval` で呼ぶ)。
  `config.sampling.watch_dirs` の各 `WatchDir(path, time_budget_sec)` を `os.scandir`
  再帰で集計。**時間予算**: `time.monotonic()` を見て予算超過したら走査を打ち切り、
  そこまでの合計を `is_estimate=1` で記録(設計書 §4.5)。シンボリックリンクは辿らない。
  PermissionError はスキップして続行
- journald: `/var/log/journal` 相当を `ctx.config` の watch_dirs と独立に集計…は
  しない。シンプルに: `WatchDir(path="/var/log/journal")` を init 雛形に含める方針
  なので特別扱い不要(T06 の DISK-JOURNAL ルールが dir_samples を読む)

### SpikeTrigger (trigger.py — Samplerではない素のクラス)
実装計画 §4 の契約(netdiag StateMachine の写像):
```python
@dataclass
class SpikeDecision:
    trigger_type: str          # "cpu" | "memory" | "io" | "storm"
    reason: str                # 日本語の発火理由 (例 "PSI cpu some10=25.3 > 20.0")
    psi_snapshot: dict | None  # 発火時点のSystemSnapshot.psi

class SpikeTrigger:
    def __init__(self, cfg: TriggerConfig, *, psi_available: bool): ...
    def observe(self, snap: SystemSnapshot, mono_now: float) -> SpikeDecision | None
```
- PSI利用可: psi の cpu_some10/mem_some10(またはmem_full10>1.0)/io_some10 を閾値比較
- PSI不可(フォールバック、設計書 §4.6): cpu_total>85 / mem_available<10%(mem_total比)/
  swap_out_delta>0
- **デバウンス**: 同一 trigger_type の再発火は `spike_cooldown_sec` 以上空ける
  (mono_now基準。typeごとに独立)
- **ストーム集約**: 直近5分(mono_now基準のスライディングウィンドウ)の発火回数が
  `spike_storm_threshold` を超えたら、以後は個別発火を止めて
  `SpikeDecision(trigger_type="storm", ...)` を**1回だけ**返し、ウィンドウ内の発火数が
  閾値の半分を下回るまで沈黙(ヒステリシス)。ストーム解除後は通常動作に戻る

## テスト要求

- disk: tmp_path に実ディレクトリツリーを作って collect_dirs のサイズ集計一致、
  時間予算(time_budget_sec=0 で即打ち切り)→ is_estimate=1。
  disk_samples は実マシンで1回実行し列数・root("/")の存在を確認
- trigger(純粋ロジック・時計は引数なので偽時計で網羅):
  - 閾値未満→None、超過→該当typeで発火、psi_snapshot が載る
  - 同type連続→cooldown内はNone、cooldown経過後に再発火
  - 異なるtypeはcooldown独立
  - PSI不可フォールバック3条件
  - 11回/5分発火させる→"storm"が1回だけ、その後沈黙、ウィンドウが掃けたら回復

## 完了条件
`.venv/bin/python -m pytest tests/test_sampler_disk.py tests/test_trigger.py -q` 全緑
+ black + 最終報告。
