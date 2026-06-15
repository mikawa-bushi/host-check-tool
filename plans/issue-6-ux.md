# Plan: issue-6 — UX（§5）

## Goal (issue #6)
レポート/CLI の小さな体験改善: 複数ホスト識別・誤提案抑制・終了コード周知・OS ツールとの数値整合。

## 変更設計

### 5-1 HTML レポートに記録済み hostname を反映
`render_html` に `meta_hostname: str | None = None` 引数を追加（現状の常に None のローカル変数を置換）。
title 解決順は `hostname_label or meta_hostname or 既定`。
cli の `check`(html) と `report` で `store` を開いている間に `store.meta().get("hostname")` を読み、render_html へ渡す。

### 5-2 is_python の厳格化
`memory.py` のインライン `"py" in name.lower()`（copy/scapy 等に誤マッチ）を、
module ヘルパ `_is_python_process(name) -> bool`（`name.lower().startswith("python")`）に抽出・置換。
テスト可能になり、python/python3/python3.11 を真、copy/scapy/happy/pypy を偽に。

### 5-3 check に --exit-zero-on-info
`check` に `--exit-zero-on-info` フラグを追加。info のみの場合の終了コードを 2→0 にできる（CI 向け opt-out）。
既定は従来通り 2。warning/critical は引き続き 1、healthy は 0。

### 5-4 humanize_bytes を 1024 基数へ
SI(1000) から binary(1024) に変更し `df -h`/`free -h` と数値整合。サフィックスは慣行どおり B/KB/MB/GB/TB を維持
（df -h 同様、binary 値に K/M/G を付す）。docstring も更新。

## テスト
- `test_report.py`: render_html が meta_hostname をタイトルに使う / hostname_label 優先。
- `test_core.py`: `_is_python_process` の真偽（python3=True, copy/scapy/pypy=False）。
  既存 `test_humanize_bytes_gb` を binary 値（2.5→2.3GB）に更新 + 1024 基数の assertion。
- `test_cli.py`: `check --help` に --exit-zero-on-info が出る / info のみシナリオで
  フラグ有→exit0・無→exit2（合成データで info finding を作る or 既存ヘルパ流用）。
- 既存スイート緑。

## Out of scope
JSON 公開キー・既存終了コードの意味（2=info）は壊さない（フラグは opt-in の追加のみ）。
HTML オフライン自己完結維持。§2/§3/§4。
