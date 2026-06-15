# Findings: issue-5-robustness

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 3
- Informational: 6
- Key Issues:
  - (Should Fix) `test_smoke_real_machine` の docstring が「8 checks」のまま → 9 に修正。
  - (Should Fix) `_check_config_security` の例外→info の理由を docstring に明記 → 追記。
  - (Should Fix) `_check_config_security` の例外パス未テスト → null byte パスで info になるテストを追加。
  - (Info) build_charts の限定 except は妥当（PSI 列は REAL で float 例外なし）/ retention 失敗後の record_event はトランザクション安全（`with self.conn` が rollback 済）/ `.get()`→`keys()` 修正は両経路で正しい。docs の chmod 推奨を 0600 主体に調整。
- Judgment: 実質的レビュー。Blocking なし。広域 except 限定の安全性・潜在バグ修正の正しさ・トランザクション安全性を確認。Should Fix 3件＋docs を反映。

### Doc Parrot
- Divergences Found: 0
- Details:
  - `doctor._check_config_security` — OK（missing→info / world-writable(0o666)→degraded / safe(0o600)→ok / 異常→info が実行結果と一致）。
  - シグネチャ変更のみの `run_doctor`（config_path 追加）は自動検出外だが、docstring を「9 items」「config_path」に更新済で test_doctor が裏付け。
- Judgment: docstring と実装は一致。

**Round 1 結果: Blocking 0 — push 可。505 passed。**

### 特記: 潜在バグの発見
広域 except の限定により、`build_charts` が `sqlite3.Row` に存在しない `.get()` を呼んでいた
潜在バグが顕在化（raw `sys_samples` のみの DB で PSI/disk/spike チャートが無言欠落）。
同 PR で `keys()` 判定に修正済み。§4-1（広域 except が不具合を隠す）の趣旨どおりの成果。
