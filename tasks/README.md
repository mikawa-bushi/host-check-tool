# procdiag 実装ワークフロー — フェーズ計画と共通規約

実装エージェントへ: あなたは `tasks/TXX-*.md` のゴールファイルを1つ割り当てられている。
**最初に本書(共通規約)→ 自分のゴールファイル → そこに列挙された参照資料**の順に読むこと。

## フェーズ構成

```
P1: T01 基盤(契約層)                     … 直列。全タスクの前提
P2: T02..T08 並列実装(7タスク同時)        … T01完了後に一斉開始
P3: T09 統合(CLI配線・E2E・README)        … P2全完了後
P4: T10..T12 残りルール9本(3タスク並列)    … P3完了後
```

| Task | 内容 | 依存 |
|---|---|---|
| T01 | pyproject / core(契約層) / config / storage / plugins | — |
| T02 | samplers/system.py + selfmeter.py (T1層+自己計測) | T01 |
| T03 | samplers/cgroup.py + process.py (T2a/T2b層) | T01 |
| T04 | samplers/disk.py + trigger.py (T3層+スパイクトリガ) | T01 |
| T05 | scheduler.py + recorder.py (常駐ループ) | T01 |
| T06 | diagnosis/ (engine + trend + 優先4ルール + テンプレ) | T01 |
| T07 | report.py (terminal/JSON/HTML+SVG) | T01 |
| T08 | cli.py + service.py + export.py | T01 |
| T09 | 統合: CLI配線確認・E2E・README・docs | T02–T08 |
| T10 | ルール追加: MEM-PRESSURE / SWAP-THRASH / CPU-SATURATION | T09 |
| T11 | ルール追加: IO-BOTTLENECK / DISK-GROWTH / DISK-JOURNAL | T09 |
| T12 | ルール追加: FD-LEAK / THREAD-LEAK / ZOMBIE | T09 |

## 共通規約(全タスク必須)

### ファイル所有権 — 最重要
- 編集・作成してよいのは**自分のゴールファイルの「所有ファイル」節に列挙されたパスのみ**。
  他のタスクが並行して同じツリーを編集している。所有外ファイルに触れると衝突する。
- 共有ファイル(`pyproject.toml`, `procdiag/core/*`, `procdiag/config.py`,
  `procdiag/storage.py`, `tests/conftest.py`)は P2 以降**読み取り専用**。
  契約(シグネチャ・列順)に不備を見つけても自分で直さず、**回避せずに最終報告で指摘**する。
  自タスク内で完結する範囲で実装を進められるなら進めてよい。
- `git commit` / `git add` は禁止(オーケストレータが管理)。`pip install` も禁止
  (依存は `.venv` に導入済み。例外: T01 のみ `pip install -e . --no-deps` を1回実行)。

### 参照資料(共通)
- `procdiag_design.md` — 仕様の正。自タスク該当節は熟読
- `procdiag_implementation_plan.md` — 構成とAPI契約の正
- `/tmp/network-check-tool/netdiag/` — **文体・イディオムの手本**(クローン済み読み取り可)。
  procdiag は netdiag の兄弟ツールであり、コードの書き味(docstring の流儀、
  防護壁パターン、stdlib志向)を意図的に揃える

### コーディング規約
- Python 3.10。型ヒント必須(`from __future__ import annotations`)
- black(line-length 88)準拠。完了前に `.venv/bin/black <所有ファイル>` を実行
- モジュール冒頭に netdiag 流の「設計意図を語る」docstring(英語)。
  コード内コメントは英語で控えめに、**ユーザー向け文字列(CLI出力・エラー・レポート)は日本語**
- 依存は psutil / pydantic / jinja2 / click と stdlib のみ。numpy・pandas・rich 禁止
- 例外の防護壁: サンプラー・ルール・ループの1単位の失敗が全体を殺してはならない
  (netdiag の `Probe.run` / `Monitor._loop` を参照)

### テストと完了条件
- テストは `tests/test_*.py`(フラット配置)。データfixtureは `tests/fixtures/` 配下に
  自タスク専用サブディレクトリを作る
- 実行: `.venv/bin/python -m pytest tests/<自分のテスト> -q`(リポジトリルートで)
- 完了条件: ①所有テスト全緑 ②black適用済み ③契約からの逸脱ゼロ
  (やむを得ず逸脱した場合は最終報告に「逸脱」節を設けて理由を明記)

### 最終報告フォーマット
1. 実装サマリ(ファイルごとに2-3行)
2. テスト結果(コマンドと出力要約)
3. 契約逸脱・未解決の懸念(なければ「なし」)
4. 他タスクへの引き継ぎ事項(なければ「なし」)
