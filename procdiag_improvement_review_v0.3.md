# procdiag 改善アイデア（v0.3.0）— UI / 保守性 / 機能 / 処理速度

対象: `host-check-tool` 同梱の **`procdiag`** パッケージ（v0.3.0, 2026-06-17 時点 `main`）
観点: **UI / 保守性 / 機能 / 処理速度** の 4 軸。各項目に「場所 / 現状 / 問題 / 改善案 / 根拠 / 影響・工数・種別」を付す。

> **対応状況 (v0.4.0, 2026-11-05)**: 本書の項目は v0.3.0 以降のコミット (c8ceb3a ほか) と v0.4.0 でほぼ実装済み。未対応・一部対応は次のとおり。
> - §4-3: fsync の強制はやめたが、スパイク時のプロセス・cgroup 全走査は 1 秒周期のスレッドで同期実行のまま (`recorder.py` の `_handle_spike`)。
> - §4-7: `Storage.proc_id_for` は INSERT と SELECT の 2 回のまま (`RETURNING` 未使用)。
> - §2-7: `trend.confidence_from_span` を使うのは `io_disk.py` の 1 か所だけで、他のルールは確証度の段階を個別に書いている。
> - §3-8: ホスト固有ベースラインを使うのは MEM-PRESSURE-01 だけ。
> - §3-7: 再起動跨ぎの昇格は「初出が現在の起動より前か」だけを見る簡略版 (再起動回数・プロセス再起動は数えない)。
> - §1-11 後半: HTML レポートの読み方の説明は未作成。
> - §2-4: 版番号は `pyproject.toml` と `procdiag/__init__.py` の 2 か所に手書きのまま。食い違いは `tests/test_storage.py` が検出する。

> **方法**: 4 軸 ×（CLI・可視化／コード構造・テスト基盤／正確性・検出力／record負荷・読取クエリ）の多視点で現状コードを精査し、**各指摘の `file:line` を実コードに照合する敵対的検証**を通したものだけを残した。検証で棄却した誤指摘（例: 「doctor コマンド未実装」＝実在する、「`db_max_size_mb` が未有効」＝`config.py:141` に既定 2048MB で存在、「sample/rollup 表に `ts` 索引が必要」＝PK 先頭列が `ts`/`ts_min` なので範囲スキャンは効く）は本書から除外している。
>
> **前提**: 直近レビュー（`procdiag_improvement_review.md`, #1〜#10, v0.2.0→v0.3.0）の全項目は対応済み。本書はそれらを**再掲せず**、v0.3.0 の現状コードに残る新規の改善余地のみを扱う。末尾「§6 既に対応済み」を参照。

---

## 0. 総評

レイヤ分離（`samplers`→`storage`→`diagnosis`→`report`）・依存注入・defensive wall・型注釈の徹底という土台は引き続き堅牢で、前回レビューの是正も丁寧に入っている。今回の指摘は「動かない」レベルではなく、**(a) 現状コードに残る確実な不具合 4 件**、**(b) 現場運用（IT 非常駐の工場エッジ PC）での実用性・正確性に効く改善**、**(c) 検出力を「なぜ落ちる」へ広げる拡張**が中心。優先度は §5 の表を参照。

### 確実な不具合（最優先・4 件）

| ID | 区分 | 一言 | 影響 |
|---|---|---|---|
| **A-01** | UI/正確性 | 空 DB で「問題は見つかりませんでした」を緑字＋`exit 0`（偽の正常） | 高 |
| **C-01** | 機能/正確性 | cgroup の CPU/IO デルタが非負クランプされず、サービス再起動で巨大な負値を会計 | 高 |
| **C-03** | 機能/正確性 | 終了時に `rollup_incremental` を呼ばず、停止直前（＝最重要区間）の生データがロールアップに昇格しないまま retention で消失 | 中 |
| **D-01** | 速度/堅牢性 | サンプラ恒常失敗時に `ctx.notes` が無制限に伸び続ける常駐メモリリーク（自己 RSS は throttle 対象外） | 中 |

---

## 1. UI / UX

### 1-1. 🔴 空 DB に対する `check` が偽の「問題なし」を返し `exit 0` になる（不具合 A-01）

- **場所**: `cli.py:226-263` / `diagnosis/engine.py:255-257, 306-310` / `report.py:231-235`
- **現状**: DB は存在するがサンプル 0 件のとき、`_determine_window` が `start==end` のゼロ幅 `Window` を返す（`engine.py:306-310`、`now=time.time(); return Window(now, now)`）。`healthy = not any(... WARNING|CRITICAL ...)` は findings 空で `True`（`engine.py:255-257`）。`check` は「診断: 問題は見つかりませんでした。」を**緑字**で表示（`report.py:231-235`）し、`sys.exit(0)`（`cli.py:259-263`）。観測品質ブロックは「観測期間: 0.0 時間」と出すだけで、異常の明示がない。
- **問題**: record を数秒だけ回した／別 DB を指定した／前段の record が落ちていた、いずれも「正常・`exit 0`」と区別がつかない。CI で `exit 0` を健全と解釈すると、**データ収集失敗を緑として通す**。「正常／未観測／異常」の混同は決定論ツールにとって最も危険な出力。
- **改善案**: engine 側で「データ皆無 / `span_hours` が全ルール最小値未満」を判定し `no_data` を立てる。`check` は冒頭に「⚠ 観測データが不足しています（観測 0.0 時間）。`procdiag record` を継続してください」を出し、`exit 0` ではなく専用コード（例 `3`）。最小実装でも `cli.py:256` 付近で「findings 空かつ `span_hours` 過小」のとき stderr に警告するだけで誤読は激減。
- **根拠**: 主用途は「後から重い/落ちる理由を診断する」こと。IT 非常駐の現場では「記録が回っていなかった」が最頻の失敗で、偽の正常はそのまま見逃しに直結する。
- **影響: 高 / 工数: 小 / 種別: bug**

### 1-2. `status` が「DB はあるがデータ皆無」を正常時と同体裁で淡々と出す（A-01 と同根）

- **場所**: `cli.py:393-447`（存在チェックは `cli.py:371` のファイル有無のみ）
- **現状**: `ts_rows` が空のとき span/start/end は `'—'` のまま、サンプル数は `counts.get(...,0)` で 0、自己負荷は `'—'`。「データがまだ無い」という注記・ヒントは一切ない。JSON にも `has_data` のような明示フィールドが無い。
- **問題**: record が起動直後に落ちた／別 DB を見ている／権限で書けていない、でも「期間: — 〜 —, サンプル数: sys=0…」と並ぶだけ。`status` は本来「記録が回っているか人が確かめる」唯一のコマンドなのに、静かな記録停止を検知できない。
- **改善案**: 主要サンプル数が全て 0（または `sys_samples==0`）のとき末尾に「⚠ まだサンプルがありません。`procdiag record` が稼働しているか `--db` のパスを確認してください」を出す。JSON に `has_data: false` を追加（後方互換に注意）。
- **影響: 中 / 工数: 極小 / 種別: improvement**

### 1-3. スパイク表が生 JSON を吐き、肝心の「犯人プロセス名」を出さない

