# Plan: issue-1 — 確実性の高い不具合3件

## Goal (issue #1 verbatim outcome)

> `procdiag check --format html` が正しく時系列チャートを描画し、`config.rules.extra_rules`
> が実際にルールを読み込み、観測品質の gap が正しい意味（秒）で表示される。

出典: `procdiag_improvement_review.md` §1-1 / §1-2 / §4-2。

## Acceptance scenarios → test mapping

1. **HTML チャート**: データ入り DB に `check --format html` → HTML に `report` と同じデータ点が出る。
   - test: `tests/test_cli.py` に `check --format html` の E2E。生成 HTML に SVG polyline（チャート点）が含まれ、「データなし」ではないことを assert。併せて `report` 出力と同等であること。
2. **extra_rules**: 有効な import パス → そのルールが診断に反映。無効パスでも全体は落ちず `--verbose`/JSON で失敗が分かる。
   - test: `tests/test_engine.py` に、一時モジュールを `@register` するパスを `config.rules.extra_rules` に渡して `Engine.run` がそのルールを実行することを確認。無効パスでは `reasoning` に失敗が載り例外で落ちないことを確認。
3. **gap 秒**: gap イベントの遅延秒の合計が `quality.gap_seconds` に入る（件数ではない）。
   - test: `tests/test_engine.py` で `ops_events` に既知 delay の gap 行を複数入れ、`_build_quality` の `gap_seconds` が「遅延秒の合計」に一致（件数と異なる値）することを確認。

## Approach (minimal, mirrors existing correct code)

### §1-1 cli.py `check` — build_charts inside Storage context
`procdiag/cli.py` の `check`: `build_charts(store)` が `with Storage(...)` の外（接続クローズ後）にあるのが原因。
`report` コマンドと同じく with ブロック内で charts を構築する。

```python
charts = None
with Storage(resolved_db, read_only=True) as store:
    engine = Engine(cfg, verbose=verbose)
    diag = engine.run(store, only=only_list, skip=skip_list)
    if fmt == "html":
        charts = report_mod.build_charts(store)
# render は store 不要なので with の外でよい
```

### §1-2 extra_rules 配線 — Engine.run の冒頭で load_extra_rules
`procdiag/diagnosis/engine.py` の `run()` 冒頭（`select()` 前、registry 読み取り前）で
`load_extra_rules(self.config.rules.extra_rules)` を呼ぶ。`@register` が発火してから `all_rules()` が読まれる順序を保証。
- 失敗は常に `reasoning` に追記（JSON 出力に残り、`--verbose` 端末でも表示）。
- 成功は `verbose` 時のみ `reasoning` に追記。
- `check`/`report` 双方が Engine.run を通るので 1 箇所で両対応。
- import: `from ..plugins import load_extra_rules`（plugins は importlib のみ依存＝循環なし）。

### §4-2 gap_seconds を「遅延秒の合計」に
- `recorder._on_gap` は既に detail を `f"{delay:.1f}s ジョブ{job_name}"` と先頭に秒を持たせている。フォーマットを機械可読寄りに微調整しつつ後方互換に。
- `engine._build_quality`: `gap_seconds = float(len(gap_rows))`（件数）を、各 gap 行の detail から遅延秒を取り出して合計するよう変更。
- 解析は防御的に（`^([0-9.]+)s` 正規表現、失敗時はその行 0 秒で件数のみ寄与）。専用ヘルパ `_parse_gap_delay_seconds(detail) -> float` を engine.py に置く。
- スキーマ変更はしない（`ops_events` の ts/kind/detail のまま）。

## Out of scope (別 Issue)
- §2 速度（複数ルールのフルスキャン共有, SQL 集計化, cgroup UPDATE 間引き）
- §3 保守（CPU/リークルールの重複抽出, registry 名前引きヘルパ, tuple 添字, dead code `live_keys`, MEM-LEAK 無駄読み）
- §5 UX（HTML への hostname 反映, is_python 部分一致, 終了コード周知, humanize_bytes 基数）
- JSON レポートの公開キー・check 終了コード仕様は不変。HTML はオフライン自己完結維持。

## Assumptions
- `.venv`（Python 3.10.12, pytest 9, black 26.5.1, procdiag editable）が正規環境。
- `store.rows("ops_events", where="kind='gap'")` は ts 昇順で全 gap 行を返す（storage 既存 API）。
- `render_terminal(diag, verbose=True)` と JSON は `diag.reasoning` を出力する（report.py 既存挙動）。

## Risks
- extra_rules はグローバル registry を変異させる（既存設計どおり）。テストは追加ルールを `disabled` で後始末、または専用 rule_id で衝突回避。
- gap detail 解析は文字列依存。`_on_gap` と解析を同 PR で対にし、フォーマットを固定。
