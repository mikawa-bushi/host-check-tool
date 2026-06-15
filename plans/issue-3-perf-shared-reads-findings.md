# Findings: issue-3-perf-shared-reads

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 2
- Informational: 6
- Key Issues:
  - (Should Fix) `cg_id_for` docstring に throttle の記載がない → 追記。
  - (Should Fix) self_cpu 集計の空テーブル（AVG→NULL→0.0）ケースが未テスト → `test_self_cpu_defaults_to_zero_when_no_samples` を追加。
  - (Info) rows キャッシュキー `(table,since,until,where,params)` は全パラメータ網羅・空結果も正しくキャッシュ。SQL 集計は旧 Python と等価（NULL 除外・0.0 含む・空→0.0）。`last_seen` はロジック非依存を確認。`_build_quality` は rows() を経由せず直接集計するため sys_samples を2回走査するが、materialize 回避が目的なので許容。
- Judgment: 実質的レビュー。Blocking なし。結果不変・キャッシュ安全性・集計等価性をすべて確認。Should Fix 2件反映。

### Doc Parrot
- Divergences Found: 0
- Details:
  - シグネチャ変更のある callable なし（自動検出ゼロ）。
  - docstring を更新した `Storage.rows`（read-only キャッシュ・共有読み取り専用ビュー）と `Storage.cg_id_for`（last_seen 60s throttle）を手動検証 → いずれも実装と一致し、`test_readonly_rows_are_cached` / `test_readwrite_rows_are_not_cached` / `test_cg_last_seen_update_is_throttled` が挙動を裏付け。
- Judgment: docstring と実装は一致。乖離なし。

**Round 1 結果: Blocking 0 — push 可。497 passed。**