- **場所**: `report.py:181-191`（`build_charts`）/ `report.py:698-716`（HTML）/ `recorder.py:249-258`
- **現状**: `build_charts` は `spike_events` から `ts/trigger_type/psi_snapshot` の 3 列だけ取り、HTML は `psi_snapshot`（`{"cpu_some10":23.4,...}` の生 JSON）を `<small>` でそのまま表示。recorder は同じ行に `top_processes`/`top_cgroups`（スパイク時の上位プロセス/cgroup）も保存しているのに、`build_charts` はそれを読まず表にも出さない。
- **問題**: スパイク時に運用者が最も知りたいのは「そのとき何が暴れていたか」。なのに表に出るのは非 IT 人材には意味不明な JSON ブロブで、記録済みの犯人情報は**死蔵**される。スパイク即時 Tier-2 起動で犯人を記録する設計の価値が、レポートに届いていない。
- **改善案**: `charts.spikes` に `top_processes/top_cgroups` を含める。HTML では `psi_snapshot` をパースして「CPU 23% / MEM 1% / IO 9%」に整形し、別列に上位プロセスを「python(pid 1234) 87%」形式で出す。※`top_processes` は `proc_id/pid/cpu_pct/rss` のみ保持なので、プロセス**名**は `proc_registry`（`storage.proc_name(proc_id)`）の逆引きが要る。パース失敗時のみ生文字列フォールバック。
- **根拠**: 工場で「深夜に何かが暴れた」を運用者自身が突き止められるかは、この 1 列にかかる。**データは既にあり、表示するだけ**で効果が大きい。
- **影響: 高 / 工数: 小 / 種別: improvement**

### 1-4. チャートに「しきい値線」が無く、なぜその finding が出たのかをグラフから読めない

- **場所**: `report.py:345-361, 404-424`（`_polyline_svg`）/ `report.py:653-695`（呼び出し）
- **現状**: `_polyline_svg`/`_psi_svg` は折れ線と y 軸最大ラベル 1 つを描くのみ。診断ルールの判定境界（CPU/ディスク使用率・PSI レベル等）を表す水平線を一切描かない。`y_max=100` 固定の CPU/ディスクでも「どの高さを超えると警告か」が見えない。
- **問題**: 運用者は「ディスク 87% の折れ線」が安全圏か危険圏か自力判断できず、finding で「警告」と言われても緊急度を体感できない。対策後の再記録で「しきい値を割ったか」も一目で確認できない。
- **改善案**: `_polyline_svg` に `threshold: float|None` と `threshold_label` を追加し、該当 y 位置に破線（`<line stroke-dasharray="4 3">`）＋ラベル（「警告 90%」）を描く。各呼び出しで対応ルールのしきい値を渡す。PSI も代表しきい値を点線で重ねる。
- **根拠**: 決定論ルールが核である以上、その判定境界をグラフに重ねると「ルールが何を見て判断したか」が透明になる（透明性原則）。印刷して上長に見せる運用でも危険度を即共有できる。
- **影響: 高 / 工数: 小 / 種別: improvement**

### 1-5. 時系列・凡例・罫線が「色のみ」依存で、色覚多様性・モノクロ印刷で判別不能

- **場所**: `report.py:310`（`_SVG_PALETTE`）/ `report.py:446-493`（PSI 3 系列）/ `report.py:532-534`（finding 左罫線）
- **現状**: PSI の 3 系列は青/橙/緑のみで区別され、線種は全て同一実線、凡例も色付き矩形のみ。finding の左罫線も色だけ。
- **問題**: 赤緑色覚特性では橙と緑の判別が難しく、白黒プリンタに通すと 3 線が全て灰色化して完全に区別不能。閉域網は紙運用（印刷掲示・回覧）を強く含意するため実害。
- **改善案**: PSI 各線に `stroke-dasharray` を変える（CPU 実線 / MEM 破線 / IO 点線）。凡例にも同じ dash サンプルを描く。`@media print` で背景色を border＋パターンに置換。
- **確度（partial）**: サマリのバッジ（`report.py:579/581`）と検出一覧表（`report.py:597`, `sev_map` の ✘/⚠/ℹ）は**既に記号・テキスト併記済み**で色のみ依存ではない。未対応はチャート/凡例の dash 化・`@media print`・色付き `badge-*` 要素への記号付与・finding 左罫線。
- **影響: 中 / 工数: 小 / 種別: improvement**

### 1-6. 前提条件エラー（DB 不在）の終了コードが `1` で、本物の異常検出と区別できない

- **場所**: `cli.py:211-216`（DB 不在 `exit 1`）/ `cli.py:259-260`（critical/warning も `exit 1`）/ `cli.py:199-202`（docstring）/ `README.md:66-76` ＋ `report/status/export/prune` も同型
- **現状**: `check` は DB 不在でも、診断で critical/warning を検出しても、同じ `sys.exit(1)`。docstring と README の終了コード表は `0/1/2` のみで「DB 不在」を含意しない。
- **問題**: ラッパーが「`exit 1` = 異常検出」と扱うと、実際は「記録 DB が無い（＝前段 record が落ちた）」観測パイプライン障害を「ホスト異常」と誤分類する。原因クラスが真逆なのに同一コードで、無人運用の自動対応が誤った復旧を誘発する。
- **改善案**: 前提条件エラー（DB 不在・設定読込失敗）は診断結果コードと別系統に（例: 利用エラー `exit 4`、`1/2` は診断結果に限定）。docstring と README 表に追記。実装は §2-6（CLI 共通ヘルパ化）と同時に行うと一貫する。
- **影響: 中 / 工数: 小 / 種別: improvement**

### 1-7. `init` の次ステップが `doctor` を案内せず、プリフライトに導線がない

- **場所**: `cli.py:94-98`（init 出力）/ `cli.py:858-866`（doctor）/ `README.md:23-38`（Quick Start）
- **現状**: init は「1. config.py を編集 / 2. `procdiag record` で記録開始」の 2 step のみ。`doctor`（「記録開始前にホストの観測能力を診断します」）への言及がない。README の Quick Start も init→record→check で doctor を飛ばす（doctor は後方のコマンド表にだけ登場）。
- **問題**: doctor の `_check_db_disk`（`doctor.py:294`, `free_gb < 1.0` で degraded）は「DB 置き場が逼迫」を**記録前に**検知するのに、初学者の自然な導線から外れている。結果、PSI 欠損や容量不足に気づかず record を回し、後から薄いレポートに気づく。
- **改善案**: init の次ステップに `procdiag doctor --config config.py で観測能力を確認` を 1 行挿入。README Quick Start にも record の直前へ追記。
- **根拠**: 現場では「最初に出た案内通りにしか操作しない」のが現実。導線に載せるだけで非 root 降格・容量の透明性が実運用で生きる。
- **影響: 中 / 工数: 極小 / 種別: improvement**

### 1-8. 全コマンドに `--quiet/-q` が無く、自動運用で定型メッセージを抑止できない

- **場所**: `cli.py:144`（record 開始通知）/ `cli.py:248-250`（check 書き出し通知）/ `cli.py:338-343`（report）/ `cli.py:733-736`（prune）/ `cli.py:819-828`（service install）
- **現状**: これらは `click.echo` を無条件出力。`--quiet/-q`/`PROCDIAG_QUIET` はどのコマンドにもグループにも無い（is_flag は `--verbose`/`--no-color`/`--exit-zero-on-info` のみ）。
- **問題**: systemd 常駐や cron 経由で record/prune を回すと定型メッセージが journald に毎回溜まる。`check --format json --output ...` でも「レポートを書き出しました」が stdout に出て機械処理の妨げになりうる。
- **改善案**: グループ共通の `--quiet/-q`（または `PROCDIAG_QUIET`）で進捗・完了メッセージを抑止。最低限 check/export/report に。Ctrl-C 案内など安全に関わる文言は残す。
- **根拠**: 自己負荷 1% を掲げる「静かな観測者」であるべきで、JSON 出力を売りにする以上 stdout を汚さない手段は必須に近い。
- **影響: 中 / 工数: 小 / 種別: feature**

