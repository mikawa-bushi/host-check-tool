# Plan: issue-4 — 保守リファクタ（挙動不変）

## Goal (issue #4)
重複ロジックの集約とデッドコード除去で、閾値・確信度ポリシー変更が1箇所で済むようにする。挙動は不変。

## 安全網
純粋リファクタのため既存スイート(476)が回帰検出の主軸。新規ヘルパには単体テストを追加。

## 変更設計

### 3-1 CPU 連続飽和ラン検出 → `trend.find_sustained_episodes`
`cpu.py` の CPU-SINGLECORE-01 (215-264) と CPU-SATURATION-01 (432-470) の同型ステートマシンを
`trend.py` の純関数に抽出:
```
find_sustained_episodes(samples: list[(bucket:int, ts:float, is_saturated:bool)], *, min_consecutive, max_gap_seconds=90.0) -> list[(start_ts, end_ts)]
```
既存ロジック（90s ジッタ許容・非連続で run 切替・末尾フラッシュ）を逐語的に再現。各ルールは
minute_data から `(bucket, ts, is_saturated)` を作って呼ぶだけにする。検出結果は不変。

### 3-2 確信度ラダー → `trend.confidence_from_fit`
MEM-LEAK (memory.py:134-139) / FD-LEAK (leak.py:150-155) / THREAD-LEAK (leak.py:275-280) の
完全一致ラダー `r2>=0.95&span>=12→HIGH / r2>=0.90&span>=min_obs→MEDIUM / else LOW` を
`confidence_from_fit(fit, *, min_observation_hours, ...)` に集約。3ルールが呼ぶ。

### 3-3 名前引き → `Storage.proc_name` / `Storage.cg_unit_path`
`SELECT name FROM proc_registry WHERE proc_id=?` の直書き（memory/leak×2/cpu/crash/io_disk/recorder）を
`Storage.proc_name(proc_id) -> str`（不在時 `proc_{id}`）に統一。
`cgroup_registry` 引き（io_disk）を `Storage.cg_unit_path(cg_id) -> str`（不在時 `str(cg_id)`）に統一。
- io_disk の proc フォールバックは現状 `str(proc_id)` だが `proc_{id}` に統一（テスト非依存・到達不能経路）。

### 3-4 タプル添字 → 列名アクセス
`recorder._top_processes_json` / `_top_cgroups_json` の `r[3]`/`r[4]` 等を、
`TABLE_COLUMNS` を使った `dict(zip(cols, r))` の列名アクセスに置換。出力 JSON のキー・値は不変。

### 3-5 デッドコード削除
`process.py:283` の常に空集合・未使用 `live_keys` 行を削除。

### 3-6 MEM-LEAK の無駄読み削除
`memory.py:143-146` の `mem_row = store.rows("sys_rollup_1m", ...)`（真偽ゲートのみ）を削除し、
`sys_last`(sys_samples) の有無で直接ガード。days_to_oom は sys_samples ベースのまま不変。
- 到達可能状態で差異なし（MEM-LEAK 発火には proc_rollup_1m が必要＝rollup 実行済＝sys_rollup_1m も存在）。

## テスト
- `tests/test_trend.py`: `confidence_from_fit`（HIGH/MEDIUM/LOW 境界）と `find_sustained_episodes`
  （連続/非連続/最短長/末尾）を新規。
- `tests/test_storage.py`: `proc_name`/`cg_unit_path` の登録済み/不在フォールバックを新規。
- 既存スイート全体が緑のまま（挙動不変の保証）。

## Out of scope
§2 速度（別 issue #3）/ §4 §5。is_python (memory.py:169) は §5-2＝issue #6。
出力・findings・JSON キー・終了コードは不変。
