# Plan: issue-3 — 速度（結果不変）

## Goal (issue #3)
findings/JSON を変えずに check/report と記録の定常コストを下げ、ルール増加に対し線形劣化しにくくする。

## 変更設計

### S1 同一テーブル重複スキャン共有 → read-only `rows()` クエリキャッシュ
`Storage.rows()` の結果は `(table, since, until, where, params)` で一意。`read_only=True` の Storage に限り
結果を辞書キャッシュし、同一引数の再呼び出しは SQLite を叩かず共有する。
- `__init__`: `self._rows_cache = {} if read_only else None`。
- `rows()`: cache 有効かつ key ヒットならキャッシュ返却、ミスなら fetchall して格納。
- read-write（記録時）はキャッシュしない（データが変わるため）。
- MEM-LEAK/FD-LEAK/THREAD-LEAK は同一 `rows("proc_rollup_1m", window)` を共有、CPU/IO/OOM は `sys_samples`/`sys_rollup_1m` を共有。
- 返り値は読み取り専用契約（rules は in-place 変更しない。sqlite3.Row は不変）。docstring 明記。

### S2 単純統計を SQL 集計化（`engine._build_quality` の self_cpu）
全 `sys_samples` 行を Python に展開して avg/max する処理を
`SELECT AVG(self_cpu), MAX(self_cpu) ... WHERE self_cpu IS NOT NULL` の集計に置換。
空集合は NULL→0.0 で従来と同値。大規模 DB で行を抱えない。

### S3 cgroup `last_seen` UPDATE 間引き
`last_seen` はロジックで読まれない純メタデータ（grep 確認済）。`cg_id_for` のキャッシュヒット時に
毎ティック発行していた UPDATE を、ユニットごと 60s に 1 回へ間引く。
- `self._cg_last_seen_written: dict[str, float]` で最終書込 ts を記録。
- insert 経路でも記録。cgroup_interval=5s なら UPDATE は 12 分の 1。

## テスト
- `test_storage.py`: read-only で `rows()` が同一オブジェクトを返す（キャッシュ）/ read-write では毎回新規。
  `cg_id_for` の last_seen が throttle 窓内で不変・窓超で更新。
- `test_engine.py`: `_build_quality` の self_cpu_avg/max が既知値と一致（SQL 集計の正しさ）。
- 既存スイート全緑（findings 不変の保証）。

## Out of scope
findings/JSON/スキーマ不変。OOM の最近傍探索など全件取得が本質的に必要な箇所は対象外。
§3/§4/§5（別 issue）。
