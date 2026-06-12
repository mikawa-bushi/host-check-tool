# T05: scheduler.py + recorder.py — monotonicスケジューラと常駐記録ループ

フェーズ: P2(並列)/ 依存: T01
参照: `tasks/README.md` → 設計書 §4.6〜§4.8, §5.1 / 実装計画 §5.1
netdiag 参照: `/tmp/network-check-tool/netdiag/watch.py`, `scheduler.py`
(常駐ループ・防護壁・graceful stop の手本)

## 所有ファイル(これ以外に触れない)

```
procdiag/scheduler.py
procdiag/recorder.py
tests/test_recorder.py
```

## 並行タスクとの結合点 — 重要

T02〜T04 が実サンプラーを並行実装中。**recorder は実サンプラーを import しない**。
`procdiag.core.sampler` の `Sampler` インターフェースと、以下の契約だけに依存して書く
(実配線は T09 が行う。本タスクのテストはすべてフェイク実装で行う):

- SystemSampler 相当: `last_snapshot: SystemSnapshot` 属性を持つ(トリガ判定の入力)
- DiskSampler 相当: 追加メソッド `collect_dirs(ctx) -> list[SampleBatch]` を持つ
- SpikeTrigger 相当(T04実装): `observe(snap, mono_now) -> SpikeDecision | None`、
  SpikeDecision は `.trigger_type: str` `.reason: str` `.psi_snapshot: dict|None`
- SelfMeter 相当(T02実装): `sample() -> (float, int)`, `update(mono_now) -> str|None`
  ("throttle_down"/"throttle_restore"/None), `factor: float`

このために recorder の `__init__` は**全コンポーネント注入可能**にする:
```python
class Recorder:
    def __init__(self, config: ProcdiagConfig, *,
                 db_path: Path | None = None,
                 duration: float | None = None,
                 ctx: RunContext | None = None,
                 samplers: dict[str, Sampler] | None = None,
                     # キー: "system","cgroup","process","disk"。None時はT09配線の既定
                     # (本タスク時点では None なら ImportError でよい — 遅延importで書いておく)
                 trigger=None, selfmeter=None, storage: Storage | None = None,
                 clock=time.monotonic, sleep=time.sleep): ...
    def run(self) -> int    # 0=正常終了(duration満了 or SIGTERM/SIGINT)
```

## 実装要求

### TierScheduler (scheduler.py)
設計書 §4.7。単一スレッド・monotonic基準:
```python
class TierScheduler:
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep,
                 on_gap: Callable[[float, str], None] | None = None): ...
    def add(self, name: str, interval: float | Callable[[], float],
            fn: Callable[[], None]) -> None
        # interval が callable なら毎周期評価 (T2bの自動減速用)
    def run(self, stop: threading.Event) -> None
        # 各ジョブ next_t += interval で管理 (ドリフト排除)
        # 実行遅延が interval の3倍超 → on_gap(遅延秒, ジョブ名) を呼び next_t を現在に再基準化
        # stop がセットされたら速やかに抜ける (sleepは最大0.5秒刻み等で分割)
```
ジョブ実行は try/except で包み、例外はジョブ名付きで logging(ループは死なない)。

### Recorder (recorder.py)
配線(設計書 §5.1 の流れ):
- 開始時: Storage を開き meta に hostname / kernel(`platform.release()`) / boot_time /
  procdiag_version / config(JSON) / started_ts を記録。
  RunContext を作り全サンプラーの setup() を呼ぶ。ctx.selfmeter を装着
- ジョブ登録:
  - T1(`system_interval`): system.run(ctx) → buffer。last_snapshot を trigger.observe に
    渡し、発火時: cgroup.run + process.run を即時実行 → buffer →
    spike_events に1行(`event_id`自動, ts, trigger_type, psi_snapshot JSON,
    top_processes JSON, top_cgroups JSON — top抽出は直近バッファ行から
    cpu順・メモリ順各10件/メモリ上位5cgroup。厳密さよりJSONが埋まることを優先)
    → `flush(reason="spike")`。
    selfmeter.update() の戻りが非None → ops_events("throttle_down"/"throttle_restore")
  - T2a(`cgroup_interval`): cgroup.run → buffer
  - T2b(callable間隔 `process_interval * selfmeter.factor`): process.run → buffer
  - T3(`disk_interval`): disk.run → buffer
  - T3dir(`dir_scan_interval`): disk.collect_dirs → buffer
  - フラッシュ(60秒): storage.flush() → storage.rollup_incremental() →
    1時間ごとに storage.retention(config.retention.raw, config.retention.rollup)
- 台帳コールバック: cgroup/process サンプラーのコンストラクタに
  `storage.cg_id_for` / `storage.proc_id_for` を渡す(T03契約)
- シグナル: SIGTERM/SIGINT で stop イベント → 最終 flush(reason="signal") →
  ops_events("stopped") → return 0。signal.signal はメインスレッド前提
- duration: 指定秒経過で自然終了(monotonic基準)
- gap: on_gap → ops_events("gap", f"{秒}s ジョブ{名}")

## テスト要求(test_recorder.py — すべてフェイクで)

- TierScheduler: 偽clock/偽sleep(呼ばれるたび時間を進める)で、
  1s/5s ジョブが期待回数呼ばれる・callable interval が毎回評価される・
  ジョブ例外でループが続く・人工的に clock を飛ばして on_gap が発火する
- Recorder: フェイクSampler群(SampleBatchを返すだけ)+フェイクtrigger
  (Nコール目に発火を返す)+フェイクselfmeter+実Storage(tmp_path)で
  `duration=数tick` 実行 → sys_samples に行がある / spike_events 1行
  (JSON列がパース可能)/ ops_events に stopped がある / meta が埋まる
- スロットル: フェイクselfmeter が throttle_down を返す→ ops_events 記録と
  T2b間隔の callable が factor を反映(scheduler 経由で間接検証)

実行時間に注意: テストは偽時計を使い、実時間スリープは合計1秒未満に抑えること。

## 完了条件
`.venv/bin/python -m pytest tests/test_recorder.py -q` 全緑 + black + 最終報告。
