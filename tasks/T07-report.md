# T07: report.py — ターミナル/JSON/単一HTML(SVGチャート)レンダラ

フェーズ: P2(並列)/ 依存: T01
参照: `tasks/README.md` → 設計書 §7 全節 / 実装計画 §2.2(出力例), §6
netdiag 参照: `/tmp/network-check-tool/netdiag/report.py` — **render_terminal の
見た目(記号・色・「━━━ 診断 (Diagnosis) ━━━」見出し・確信度 高/中/低)を意図的に揃える**

## 所有ファイル(これ以外に触れない)

```
procdiag/report.py
tests/test_report.py
```

## 契約(T01 提供 — 変更禁止)

- `procdiag.core`: `Diagnosis`, `Finding`, `Severity`, `ObservationQuality`
- `procdiag.storage.Storage`: `series()`, `rows()`, `meta()`, `counts()`
- 外部契約(T08 の cli が遅延importで呼ぶ):
  ```python
  def render_terminal(diag: Diagnosis, *, color: bool = True, verbose: bool = False) -> str
  def render_json(diag: Diagnosis) -> str
  def render_html(diag: Diagnosis, charts: "ChartData | None" = None,
                  *, hostname_label: str | None = None) -> str
  @dataclass
  class ChartData: ...        # フィールドは本タスクの内部設計に任せる
  def build_charts(store: Storage, *, since: float | None = None) -> ChartData
  ```

## 実装要求

### render_terminal
netdiag の構成を踏襲(richは使わない。ANSIエスケープ直書き):
1. Finding一覧行: `{記号} [{rule_id}] {subject.ljust(…)} {summary}`
   (severity色: critical=赤, warning=黄, info=シアン。color=False で無色)
2. healthy 時: 「診断: 問題は見つかりませんでした。」(緑・太字)
3. `━━━ 診断 (Diagnosis) ━━━` 見出しの下に Finding ごと:
   `N. 推定原因: {rule_id} — {summary} [確信度: 高/中/低]`、
   `proposal`(5部構成)をインデントして全文、`related` があれば
   「関連する検出 (同一原因とみられるもの): …」
4. 末尾に観測品質の要約1ブロック(期間・gap・自己負荷・欠損率・notes)
5. verbose 時は reasoning を「推論過程:」として列挙

### render_json
`{"healthy": …, "findings": [Finding.to_dict()…], "quality": …, "reasoning": …,
"generated_at": iso8601, "procdiag_version": …}` を indent=2, ensure_ascii=False で。
**機械可読の安定形**(設計書: 外部LLM・チケット連携用)なのでキー名を変えないこと。

### SVGチャート(外部依存なし・自前生成)
- `_polyline_svg(series, *, w=720, h=120, label, unit, y_max=None) -> str`
  時系列折れ線1枚。軸は最小限(開始/終了時刻、y最大値のテキスト)。点数が多い場合は
  幅pxに合わせて間引き(LTTBは不要、等間隔バケットのmax/avgで可)
- `ChartData`: cpu(全体%)/ cpu_max_core / mem_used / psi(cpu・mem・io の some10 を
  1枚に3系列、色分け+凡例)/ mountpointごとの使用率 を保持
- `build_charts(store)`: sys_rollup_1m(無ければ sys_samples)と disk_samples から
  series を読む。空DBなら空のChartData(render側は「データなし」表示)

### render_html
設計書 §7.1 の5節構成・**単一ファイル・外部参照ゼロ**(CSSインライン、JSは
ツールチップ程度までで省略可。CDN・<script src>・<link href> 禁止):
① サマリ(健全性 + Finding件数バッジ + 一覧表) ② 提案詳細(Finding ごとに
5部構成を <pre> でなく整形段落で) ③ 時系列チャート(SVGインライン)
④ スパイクイベント一覧(charts と同様 store から…は取らない。**Diagnosis と
ChartData だけから描く**。スパイク一覧は ChartData に `spikes: list[dict]` として
build_charts が詰める) ⑤ 観測品質。
タイトルは `hostname_label or meta hostname`(引数優先)。
netdiag render_html の inline-CSS スタイルを参考に、日本語で。

## テスト要求(test_report.py)

Diagnosis/Finding/ChartData はテスト内で合成(他タスクの実装に依存しない):
- render_terminal: healthy/finding あり/color=False でANSIコード無し/verbose で推論過程
- render_json: json.loads で往復可能、キー存在、日本語が \uXXXX にならない
- render_html: 出力に `http://`・`https://`・`<script src`・`<link` が**含まれない**
  (オフライン要件の機械検査)、SVGが Finding 数に関係なく描かれる、
  ⑤観測品質の数値が出る
- build_charts: tmp_path の実Storageに sys_samples/disk_samples を数行入れて
  系列が取れる、空DBで例外にならない
- SVG間引き: 10,000点入れても出力サイズが暴れない(< 数百KB)

## 完了条件
`.venv/bin/python -m pytest tests/test_report.py -q` 全緑 + black + 最終報告。
