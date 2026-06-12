# T09: 統合 — 配線・E2E・README・docs

フェーズ: P3(直列。T02〜T08 の全成果がツリーに揃った状態で開始)
参照: `tasks/README.md` → 実装計画 §2.2, §5, §9(M6/M8相当) / 設計書 §3, §10-4
netdiag 参照: `/tmp/network-check-tool/README.md`, `docs/netdiag.md`(文書の体裁)

## 所有ファイル

```
procdiag/samplers/__init__.py      # 最終形を書く (T01は空で作成済み)
procdiag/recorder.py               # 既定サンプラー配線の修正のみ可
procdiag/cli.py                    # 配線バグの修正のみ可
tests/test_e2e.py
README.md
docs/procdiag.md
```

**例外規定**: 本タスクは統合フェーズなので、結合バグを見つけた場合に限り
T02〜T08 の成果ファイルを最小修正してよい(契約シグネチャの変更は不可)。
修正した場合は最終報告に「統合修正」節としてファイル・理由を列挙すること。

## 作業内容

### 1. 配線
- `procdiag/samplers/__init__.py`: system/cgroup/process/disk を eager import して
  レジストリ登録(netdiag `probes/__init__.py` 参照)。trigger/selfmeter は
  サンプラーでないため import しない
- recorder の既定配線(`samplers=None` 時)を実サンプラー+SpikeTrigger+SelfMeter で
  埋める(T05 が遅延importで書いた箇所を実モジュール名に合わせる)
- cli の explain/list-samplers が実サンプラー4種・実ルール4本を表示することを確認

### 2. 全テストスイートの通過
`.venv/bin/python -m pytest tests/ -q` を実行し、統合起因の失敗を修正
(各タスクの単体テストは原則すでに緑のはず。落ちる場合は結合面のずれを疑う)。

### 3. E2E テスト(tests/test_e2e.py、設計書 §10-4)
`@pytest.mark.e2e` を付与:
- tmp_path で `procdiag init` → 生成configの db_path/watch_dirs を tmp に書き換え
  (sampling間隔を 0.2s/0.5s/1s 程度に短縮)→ CliRunner でなく subprocess で
  `procdiag record --duration 8s --config … --db …` を実行(シグナル処理の検証を兼ねる)
- 完走後: status が期間とサンプル数を表示 / sys_samples 行数 > 0 /
  check が exit 0〜2 のいずれかで完走(Finding有無は環境依存なので問わない)/
  report が report.html と report.json を生成し、HTMLに外部参照がない
- もう1ケース: record を起動し2秒後に SIGTERM → exit 0 かつ ops_events に stopped
- 自己負荷: meta/sys_samples の self_cpu 平均が 5% 未満であること(短時間実行のため
  予算1%でなく緩い閾値で)

### 4. README.md(リポジトリ顔。netdiag README の体裁を写像)
- 冒頭: 「host-check-tool は `procdiag` を出荷する」— network-check-tool との
  兄弟関係を1段落で(実装計画 §1 の表を流用してよい)
- Quick Start(pip install → init → record → check)、コマンドリファレンス表
  (実装計画 §2.2)、終了コード、systemd 運用、英日混在は netdiag README に倣う

### 5. docs/procdiag.md
docs/netdiag.md の構成を写像: Design in one picture(ASCII図)/ Quick start /
コマンドリファレンス / アーキテクチャマップ(実装計画 §3 のツリー)/
3層+トリガの説明 / 観測品質の思想。日本語主体でよい。

## 完了条件
`pytest tests/ -q` 全緑(e2e含む)+ black + 最終報告
(統合修正の一覧、E2Eの実測値: 記録行数・自己CPU・DB容量を含めること)。
