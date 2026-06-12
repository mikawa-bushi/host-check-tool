# T02: samplers/system.py + selfmeter.py — Tier1 システム全体と自己計測

フェーズ: P2(並列)/ 依存: T01
参照: `tasks/README.md` → 設計書 §4.2, §4.7(自己計測), §4.8 / 実装計画 §4.2
netdiag 参照: `/tmp/network-check-tool/netdiag/probes/system.py`(文体),
`core/probe.py`(基底の使い方)

## 所有ファイル(これ以外に触れない)

```
procdiag/samplers/system.py
procdiag/samplers/selfmeter.py
tests/test_sampler_system.py
tests/fixtures/proc_tree/            # 擬似 /proc (PSI・loadavg等のテキストファイル)
```

注意: `procdiag/samplers/__init__.py` は触らない(T09 が最終形を書く)。
テストは `from procdiag.samplers.system import SystemSampler` と直接 import する。

## 契約(T01 が提供済み — 変更禁止、読むだけ)

- `procdiag.core.sampler`: `Sampler`, `SampleBatch`, `SystemSnapshot`, `RunContext`,
  `register_sampler`
- `procdiag.storage.TABLE_COLUMNS["sys_samples"]` の列順 =
  `ts, cpu_total, cpu_max_core, mem_used, mem_available, swap_used, swap_in, swap_out,
   load1, psi_cpu_some10, psi_mem_some10, psi_mem_full10, psi_io_some10, psi_io_full10,
   disk_read_bytes, disk_write_bytes, self_cpu, self_rss`
  (swap_in/out と disk_read/write は**前回collectからの増分bytes**を記録する)

## 実装要求

### SystemSampler (`@register_sampler`)
- クラス属性: `name="system"`, `tier="T1"`, `interval_key="system_interval"`,
  title/what/why/manual_commands(`cat /proc/pressure/cpu`, `vmstat 1` 等)を日本語で
- `setup(ctx)`: PSI対応判定 — `ctx.proc_root/"pressure"/"cpu"` が読めるか。
  結果を `ctx.capabilities["psi"]` に記録。非対応なら ctx.notes に日本語で降格理由を追記
- `collect(ctx)`:
  - psutil: `cpu_percent(percpu=True)`(全体は平均、最大コア値を別途)、
    `virtual_memory()`, `swap_memory()`(sin/soutの増分計算)、
    `disk_io_counters()`(read/write bytesの増分)
  - `ctx.proc_root/"loadavg"` と `ctx.proc_root/"pressure"/{cpu,memory,io}` は
    **ファイル直読み**(proc_root差替えでテスト可能にするため。psutilを使わない)
  - PSIパース: `some avg10=1.23 avg60=... total=...` 形式。
    cpu は some のみ、memory/io は some と full の avg10 を取る
  - self_cpu/self_rss: `ctx.selfmeter` が非Noneなら `ctx.selfmeter.sample()` の値、
    Noneなら (None, None)
  - 戻り値: `[SampleBatch("sys_samples", [row])]` 1行
  - 副作用: 最新の `SystemSnapshot` を `self.last_snapshot` に保持
    (recorderがトリガ判定に使う。swap_out_delta は増分bytes)
  - 初回collectは増分系がすべて0になってよい(基準値の記録のみ)

### PSIパーサ
純関数 `parse_psi(text: str) -> dict[str, float]` として切り出しテスト可能に
(`{"some_avg10": .., "some_avg60": .., "some_avg300": .., "some_total": ..,
"full_avg10": .., ...}`。full行は無い場合もある)。

### SelfMeter (selfmeter.py)
実装計画 §4 の契約:
```python
class SelfMeter:
    def __init__(self, cfg: BudgetConfig): ...
    def sample(self) -> tuple[float, int]
        # (前回sample以降の自己CPU%, 現在RSS bytes)。psutil.Process(自プロセス)を保持
    def update(self, mono_now: float) -> str | None
        # 直近60秒の自己CPU移動平均を更新し、状態遷移時のみ
        # "throttle_down" / "throttle_restore" を返す (設計書§4.8):
        # - 平均 > cfg.overhead_budget_pct → throttle_down (factorを cfg.throttle_factor 倍、
        #   ただし間隔上限 cfg.throttle_max_interval を超えない範囲)
        # - 予算内が10分継続 → throttle_restore (段階的に1.0へ戻す)
    @property
    def factor(self) -> float   # T2b間隔に掛ける倍率 (>= 1.0)
```

## テスト要求(test_sampler_system.py)

- `parse_psi`: 正常(some+full / someのみ)、空文字・壊れた行 → 例外でなく部分結果
- 擬似 /proc ツリー(`tests/fixtures/proc_tree/` に pressure/{cpu,memory,io}, loadavg を
  置く)を `RunContext(config, proc_root=fixtureパス)` で注入し、collect が
  sys_samples 1行(列数が TABLE_COLUMNS と一致)を返す
- PSI無しツリー(pressureディレクトリ欠如)で capabilities["psi"]=False になり
  psi列が None で埋まる
- 2回連続collectで増分列(swap/disk)が非負
- SelfMeter: 偽の時計(mono_nowを手で進める)+ sampleのCPU値をモンキーパッチして、
  予算超過→"throttle_down"・factor>1、10分予算内→"throttle_restore"・factor=1.0 の遷移

## 完了条件
`.venv/bin/python -m pytest tests/test_sampler_system.py -q` 全緑 + black + 最終報告。
実マシンの /proc でも `collect` が例外なく動くこと(テスト内で1回だけ実体実行)。
