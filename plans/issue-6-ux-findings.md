# Findings: issue-6-ux

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 2
- Informational: 8
- Key Issues:
  - (Should Fix) `meta_hostname` の fetch が html 専用である旨のコメント不足 → コメント追記。
  - (Should Fix) `test_humanize_bytes_mb` の値 `1_234_567` が SI/binary 両方で "1.2MB" になり基数を判別できない → `1_500_000`（binary 1.4MB / SI 1.5MB）に変更。
  - (Info) exit コード書き換えは全4ケース正しい（無=0 / warn|crit=1 / info のみ=2 / flag で 0）。0.2.4→0.3.0 の minor bump は妥当（CI 影響あり得る挙動変更）。hostname は両 caller とも with 内で読取。startswith("python") の pypy 除外は妥当。
- Judgment: 実質的レビュー。Blocking なし。exit ロジックの全分岐・hostname スレッディング・基数変更の影響範囲を確認。Should Fix 2件を反映。

### Doc Parrot
- Divergences Found: 0
- Details:
  - `memory._is_python_process` — OK（python3/Python3.11→True、copy/scapy/pypy→False が実行結果と一致）。
  - docstring 変更の `render_html`（meta_hostname 追加。3 テストで裏付け）/ `humanize_bytes`（binary 基数。docstring 例 1.2MB が成立）を手動検証 → 一致。
- Judgment: docstring と実装は一致。

**Round 1 結果: Blocking 0 — push 可。513 passed。**

### 特記: 終了コードの到達不能バグ
`check` の exit 2（info のみ）は、`diag.healthy`（warn/crit が無ければ True）経由で常に 0 に倒れ、
ドキュメント記載の「2=info のみ」が**到達不能**だった。本 PR で exit ロジックを findings ベースに書き換え、
ドキュメント通り info のみ→2 を実発火させ、`--exit-zero-on-info` で opt-out 可能にした（v0.3.0 / CHANGELOG 明記）。
