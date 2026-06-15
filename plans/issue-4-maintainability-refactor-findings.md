# Findings: issue-4-maintainability-refactor

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 2
- Informational: 4
- Key Issues:
  - (Should Fix) `confidence_from_fit` の厳密境界テスト不足 → `(0.95,12.0)→HIGH` / `(0.90,6.0)→MEDIUM` を追加。
  - (Should Fix) `plans/workflow_state.md` はリポジトリに不要 → step 9 で削除（最終 PR には含めない）。
  - (Info) io_disk の proc フォールバックが `str(proc_id)`→`proc_{id}` に変わるが到達不能経路（plan で明記済み）。挙動5点（状態機械・確信度ラダー・proc_name フォールバック・days_to_oom ゲート・名前付きソートキー）はすべて差分検査で不変を確認。
- Judgment: 実質的レビュー。Blocking なし。挙動不変の主張5点を逐語検証で支持。Should Fix 2件を反映。

### Doc Parrot
- Divergences Found: 0
- Details:
  - `trend.confidence_from_fit` — OK（HIGH/LOW 例が実行結果と一致）。
  - `trend.find_sustained_episodes` — OK（連続/短すぎ run の例が一致）。
  - `Storage.proc_name` — OK（既知=nginx / 不在=proc_99999）。
  - `Storage.cg_unit_path` — OK（既知=unit path / 不在=str(cg_id)）。
- Judgment: 新規4 callable とも docstring と実装一致。

**Round 1 結果: Blocking 0 — push 可。490+2=492 passed 見込み。**