### 1-9. `doctor` の `✔/⚠/ℹ` 記号に ASCII フォールバックが無い

- **場所**: `cli.py:892-906`（`_STATUS_SYMBOL` を無条件出力、`--no-color`/`--ascii`/TTY 判定なし）
- **現状**: doctor の text 出力は Unicode 記号を無条件で使う。check 側は `sys.stdout.isatty() and not no_color`（`report.py:245`）で色制御しているのと非対称。
- **問題**: 最小構成端末（POSIX ロケール / 古い TeraTerm / シリアルコンソール / journalctl パイプ）では `✔⚠ℹ` が豆腐化し、記録前の最重要判断（degraded か否か）が読めなくなる。
- **改善案**: 非 TTY または `--ascii` 指定時に `[OK]/[WARN]/[INFO]` へ退避する `_STATUS_SYMBOL` 切替。`NO_COLOR`（§1-10）も尊重。
- **影響: 中 / 工数: 小 / 種別: improvement**

### 1-10. `NO_COLOR` 環境変数（デファクト標準）を無視している

- **場所**: `cli.py:245`（`color = sys.stdout.isatty() and not no_color`）/ `report.py:49-52`
- **現状**: 色制御は `--no-color` フラグと `isatty` のみで、`NO_COLOR`（[no-color.org](https://no-color.org) 慣習）を一切参照しない。
- **問題**: TTY だが色を嫌う環境（screen/tmux キャプチャ、色非対応ページャ、組織の標準で `NO_COLOR` 設定）で毎回 `--no-color` を付け忘れると ANSI エスケープが記録/貼り付けに混入する。出力を手順書やチケットへ転記する現場で実害。
- **改善案**: 色判定を `core/util.py` に集約（`should_use_color(no_color_flag)`）し、`os.environ.get("NO_COLOR")` を条件に追加。check/doctor 双方が使う。
- **影響: 中 / 工数: 極小 / 種別: improvement**

### 1-11. README が HTML レポートを「jinja2 製」と記載するが実装は f-string、かつ「レポートの読み方」導線が無い

- **場所**: `README.md:123` / `report.py:21,737-750`（jinja 未使用）/ `diagnosis/engine.py:28`（jinja は提案文テンプレート用）
- **現状**: README は依存に「jinja2 — HTML レポートテンプレート」と書くが、`report.py` は jinja を import せず HTML を f-string で組む。jinja2 が実際に使われるのは engine の**提案文**テンプレート。
- **問題**: 用途説明が誤りで、保守者が「HTML テンプレートを差し替えたい」と `templates/` を探しても無く混乱する。レポート章はコマンド 1 行のみで、出力の構成（①〜⑤）・印刷/共有の想定・しきい値/スパイクの読み方への導線が無い。
- **改善案**: README:123 の jinja2 用途を「提案テンプレートのレンダリング」に修正。レポート章に「HTML の構成と各チャートの読み方」小節を追加し、§1-3/1-4 の改善（犯人列・しきい値線）の説明置き場にする。
- **影響: 低 / 工数: 極小 / 種別: improvement**

---

## 2. 保守性

### 2-1. `schema_version` を書くだけで読み戻さず、列追加に対する移行経路が無い

- **場所**: `storage.py:35`（`SCHEMA_VERSION = 1`）/ `storage.py:259-284`（read_only は `_SCHEMA` 非実行、書込側は初回のみ INSERT）/ `_SCHEMA` 全テーブルが `CREATE TABLE IF NOT EXISTS`
- **現状**: 新規 DB に `schema_version=1` を 1 度書くが、開く際に現行 `SCHEMA_VERSION` と突き合わせる処理がコードベースに皆無（`grep` 確認）。`ALTER TABLE`/`_migrate_*` も存在しない。
- **問題**: 長期記録した DB を後から別バイナリで読むのが常態（USB 持ち出し・別機解析）。将来 `SCHEMA_VERSION=2` 版が、(1) 既存 DB を書込で開いても `IF NOT EXISTS` で新列は追加されず INSERT が静かに壊れる、(2) 旧 DB を新 read-only で開くと新列 SELECT が `OperationalError(no such column)` になる。
- **改善案**: 開く際に `meta.schema_version` を読み、`>` なら明示エラー、`<` なら番号付き `ALTER TABLE` 移行へディスパッチ。最低限「version mismatch 警告」分岐と、doctor の突合チェック、`test_storage` の往復テスト（write→reopen→一致）を入れる。
- **確度（partial）**: 例外は engine（`diagnosis/engine.py:217-227`）が `reasoning` に型・メッセージ付きで append するため「完全に不可視」ではない。残る正当な課題は「version 照合の不在」「既存 DB への列追加移行の不在」「運用者が reasoning の隅を読まないと気づけない」。
- **影響: 中 / 工数: 中 / 種別: improvement**

### 2-2. テスト用 synth フィクスチャが列順を手書き複製し、`TABLE_COLUMNS` と二重管理

- **場所**: `storage.py:152-239`（`TABLE_COLUMNS`）/ テスト synth 行が positional tuple ＋ インラインコメントで列順を手書き（`flush` は `TABLE_COLUMNS` 順 INSERT）
- **現状**: 本番 sampler 出力は `TABLE_COLUMNS` 順で守られるが、診断ルールテストの合成データは tuple の固定インデックスで列を並べ、`TABLE_COLUMNS` の長さ・列名と突き合わせるテストが無い。
- **問題**: 将来 `_SCHEMA` に列を足すと、synth フィクスチャが静かにズレて**別列を読み**、診断ルールテストが偽の緑になる。
- **改善案**: synth 生成を `dict(zip(TABLE_COLUMNS[t], row))` ベースに寄せるか、tuple 長と `TABLE_COLUMNS` の整合を検査するテストを 1 本追加。
- **影響: 中 / 工数: 小 / 種別: improvement**

### 2-3. `ruff` と `coverage` が dev 依存に宣言されておらず、CI 再現性とゲートが担保できない

- **場所**: `pyproject.toml:36-39`（dev は pytest/black/pre-commit のみ）/ `poetry.lock`（ruff・pytest-cov の top-level エントリ無し）/ `.pre-commit-config.yaml:36-40`（ruff は pre-commit の隔離 env で v0.15.17 ピン）/ pypi skill は「カバレッジゲート通過」を要求
- **現状**: ruff は pre-commit の隔離環境にしか入らず `poetry run ruff` は失敗。pytest-cov/coverage も lock に無くカバレッジ計測手段が無い。CI（`black-formatter.yml`）は `black --check` のみで ruff もカバレッジも回さない。
- **問題**: ①CI と pre-commit で ruff バージョンがブレる、②決定論ルールの分岐網羅（detect/non-detect）を定量管理できない、③開発者ごとに lint/coverage の有無が異なり再現性が崩れる。「カバレッジゲート通過」を謳う運用とゲートの道具が揃っていないのは不整合。
- **改善案**: `[tool.poetry.group.dev.dependencies]` に `ruff`（pre-commit と同系列）と `pytest-cov` を追加して lock 更新。`[tool.coverage]` 設定と CI の `pytest --cov=procdiag --cov-fail-under=N` を入れる。
- **根拠**: 再現性は閉域網運用ツールの生命線で、「手元では通るが別機で違う」は決定論の看板と矛盾する。
- **影響: 中 / 工数: 小 / 種別: improvement**

### 2-4. `version` の二重管理

- **場所**: `pyproject.toml:3`（`version = "0.3.0"`）と `procdiag/__init__.py:__version__`（CHANGELOG/タグも別管理）
- **現状**: リリース版数が複数箇所に手書きされ、`pypi` runbook の手順で同期している。
- **問題**: 片方更新漏れで「タグは v0.3.1 だがレポートに 0.3.0 と出る」事故が起きうる（レポート/JSON はこの値を表示する）。
- **改善案**: 単一情報源化（`importlib.metadata.version("host-check-tool")` 参照、または `__version__` を `pyproject` から生成）。
- **確度（partial）**: pypi skill 手順で運用カバー済みのため即時の不具合ではない。
- **影響: 中 / 工数: 小 / 種別: improvement**

### 2-5. CLI コマンド間で「DB 存在チェック＋config ロード」が重複（§1-6 の実装面）

- **場所**: `cli.py` の check/report/status/export/prune に同じ 3〜5 行（`load_config`→`resolved_db`→`if not exists: echo+exit(1)`）が散在（例 `cli.py:318-323`, `371-376`）
- **現状**: 同一パターンとエラー文字列が手書きで各コマンドに複製。
- **問題**: 文言更新時の一貫性喪失、例外方針（§1-6 の終了コード分離）を変えるとき修正漏れの温床、テストが煩雑。
- **改善案**: `_ensure_db_and_config(config_path, db_path) -> (cfg, db)` を 1 つ用意。DB 不在は `click.ClickException`（または専用 exit コード）に集約。§1-6 と一緒に入れると終了コード契約も同時に整う。
- **影響: 低 / 工数: 極小 / 種別: improvement**

### 2-6. `SampleBatch.rows` がタプル列順契約に依存（書き込み経路の密結合）

- **場所**: `storage.py:152-239`（`TABLE_COLUMNS` が tuple 順を定義）/ 各 sampler が tuple literal で行を構築 / `recorder.py:263-319` は `dict(zip(cols, r))` で名前復元
- **現状**: read 側（diagnosis ルール）と spike 行の復元は名前アクセス済みだが、**sampler→storage の書き込み経路は依然 positional tuple**。列順を `TABLE_COLUMNS` のコメントで人手管理している。
- **問題**: 列の追加・並べ替え時に sampler/`TABLE_COLUMNS`/buffer 変換の整合を人手で保つ必要があり、ズレても SQL は名前マッピングで通ってしまうため検知が遅れる。
- **改善案**: sampler の `collect()` を name-keyed（dict か小さな dataclass）に統一し、`storage.buffer()` が `TABLE_COLUMNS` 参照で tuple 化を一手に引き取る。型チェッカが添字ミスを拾えるようになる。
- **確度**: 前回 §3-4 で「spike 行の読み取り側」は名前アクセス化済み。本項は残る**書き込み側**の契約が対象で別物。
- **影響: 中 / 工数: 中 / 種別: improvement**

### 2-7. 非回帰ルールの確信度ラダーが各所に分散

- **場所**: `diagnosis/io_disk.py:157-164`（時間＋点数）/ `diagnosis/cpu.py:126`（周期カウント）/ `diagnosis/memory.py:201-209`（PSS slope/r2）。回帰系は `trend.py` の `confidence_from_fit` に共通化済み
- **現状**: 回帰リーク系は前回共通化されたが、外挿（DISK-FULL）・周期（CPU-PERIODIC）・PSS の確信度しきい値は各ルールに個別実装。
- **問題**: 「観測品質が低い＝確信度を落とす」の基準がルール間で不統一。ルール追加時に方針がさらに割れる。
- **改善案**: 確信度の「種別→しきい値」表を `diagnosis/` の 1 モジュールに集約（`confidence_from_observations(kind, ...)`）し、なぜ 24h/r2 0.95 かのコメントも 1 箇所に。
- **影響: 中 / 工数: 中 / 種別: improvement**

### 2-8. 細かな構造改善（低優先）

- **PSS pre-check の診断ロジックが recorder に滲む**（`recorder.py:420-498` が leak 検出相当を再実装、マジック定数も散在）。`ProcessSampler.estimate_leak_targets(...)` に静的化し、recorder は呼ぶだけ・定数は `config.sampling` 階層へ。`影響: 中 / 工数: 小`
- **engine が `store.conn.execute` で `rows()`/`series()` を bypass**（`diagnosis/engine.py:285-299` の `_determine_window`）。`Storage.window_bounds()` を追加してカプセル化（キャッシュ機構の恩恵も受けられる）。`影響: 低 / 工数: 極小`

---

## 3. 機能

### 3A. 正確性・堅牢性（現状コードに残る不具合・脆弱性）

#### 3-1. 🔴 cgroup の CPU/IO デルタが非負クランプされず、ユニット再起動で巨大な負値を会計（不具合 C-01）

- **場所**: `samplers/cgroup.py:186, 199, 203`（比較対象: `samplers/system.py:196-197,308-309` と `samplers/process.py:250-251` は `max(0, …)` 済み）
- **現状**: cgroup の `cpu_usec_delta = cpu_raw - self._prev_cpu[unit_path]` 等が**単純引き算**で、`max(0, …)` クランプが無い。`_prev_*` は `unit_path` キーのキャッシュ。system/process の全デルタはクランプ済みで、cgroup だけ抜けている。
- **問題**: systemd ユニットが再起動すると cgroup が作り直され `cpu.stat`/`io.stat` の累積カウンタが 0 に戻る。再起動直後の tick で「0 − 直前の巨大累積値」＝大きな負の delta が `cg_samples` に書かれ、`cg_rollup_1m` の `*_sum` に積算され（`storage.py:490-505`）、io_disk 診断の cgroup フォールバック（`io_disk.py:363-368`）にも入る。結果、**再起動したサービスの I/O 合計が過小**になり真犯人が top_writers から消える／物理的にありえない負バイト数が混入する。
- **改善案**: 3 か所を `max(0, raw - prev)` にクランプし、カウンタ減少（リセット）tick は delta を 0 として次の正常区間から会計を再開する。
- **根拠**: 「落ちて再起動を繰り返すサービス」こそ調査対象で、まさにそこで会計が壊れる。他の全 delta サンプラが採用済みの一貫性欠如でもある。
- **影響: 高 / 工数: 極小 / 種別: bug**

#### 3-2. 🔴 終了時に `rollup_incremental` を呼ばず、停止直前の生データがロールアップに昇格しないまま消える（不具合 C-03）

- **場所**: `recorder.py:387-390`（通常は flush→rollup の順）vs `recorder.py:589-594`（shutdown は `flush(reason="signal")` のみ）/ `storage.py:916-920`（close も flush のみ）
- **現状**: Ctrl-C/SIGTERM の終了パスは flush するだけで `rollup_incremental()` を呼ばない。`rollup_incremental` は「進行中の分」を集計しない冪等設計。
- **問題**: 停止直前の最大「flush 間隔＋1 分」ぶんの生サンプルが `*_rollup_1m` に集計されないまま終わる。その後 raw retention（既定 48h）で生サンプルが消えると、その区間のロールアップは**永久欠落**。MEM-LEAK/trend 系は rollup を主に読むため「落ちる直前の一番見たい区間」のリーク傾向が後日の診断から抜ける。短時間 `--duration` 採取では影響が顕著。
- **改善案**: shutdown ブロックで `flush(reason="signal")` の直後に `rollup_incremental()` を 1 回呼ぶ（冪等なので安全）。
- **影響: 中 / 工数: 極小 / 種別: bug**

#### 3-3. ロールアップ・ウィンドウ・retention が壁時計 `time.time()` 依存で、時計巻き戻しに脆弱

- **場所**: `storage.py:355-364`（`rollup_incremental` の watermark 早期 return）/ `storage.py:524`（watermark 前進）/ `storage.py:534-536`（retention）/ `diagnosis/engine.py:288-293`（`MIN/MAX(ts)` ウィンドウ）
- **現状**: サンプル `ts` は全て壁時計。スケジューラ自体は monotonic（設計通り）だが、行に刻む `ts` とロールアップ判定は壁時計のまま。
- **問題**: 工場エッジ PC は NTP/手動同期やサスペンド復帰で時計が飛ぶ。未来へ飛んで watermark が進んだ後に巻き戻ると、戻った時刻〜旧 watermark 間の新規サンプルが `watermark >= current_minute_floor` で「集計済み扱い」にされ**二度とロールアップされない**。`ts` 逆行で `MIN/MAX` ウィンドウが狂い `span_hours` ガード（`engine.py:211`）が誤判定する。
- **改善案**: 段階的に。(1) `rollup_incremental` で巻き戻し（watermark が `current_minute_floor` を大きく超過）を検出し `ops_events` に `clock_skew` を記録、watermark を `min(watermark, current_minute_floor)` へ補正。(2) 少なくとも検出して診断 notes に「記録中に時計ジャンプを検出。この区間の集計は不完全な可能性」と出す。まず可視化だけでも価値が高い。
- **影響: 中 / 工数: 中 / 種別: improvement**

#### 3-4. HTML レポートで時刻軸（ローカル）と生成時刻（UTC）が混在

- **場所**: `report.py:400-401, 496-497, 701`（`datetime.fromtimestamp` ＝ローカル）vs `report.py:298, 735`（`datetime.now(tz=utc)` ＝UTC）
- **現状**: チャート時刻軸とスパイク表はローカル TZ（TZ 名の明示なし）、JSON/HTML フッタの生成日時は UTC。
- **問題**: 1 枚の HTML 内でグラフの 14:00（ローカル）とフッタの「… UTC」が並び、JST(+9) では 9 時間ズレた 2 系が同居する。`fromtimestamp` はホスト TZ 依存なので、別ホストで開くと軸が変わる（閉域網で別マシンへ持ち出す用途で混乱）。
- **改善案**: レポート全体で時刻系を統一し TZ 名を明示。推奨は「記録ホストのローカル時刻＋`JST` 等の tzname」。`generated_at` も `datetime.now().astimezone()` でローカル＋tzname に揃えるか、全軸を UTC＋ラベル `UTC` に統一。
- **影響: 中 / 工数: 小 / 種別: improvement**

#### 3-5. スパイク処理の暗黙トランザクション境界が非自明（部分原子性）

- **場所**: `recorder.py:227-261`（cg/proc run→buffer→`with conn:` で spike_events INSERT→別 tx で `flush`）/ `storage.py` の接続は `isolation_level` 未指定＝暗黙トランザクション
- **現状**: `_handle_spike` は台帳 upsert（`proc_id_for`/`cg_id_for`, commit せず）→`with conn:` で spike_events INSERT（ここで台帳 upsert ごとコミット）→`flush(reason="spike")`（別トランザクションで生サンプル）。
- **問題**: 台帳 upsert と spike_events は同一コミットに入りアトミックだが、**真の非原子境界は spike_events コミットと `flush` の間**にあり、ここで電源断すると「spike_events と台帳は永続化済みだが対応する生サンプル行が欠落」しうる。複数のコミット境界（`record_event` の `with conn:`、`_record_ops_event_direct` の明示 commit、throttled UPDATE）が混在し、電源断時にどこまで永続化されるかが追いにくい。
- **改善案**: スパイク処理を「サンプラ run→buffer→spike_events INSERT を同一 `flush` トランザクションに束ねる」形にし、台帳 upsert→サンプル→spike_events を単一の原子的書き込みに。少なくとも依存するコミット順序を docstring で明示しテストで固定。
- **確度（partial）**: WAL＋`synchronous=NORMAL` のため致命的破損には至らない。影響は「再現困難な部分的不整合」に留まる。
- **影響: 中 / 工数: 中 / 種別: improvement**

#### 3-6. `gap_seconds` がジョブ数ぶん多重計上される

- **場所**: `scheduler.py:101-121`（1 イテレーション内 `now` 固定、各 due ジョブが独立に `on_gap(delay)` を発火）/ `recorder.py:500-505`（`delay` 秒を `ops_events('gap')` に記録）/ `diagnosis/engine.py:332-336`（区間マージなしの単純総和）
- **現状**: 6 つ以上のジョブが同一 `on_gap` を共有し、サスペンド復帰時に各ジョブが独立に `delay≈7200s` を 1 件ずつ記録、engine がそれを単純合算する。
- **問題**: 同一実時間ギャップが**ジョブ数ぶん多重計上**され `gap_seconds` が実ギャップより大きく出る。観測品質（この診断をどれだけ信じてよいか）の数値が系統的にズレ、運用者の判断を誤らせる。
- **改善案**: gap の集計を「ジョブごとの delay 総和」ではなく「実時間で重複しないギャップ区間の総和」に。recorder 側で「最後にどれかのジョブが正常 fire した monotonic 時刻」を 1 つ持ち復帰時に 1 件だけ記録するか、engine 側で gap イベントを区間マージしてから合計。
- **影響: 低 / 工数: 中 / 種別: improvement**

### 3B. 検出力の拡張（ロードマップ級・原則順守。`procdiag_extension_proposal.md` の未実装項目）

> いずれも **オフライン／決定論／自己負荷 1%（または record 外）／非 root 降格** の原則を崩さない。OOM-KILL-01・PSS 自動配線は**既に実装済み**（`diagnosis/crash.py` / `recorder.py:420-498`）なので対象外。

#### 3-7. 再起動跨ぎの確証度昇格（EXT-03）

- **場所/現状**: `proc_registry` に `first_seen/last_seen`（`storage.py:51-58`）、`meta` に `boot_time`（`recorder.py` 記録）はあるが、これらを突合して「再起動をまたいで同じリーク傾向が再現」を判定するルールは無い（MEM-LEAK-01 は単一ウィンドウの傾向のみ）。
- **問題**: 「定期再起動→同じプロセスでリーク再発」が続く工場 PC では、単一ウィンドウだけだと確信度が MEDIUM 止まり。再起動後も再現するなら HIGH に上げ「根本バグ確度高」を運用者に伝えたい。
- **改善案**: MEM-LEAK-01 の evidence に `boot_time` 履歴と照合した「またいだ再起動回数」を併記し、2 回以上再現で HIGH へ昇格。**追加計測ゼロ・時刻比較のみ・非 root**。
- **影響: 中 / 工数: 小 / 種別: feature**

#### 3-8. ホスト固有ベースライン（EXT-04）

- **場所/現状**: `trend.py` に `fit_linear`/`extrapolate_full` はあるが、曜日×時間帯別の median/MAD 学習は無い。`config.py` にベースライン用パラメータも無い。
- **問題**: 「CPU 平均 45% は異常か通常か」を絶対閾値だけで判断するのは脆い。現場によっては「平日の日中だけ稼働」など強い周期性があり、「普段のこの時間帯比」での相対判定が誤検知を減らす。
- **改善案**: 90 日 rollup から曜日×時間帯ビンの median/MAD（stdlib `statistics`）を学習し、Finding の evidence に「普段比 +150% / +3σ」を**併記**（絶対閾値による昇格/降格はせず併記から始める＝誤検知抑制方針を崩さない）。**record 外・決定論・非 root**。
- **影響: 中 / 工数: 中 / 種別: feature**

#### 3-9. サーマル / CPU throttle 検出（EXT-05 の一部、「なぜ重い/落ちる」直接回答）

- **場所/現状**: `samplers/system.py` は CPU 平均/最大コア・loadavg を記録するが、温度（`/sys/class/thermal` ・`psutil.sensors_temperatures()`）やクロック（`/sys/.../cpufreq/scaling_cur_freq`）を読まず、スキーマに温度/周波数列も無い。
- **問題**: 現状ルールは PSI/単一コア飽和でアプリ需要由来の重さは検出できるが、**熱スロットリング**（CPU クロックが物理的に絞られる）は識別できない。原因が「需要」か「物理制限」かで対処が根本的に違う。
- **改善案**: 低頻度（Tier-3, 10 秒）の `ThermalSampler` を追加（`sensors_temperatures` ＋ `cpufreq`、いずれも非 root 読取可・失敗時は `capabilities["thermal"]=False` で沈黙）。`THERMAL-01`: 「高温 AND 現在クロック < 最大の 0.8 AND コア負荷高」が数分継続で WARNING。**外部通信なし・決定論（温度×クロック×負荷の AND）・非 root 降格**。
- **影響: 高 / 工数: 中 / 種別: feature**

---

## 4. 処理速度

### 4A. record 常駐ループ・サンプラー（自己負荷予算 1% に直結）

#### 4-1. 🔴 サンプラ恒常失敗時に `ctx.notes` が無制限に伸びる常駐メモリリーク（不具合 D-01）

- **場所**: `core/sampler.py:86`（`notes` を 1 度生成）/ `core/sampler.py:134-138`（`run()` の except が毎 tick append）/ `recorder.py:331,357,365,373`（各周期ジョブが `sampler.run`）
- **現状**: `run()` は `collect()` の例外を握って `ctx.notes.append("… collect失敗 …")` し空リストを返す。`ctx.notes` は record ループ中に clear/上限処理される箇所が無い（`grep` で `notes.clear`・再代入なし）。さらに `report` 側はこの live notes を**そもそも読まない**（`q.notes` は engine が DB から別途構築）ため、累積は純粋な死蔵メモリ。
- **問題**: PSI が読めなくなる・特定サンプラが恒常的に OSError、等の持続失敗下では T1 が毎秒 1 エントリ append し、72h 常駐で 1 日あたり 8.6 万件×複数サンプラがメモリに積む。`selfmeter` は RSS を計測するが throttle 判定は CPU のみ（`selfmeter.py:142-143` で rss を捨てる）ため**自動減速も効かない**。最悪 OOM で観測そのものが落ちる。
- **改善案**: (a) `run()` の note 追加をサンプラ単位で直近文字列と比較し連続重複は加算しない、または (b) `ctx.notes` を `deque(maxlen=N)` に。さらに (c) 各ジョブ末尾で新規 note のみ `ops_events` に転記してから切り詰める。
- **根拠**: 閉域・無人で数日〜数週間回すのが想定運用。恒常的失敗（権限変化・カーネル設定差・FS 異常）下でこそ軽く回り続けるべきで、失敗が続くほど自分が太るのは原則違反。
- **影響: 中 / 工数: 小 / 種別: bug**

#### 4-2. 全プロセスに毎ティック `num_fds()`（/proc/<pid>/fd 走査）を実行

- **場所**: `samplers/process.py:143-146, 169-172`
- **現状**: `_job_process`（既定 10 秒）が `_pid_cache` 全 PID を走査し、各 PID で無条件に `num_fds()` を呼ぶ。`num_fds` は Linux で `/proc/<pid>/fd` を opendir+readdir する独立 syscall（`oneshot` の stat キャッシュに乗らない）。結果 `fds_max` は FD-LEAK-01 の線形回帰でのみ使う。PSS は `pss_targets` で絞り込み済みなのに `num_fds` には同種ゲートが無い。
- **問題**: `num_fds` は本サンプラ最高コストの per-process syscall。fd を多数開く常駐プロセス（DB/Web/ログ収集）では fd 走査が psutil 走査時間の支配項になりやすく、自己負荷予算 1% を最も食う。FD リーク判定は「徐々に増える傾向」を見る用途で、全プロセスを 10 秒粒度で精密計測する必要はない。
- **改善案**: 二段構え。(1) 通常 tick は `num_fds` をスキップし、FD 観測対象 PID（`pss_targets` 同型）のみ毎回。(2) 全プロセスの fds は低頻度（60〜120 秒）でだけ採取。候補選定は既存 `_job_pss_precheck` と同型で `fds_max` の傾きから選べる。
- **影響: 高 / 工数: 中 / 種別: improvement**

#### 4-3. スパイク発火時に full process 走査＋強制 fsync を T1（1 秒）スレッドで同期実行

- **場所**: `recorder.py:227-261`（`_handle_spike`）/ `recorder.py:338-350`（T1 が同期呼び出し）
- **現状**: T1（1 秒）内で trigger 発火すると `_handle_spike` を**同期**実行。中で cg/proc サンプラ run（＝full process-table 走査、4-2 の重い `num_fds` 含む）→JSON 化→spike_events INSERT→`flush(reason="spike")` で WAL コミットを強制。`process_interval`(10 秒)と無関係に 1 秒ジョブ上でプロセス全走査が走る。
- **問題**: スパイクは CPU/メモリ/IO 逼迫の瞬間に発火する。**まさにシステムが重い時に** procdiag が 1 秒スレッド上で full /proc 走査と DB コミットを同期実行する。観測のための負荷がインシデントを悪化させ、1 秒ティック自体を遅延させて gap 検出を誘発する。
- **改善案**: 重処理を T1 から外す。(a) 発火時はフラグと `psi_snapshot` だけ即記録し、full proc/cg 走査は次の T2a/T2b に優先フラグで委譲、または (b) `_handle_spike` を短命ワーカー/キューに退避。`flush(reason="spike")` の即時 fsync も WAL＋`synchronous=NORMAL` では 30 秒後の通常 flush へ委ねて耐障害性は変わらないため、強制コミットを外す。
- **根拠**: 「なぜ重い/落ちるか」を観測するツールが観測行為で重さを増すのは本末転倒。過負荷の瞬間こそ自己負荷を最小化すべき。
- **影響: 中 / 工数: 中 / 種別: improvement**

#### 4-4. 1 分ロールアップ集計を Python 側（行ロード＋辞書 group＋avg/max）で実行

- **場所**: `storage.py:347-525`（`rollup_incremental` の sys/proc/cg 3 ブロック）
- **現状**: 60 秒ごと（`recorder.py:390`）に `sys_samples`/`proc_samples`/`cg_samples` の未処理分を素の範囲 SELECT で全列ロードし、`defaultdict` で分・proc_id 単位に group 化、内包表記で None 除外して `_avg`/`_p95`/`max` を計算、1 行ずつ `INSERT OR REPLACE`。SQL 側に `AVG/MAX/SUM/GROUP BY` は無い。
- **問題**: `proc_samples` は「プロセス数×(60/process_interval)」行（数百プロセスなら毎分数千行）を Python に引き上げて辞書化・再計算する。これが 60 秒ごと 3 系統で走り、`sqlite3.Row` 生成・リスト内包・個別 INSERT が CPU と一時メモリを食う（前回 SQL 化したのは **read 経路**の `self_cpu`、この書き込み rollup は対象外で未対応）。
- **改善案**: sys/cg と proc の avg/max/sum を `INSERT INTO ..._rollup_1m SELECT (ts/60)*60 AS ts_min, AVG(...), MAX(...), SUM(...) ... GROUP BY ts_min[, proc_id]` の単一文へ。`p95` のみ SQLite に分位関数が無いので必要列だけ別途軽量取得または近似。stdlib のみ方針とも整合。
- **影響: 中 / 工数: 中 / 種別: improvement**

#### 4-5. 全プロセスに毎ティック `status()` を読み `last_zombie_count` を計算するが、結果が未使用（デッド計算）

- **場所**: `samplers/process.py:199-202, 217-219, 280`（計算）/ `diagnosis/leak.py:311-331`（ZOMBIE-01 は別経路で近似）
- **現状**: 各 PID で `proc.status()` を読み zombie を数えて `self.last_zombie_count` に格納するが、これを読む production コードは皆無（`grep` ヒットはテストのみ）。ZOMBIE-01 は `proc_samples` に status 列が無い前提で「`cpu_pct=0 AND rss=0 AND num_threads=0` のサンプル数」で zombie を粗く近似している。
- **問題**: per-process の `status()` 取得とゾンビ計数が完全に死んでいる（全プロセス×毎ティックの無駄な分岐・例外処理）。同時に、正確な status 情報を捨てて ZOMBIE-01 は粗い 3 条件近似に劣化している（速度と正確性の両損）。
- **改善案**: (A) `last_zombie_count` と `status()` 読みを削除して走査を数値メトリクスに絞る（最小・即効）、または (B) 価値があるなら `zombie_count` を 1 列永続化して ZOMBIE-01 を列ベースに切替。「計算するが使わない」状態を解消する。
- **影響: 中 / 工数: 小 / 種別: improvement**

#### 4-6. PSI を毎秒 3 ファイル分パースし、未使用トークン（avg60/avg300/total）も毎回 float 化

- **場所**: `samplers/system.py:45-87`（`parse_psi`）/ `system.py:215-236`（cpu/memory/io を毎秒 `read_text`＋full パース）
- **現状**: `parse_psi` は `some`/`full` 各行の `avg10/avg60/avg300/total` を全てパースして dict に詰める。T1（毎秒）で 3 ファイルを読み、loadavg と合わせ毎秒 4 回の /proc 読み。
- **問題**: 収集側が実際に使うのは `cpu_some10 / mem_some10 / mem_full10 / io_some10 / io_full10` の 5 キーのみ。残り（全ファイルの `avg60/avg300/total` 等）を毎秒パースして捨てている。
- **改善案**: 使用キーの allowlist で `parse_psi` を絞る（必要キーが揃ったら early-break）。毎秒×3 ファイルのパース量を削れる。
- **確度（partial）**: io 行でも `full_avg10` を使う（`system.py:233-234`）ので allowlist には `io_full10` を含めること。
- **影響: 低〜中 / 工数: 小 / 種別: improvement**

#### 4-7. PID キャッシュ更新で `set(psutil.pids())` を毎ティック 2 度生成、新規 identity で `proc_id_for` が INSERT＋SELECT の二度引き

- **場所**: `samplers/process.py:122-141, 282-286` / `storage.py:839-851`
- **現状**: 毎ティック `live_pids=set(psutil.pids())` を取り、末尾で `_pid_cache` を剪定済みにもかかわらず `set(self._pid_cache.keys())` を**再構築**して `_prev_io` を掃除する。新規 identity 解決時に `proc_id_for` が `INSERT ... ON CONFLICT` の**直後に** `SELECT proc_id` を別途実行（取得のためだけの追加ラウンドトリップ）。
- **問題**: 短命プロセスを大量 fork する環境（CI ランナー・cron 多発・ビルドサーバー＝まさに重い/落ちる調査対象）で、新規 identity あたりの SQL ラウンドトリップと set 再構築が効いてくる。
- **改善案**: (a) 末尾の set 再構築をやめ `_pid_cache.keys()` を直接使う。(b) `proc_id_for` の INSERT を `RETURNING proc_id`（SQLite 3.35+）にしてラウンドトリップを 1 回に。
- **影響: 低 / 工数: 中 / 種別: improvement**

### 4B. check / レポート読み取り側

#### 4-8. `ops_events` に索引が無く、`kind` フィルタ・retention の DELETE が全表走査

- **場所**: `storage.py:119-121`（`CREATE TABLE ops_events (ts, kind, detail)` — **PRIMARY KEY も INDEX も無し**）/ engine `_build_quality` が `kind='gap'`/`throttle` で読む / retention が `ts` で DELETE
- **現状**: sample/rollup 各表は PK 先頭列が `ts`/`ts_min` で範囲スキャンが効く（＝索引追加不要）が、`ops_events` だけは無索引。`kind` での絞り込みも `ts` での retention DELETE も全表走査。
- **問題**: `ops_events` は record 中に gap/throttle/spike/stopped が積もって伸びる唯一の無索引テーブル。check のたびに `kind` で全件走査し、retention も毎回フルスキャンで削る。データ量が増えるほど効く。
- **改善案**: `CREATE INDEX IF NOT EXISTS idx_ops_events_kind_ts ON ops_events(kind, ts);`（または最低限 `ts`）を `_SCHEMA` に追加。`SCHEMA_VERSION` を上げて §2-1 の移行経路で既存 DB にも適用。
- **影響: 低〜中 / 工数: 極小 / 種別: improvement**

#### 4-9. `build_charts` が rollup→raw フォールバックを系列ごとに繰り返し、行を複数回走査

- **場所**: `report.py:96-199`（`build_charts`）
- **現状**: CPU/最大コア/メモリを「rollup 優先・空なら raw」で 1 系列ずつ取得し（`store.series` を最大 6 回）、PSI は `rows()` を別途読む。同じ行を系列ごとに読み直す形。
- **問題**: 48h データで「同テーブル・時間範囲だが異なるクエリ引数」が増え、read-only キャッシュのキーが分散してヒット率が落ちる。1 行から複数列を取れるのに系列ごとに走査している。
- **改善案**: 先頭で「rollup を使うか raw か」を 1 度だけ確定し、`rows()` を 1 回引いて 1 ループで CPU/最大コア/メモリ/PSI を同時抽出する（行ロカリティ改善＋キャッシュキー削減）。
- **確度（要検証）**: read-only キャッシュにより実害は限定的。可読性・キャッシュ効率の改善が主目的。
- **影響: 低 / 工数: 小 / 種別: improvement**

---

## 5. 優先度まとめ

| # | 軸 | 項目 | 影響 | 工数 | 種別 | 確度 |
|---|---|---|---|---|---|---|
| 3-1 | 機能 | cgroup デルタ非負クランプ欠如（再起動で負値会計）| 高 | 極小 | **bug** | high |
| 1-1 | UI | 空 DB で偽の「問題なし」＋`exit 0` | 高 | 小 | **bug** | high |
| 3-2 | 機能 | 終了時 `rollup_incremental` 未呼び出しでデータ消失 | 中 | 極小 | **bug** | high |
| 4-1 | 速度 | `ctx.notes` 無制限肥大（常駐メモリリーク）| 中 | 小 | **bug** | high |
| 1-3 | UI | スパイク表に犯人プロセス名を出す | 高 | 小 | improvement | high |
| 1-4 | UI | チャートにしきい値線 | 高 | 小 | improvement | high |
| 4-2 | 速度 | `num_fds` 全プロセス毎ティック → 二段構え | 高 | 中 | improvement | high |
| 3-9 | 機能 | サーマル/throttle 検出（EXT-05）| 高 | 中 | feature | high |
| 1-6 | UI | DB 不在の終了コードを診断結果と分離 | 中 | 小 | improvement | high |
| 1-7 | UI | `init`/README 導線に `doctor` を追加 | 中 | 極小 | improvement | high |
| 1-2 | UI | `status` の空 DB 警告 | 中 | 極小 | improvement | high |
| 1-8 | UI | `--quiet/-q` | 中 | 小 | feature | high |
| 1-9 | UI | `doctor` の ASCII フォールバック | 中 | 小 | improvement | high |
| 1-10 | UI | `NO_COLOR` 尊重 | 中 | 極小 | improvement | high |
| 1-5 | UI | チャート色のみ依存（dash/印刷対応）| 中 | 小 | improvement | partial |
| 2-1 | 保守 | `schema_version` 照合＋移行経路 | 中 | 中 | improvement | partial |
| 2-3 | 保守 | `ruff`/`coverage` を dev 依存に宣言 | 中 | 小 | improvement | high |
| 2-2 | 保守 | synth フィクスチャの列順整合テスト | 中 | 小 | improvement | high |
| 2-6 | 保守 | SampleBatch 書き込み経路の name-key 化 | 中 | 中 | improvement | high |
| 2-7 | 保守 | 非回帰ルールの確信度ラダー共通化 | 中 | 中 | improvement | high |
| 3-3 | 機能 | 時計巻き戻し検出（`clock_skew`）| 中 | 中 | improvement | high |
| 3-4 | 機能 | HTML 時刻系の TZ 統一 | 中 | 小 | improvement | high |
| 3-5 | 機能 | スパイク書き込みの原子境界 | 中 | 中 | improvement | partial |
| 3-7 | 機能 | 再起動跨ぎ確証度昇格（EXT-03）| 中 | 小 | feature | high |
| 3-8 | 機能 | ホスト固有ベースライン併記（EXT-04）| 中 | 中 | feature | high |
| 4-3 | 速度 | スパイク重処理を T1 から退避 | 中 | 中 | improvement | high |
| 4-4 | 速度 | rollup 集計の SQL 化 | 中 | 中 | improvement | high |
| 4-5 | 速度 | zombie `status()` デッド計算の除去 | 中 | 小 | improvement | high |
| 4-8 | 速度 | `ops_events` への索引 | 低〜中 | 極小 | improvement | high |
| 1-11 | UI | README の jinja2 記載修正＋読み方導線 | 低 | 極小 | improvement | high |
| 2-4 | 保守 | `version` 単一情報源化 | 中 | 小 | improvement | partial |
| 2-5 | 保守 | CLI 前提条件チェックの共通ヘルパ | 低 | 極小 | improvement | high |
| 2-8 | 保守 | PSS pre-check の責務分離 / engine の `conn` bypass | 中/低 | 小/極小 | improvement | high |
| 3-6 | 機能 | `gap_seconds` 多重計上の是正 | 低 | 中 | improvement | high |
| 4-6 | 速度 | PSI パースの allowlist 化 | 低〜中 | 小 | improvement | partial |
| 4-7 | 速度 | PID set 二重生成 / `proc_id_for` 二度引き | 低 | 中 | improvement | high |
| 4-9 | 速度 | `build_charts` の単一走査化 | 低 | 小 | improvement | 要検証 |

---

## 6. 既に対応済み（再掲しない）

`procdiag_improvement_review.md`（#1〜#10, v0.2.0→v0.3.0）で対応済みのため本書では扱わない。現状コードで解消を確認した:

- `check --format html` のチャート空バグ（`build_charts` を `with` 内で呼ぶ）／`build_charts` の except を `(sqlite3.Error, OSError)` に限定し `logger.warning` で可視化。
- `config.rules.extra_rules` の配線／HTML タイトルへの記録済み hostname 反映（`meta_hostname`）。
- `check` の `exit 2`（info のみ）到達可能化＋`--exit-zero-on-info`／`is_python` 判定の厳格化。
- 連続飽和ラン検出の `trend.py` 抽出／回帰リーク検出の確信度ラダー共通化（`confidence_from_fit`）／`proc_registry` 名前引きの Storage ヘルパ化／`live_keys` デッドコード除去。
- read-only rows キャッシュ／**read 経路の** `self_cpu` 統計の SQL 化／MEM-LEAK の無駄な `sys_rollup_1m` 全件読み削除／cgroup `last_seen` UPDATE の間引き。
- `enforce_size_budget` の握り潰しに記録/ログ追加／`exec()` config に対する権限 doctor チェック。
- 既に実装済みで本書の検出力拡張からは除外: **OOM-KILL-01**（`diagnosis/crash.py`, journald 昇格つき）、**PSS 精査の自動配線**（`recorder.py:420-498`＋`memory.py` の確信度昇格）。

### 検証で棄却した誤指摘（記録）

- 「`doctor` コマンド未実装」→ **実在**（`cli.py:858-906`, `doctor.py` 405 行）。
- 「`db_max_size_mb` が未有効」→ **実在**（`config.py:141`, 既定 2048MB／`min_free_disk_mb` 512MB）。
- 「`proc_rollup_1m`/`proc_samples` に `ts` 索引が必要」→ PK 先頭列が `ts`/`ts_min` のため範囲スキャンは効く。真に無索引なのは `ops_events` のみ（§4-8）。

---

### 付記: 本書の作成方法と限界

- 各指摘は「現状コードを Read → `file:line` を実コードに照合 → 既対応でないことを確認」する敵対的検証を通している。`確度: partial` は「中心主張は成立するが現状説明の一部に不正確さがある／一部既対応」、`要検証` は「方向は妥当だがキャッシュ等で実害が限定的」を意味する。
- 影響/工数は相対評価。実装前に各 `file:line` を再確認し、テスト（特に診断ルールの detect/non-detect）を先に固定してから着手することを推奨する。
