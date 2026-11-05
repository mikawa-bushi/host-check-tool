# procdiag 改善レビュー（処理速度 / 保守性 / エラーハンドリング / UX）

対象: `procdiag` パッケージ全体（v0.2.0, 2026-06-13 時点 `main`）
観点: **処理速度・保守性・エラーハンドリング・UX** の 4 軸。各項目に「場所 / 現状 / 問題 / 改善案 / 理由」を付す。

> **対応状況 (2026-06-14時点): 本レビューの全項目は対応済み。**
>
> | 区分 | Issue / PR | リリース |
> |---|---|---|
> | §1-1 / §1-2 / §4-2（確実な不具合3件） | #1 / PR #2 | v0.2.1 |
> | §3 保守リファクタ | #4 / PR #7 | v0.2.2 |
> | §2 速度 | #3 / PR #8 | v0.2.3 |
> | §4-1 / §4-3 堅牢化 | #5 / PR #9 | v0.2.4 |
> | §5 UX | #6 / PR #10 | v0.3.0 |
>
> 実装中に2件の潜在バグも発見・修正した: (a) `build_charts` が `sqlite3.Row` に `.get()` を呼んでおり raw DB でチャートが無言欠落していた (PR #9)、(b) `check` の終了コード `2`(info のみ) が到達不能だった (PR #10)。

---

## 0. 総評

全体として完成度は高い。レイヤ分離（`samplers` → `storage` → `diagnosis` → `report`）、依存注入による高いテスト容易性（461 テスト関数）、サンプラー／ルールの defensive wall、`from __future__ import annotations` と型注釈の徹底、ドキュメント文字列の充実――いずれも保守を強く意識した設計になっている。

したがって本レビューは「動かない」レベルの問題ではなく、**1 件の実証済みバグ**と、**長期運用・拡張時に効いてくる中〜小の改善**が中心になる。優先度は §6 の表を参照。

---

## 1. 実証済みの不具合（最優先）

### 1-1. `check --format html` がチャート空のレポートを生成する 🔴

- **場所**: `procdiag/cli.py:217-227`
- **現状**:
  ```python
  with Storage(resolved_db, read_only=True) as store:
      engine = Engine(cfg, verbose=verbose)
      diag = engine.run(store, only=only_list, skip=skip_list)

  if fmt == "json":
      text = report_mod.render_json(diag)
  elif fmt == "html":
      charts = report_mod.build_charts(store)   # ← with を抜けた後＝store は閉じている
  ```
  `build_charts(store)` が `with Storage(...)` ブロックの**外**で呼ばれており、その時点で SQLite 接続は `close()` 済み。
- **問題**: `build_charts` は内部を広域 `except Exception: pass`（`report.py:176`）で囲っているため、`Cannot operate on a closed database` を握り潰し、**例外を出さずに空の `ChartData` を返す**。結果として `procdiag check --format html` は「時系列チャートがすべて『データなし』の HTML」を出力する。`procdiag report`（`cli.py:307-310`）は `with` 内で呼んでいるため正常。
- **実証**（閉じた store で `build_charts` を呼ぶ再現）:
  ```
  cpu points after closed-store build_charts: 0
  cpu points with OPEN store: 120
  ```
- **改善案**: `build_charts` 呼び出しと `render_html` を `with` ブロック内へ移す（`report` コマンドと同じ構造に揃える）。
- **理由**: ユーザーから見ると静かに壊れている（エラーにならないので気づけない）。広域 except がバグを隠した典型例で、§4 のエラーハンドリング方針とも直結する。

### 1-2. `config.rules.extra_rules` が配線されておらず黙殺される 🟠

- **場所**: 定義 `procdiag/config.py:175`・テンプレ `config.py:288`、関数 `procdiag/plugins.py:59 load_extra_rules`、呼び出し側 `cli.py:65`
- **現状**: エントリポイント方式の `load_plugins()` は CLI 起動時に呼ばれるが、`load_extra_rules(config.rules.extra_rules)` は**どこからも呼ばれていない**。`grep` で参照ゼロ。
- **問題**: `config.py` のテンプレートには「ユーザー定義ルールのimportパス」として `extra_rules=[]` が案内されているのに、ここに値を入れても**何も起きない**。設定が嘘をついている状態。
- **改善案**: `Engine.__init__` または `cli` の `check/report` 実行前に `load_extra_rules(cfg.rules.extra_rules)` を呼ぶ。失敗は `(name, ok, msg)` を集めて `--verbose` 時に表示。
- **理由**: 拡張ポイントとして公開しているものが機能しないと、利用者は原因不明のまま時間を溶かす。デッドコードでもあり保守上の負債。

---

## 2. 処理速度

### 2-1. 同一テーブルを複数ルールが個別フルスキャン

- **場所**: `proc_rollup_1m` を `memory.py:90`(MEM-LEAK) / `leak.py:113`(FD-LEAK) / `leak.py:238`(THREAD-LEAK) がそれぞれ `store.rows(..., window)` で全件読み込み、Python 側で proc_id ごとに再グルーピング。`sys_samples`/`sys_rollup_1m` も CPU 系・メモリ系ルールがそれぞれ読み直す。
- **問題**: 1 回の `check` で同じウィンドウの同じテーブルを 3 回以上 `fetchall()` して同じ group-by を 3 回行う。90 日 rollup だと proc 行数は大きくなり得る。
- **改善案**: `Engine.run` で「ウィンドウ内の `proc_rollup_1m` / `sys_samples` / `sys_rollup_1m`」を 1 度だけ読み、proc_id ごとに前処理した辞書を `evaluate(store, window, shared)` のように共有コンテキストで渡す。あるいは `Storage` に `proc_rollup_grouped(window)` のキャッシュ付きヘルパを置く。
- **理由**: ルール数は今後増える前提（拡張提案あり）。1 ルール = 1 フルスキャンのままだと線形に遅くなる。

### 2-2. 集計を Python で回している（SQL で済む）

- **場所**: `engine.py:296-305`（`_build_quality` が `sys_samples` 全行を取得して `self_cpu` の avg/max を Python で計算）。`crash.py:208` も `sys_rows` 全件取得。
- **問題**: 48h × 1s = 約 17 万行を毎回 Python リストに展開してから平均を取る。`SELECT AVG(self_cpu), MAX(self_cpu) FROM sys_samples WHERE ts BETWEEN ? AND ?` なら DB 内で完結し、転送・GC コストが消える。
- **改善案**: 単純統計（avg/max/count/sum）は SQL 集計に寄せる。OOM ルールのように最近傍探索が要る箇所は全件取得が妥当なので対象外。
- **理由**: メモリ確保と Python ループが自己負荷（`overhead_budget_pct`）に乗る。診断側は record と違い予算管理外なので特に効く。

### 2-3. cgroup の `last_seen` UPDATE が毎ティック・ユニット分発行される

- **場所**: `cgroup.py:210`（毎 `collect` で全ユニットに `resolve_cg`）＋ `storage.py:813-820`（キャッシュヒット時も `UPDATE cgroup_registry SET last_seen=?` を実行）
- **現状**: プロセス側（`process.py`）は PID 単位で identity をキャッシュし、既知 PID では `resolve_proc` を呼ばないので UPDATE が出ない。cgroup 側は**毎ティック全ユニットに UPDATE** が出る。
- **問題**: ユニット 100 個・5 秒間隔なら 20 UPDATE/s。`flush` までトランザクションに溜まるので fsync は増えないが、SQL 実行自体は無駄。
- **改善案**: `last_seen` を「N 分に 1 回だけ更新」または「flush 時にまとめて 1 本の `UPDATE ... WHERE unit_path IN (...)`」に集約。プロセス側と同じく書き込み頻度を間引く。
- **理由**: 常駐デーモンの定常負荷を下げる。自己負荷予算 1% を守るうえで地味に効く。

---

## 3. 保守性

### 3-1. 「連続飽和分の検出」ステートマシンが 2 ルールで重複

- **場所**: `cpu.py:215-264`（CPU-SINGLECORE）と `cpu.py:432-470`（CPU-SATURATION）
- **問題**: `run_start/run_end/run_len/prev_bucket` を使った「連続する分バケットのラン検出（90s ジッタ許容・末尾フラッシュ）」が約 50 行ほぼコピペ。閾値判定だけ違う。
- **改善案**: `trend.py` に `find_sustained_episodes(buckets, is_saturated_fn, min_minutes) -> list[(start, end)]` を抽出し、両ルールから呼ぶ。
- **理由**: 片方だけバグ修正して他方が取り残される事故を防ぐ。テストも 1 箇所に集約できる。

### 3-2. 「線形回帰リーク検出」の定型が 3 ルールで重複

- **場所**: `memory.py:85-229`(MEM-LEAK) / `leak.py:109-198`(FD-LEAK) / `leak.py:234-`(THREAD-LEAK)
- **問題**: 「`proc_rollup_1m` を proc_id でグルーピング → `fit_linear(min_points=12)` → slope/r2 で足切り → 確信度ラダー → 名前引き → Finding 生成」がほぼ同形。特に確信度ラダー
  ```python
  if fit.r2 >= 0.95 and fit.span_hours >= 12.0: HIGH
  elif fit.r2 >= 0.90 and fit.span_hours >= self.min_observation_hours: MEDIUM
  else: LOW
  ```
  は `memory.py:134-139` と `leak.py:150-155` 等で**完全一致のコピー**。
- **改善案**: `confidence_from_fit(fit, min_hours)` を共通化。さらに「rollup 列名・slope 単位変換・Finding テンプレ」をパラメータ化した基底ルール（例 `_MonotonicGrowthRule`）にまとめると 3 ルールが各 20 行程度になる。
- **理由**: 閾値ポリシーの変更が 1 箇所で済む。レビュー時の認知負荷が下がる。

### 3-3. `proc_registry` 名前引きが各所にインライン散在

- **場所**: `memory.py:121` / `cpu.py:296` / `io_disk.py:335` / `leak.py:144,269` / `crash.py:258` / `recorder.py:489` で同じ `SELECT name FROM proc_registry WHERE proc_id=?` を直書き。
- **改善案**: `Storage.proc_name(proc_id) -> str`（見つからなければ `f"proc_{proc_id}"`）を 1 つ用意して全置換。同様に `cgroup_registry` 引きも `unit_path_for(cg_id)` に。
- **理由**: SQL 文字列の散在はスキーマ変更時の修正漏れ温床。フォールバック規則（`proc_{id}`）も統一できる。

### 3-4. タプル添字でのスパイク行アクセスが列順に密結合

- **場所**: `recorder.py:272-290`（`r[3]=cpu_pct, r[4]=rss` 等の固定インデックス）/ `_top_cgroups_json` も同様
- **問題**: `storage.TABLE_COLUMNS["proc_samples"]` の順序が変わると静かに別列を読む。コメントで列順を手書き管理している。
- **改善案**: `dict(zip(TABLE_COLUMNS["proc_samples"], row))` で名前アクセスにするか、`SampleBatch` に列名を持たせる。
- **理由**: 列追加・並べ替えは将来あり得る。マジックインデックスは最も壊れやすい結合。

### 3-5. デッドコード `live_keys`

- **場所**: `procdiag/samplers/process.py:283`
  ```python
  live_keys = {(proc_id, pid) for pid in self._pid_cache for proc_id in []}  # 常に空集合・未使用
  # Simpler: just keep keys whose pid is still in cache.
  live_pid_set = set(self._pid_cache.keys())
  ```
- **改善案**: `live_keys` 行を削除（リファクタの取り残し。コメントも「Simpler:」と自認している）。
- **理由**: 読み手を混乱させる。0 行で同義。

### 3-6. `MEM-LEAK-01` の `days_to_oom` に無駄な全件読み

- **場所**: `memory.py:143-145`
  ```python
  mem_row = store.rows("sys_rollup_1m", since=..., until=...)  # 真偽判定にしか使われない
  if mem_row:
      sys_last = store.rows("sys_samples", ...)  # 実際の計算はこちら
  ```
- **問題**: `mem_row` は `if mem_row:` のゲートにしか使われず、計算には `sys_last` を使う。`sys_rollup_1m` 全件取得が純粋に無駄。さらに proc ごとにこの 2 本の読みが走る（2-1 とも複合）。
- **改善案**: `mem_row` 読みを削除し、`sys_last` の取得をループ外で 1 回だけ行う。
- **理由**: 処理速度（2-1/2-2）と保守性の両面で効く明確な無駄。

---

## 4. エラーハンドリング

### 4-1. 広域 `except` が回復可能でない不具合まで握り潰す

- **場所**: `report.py:176`（`build_charts` 全体）、`storage.py:621-624, 632-636`（`enforce_size_budget` の `retention`/`VACUUM`）
- **問題**:
  - `build_charts` の広域 except は §1-1 のバグを隠した。チャート構築の「部分失敗で空シリーズ」は仕様だが、**プログラミングエラー（閉じた接続）**まで同じ網で捕る設計だと検知不能になる。
  - `enforce_size_budget` は retention 失敗を黙殺し `ops_events` にも残さない。retention が継続的に失敗すると DB が無制限に膨らみ、誰も気づけない。
- **改善案**: 「想定済みの I/O 例外（`OSError`, `sqlite3.OperationalError`）」だけを捕り、それ以外は通す。最低でも except 節で `logger.exception(...)` か `record_event("error", ...)` を残す。
- **理由**: defensive wall は「1 ティックの失敗でループを殺さない」ためのもの。**沈黙して誤った成功を返す**のは別問題で、本ツールの売りである「観測品質の可視化」と矛盾する。

### 4-2. `gap_seconds` が「秒」ではなく「件数」

- **場所**: 生成 `engine.py:293` `gap_seconds = float(len(gap_rows))`、表示 `report.py:242` `"欠測 gap 合計 : {q.gap_seconds:.0f} 秒"`、実データ `recorder.py:503` `detail=f"{delay:.1f}s ジョブ{job_name}"`
- **問題**: gap **イベント数**を `gap_seconds` という名前に入れ、レポートで「N 秒」と表示している。実際の遅延秒（`delay`）は `ops_events.detail` の文字列に入っているのに集計時に捨てている。「gap 3 秒」と出ても実態は「gap が 3 回」。
- **改善案**: `record_event("gap", ...)` の detail を構造化（または専用列）して `delay` 秒を保持し、`_build_quality` で `SUM(delay)` を取る。表示と意味を一致させる。フィールド名も実態に合わせる。
- **理由**: 観測品質は「結果をどれだけ信頼してよいか」の指標。そこに誤った数字が出ると判断を誤らせる。

### 4-3. `load_config` の `exec()`（セキュリティ注記）

- **場所**: `config.py:333` `exec(compile(source, ...), namespace)`
- **現状**: netdiag 由来の意図的設計で、設定に Python 式を書けるのが利点。ただし `procdiag service install` でデーモン化される文脈では、`config.py` の書き込み権限＝任意コード実行権限になる。
- **改善案**: 仕様維持で良いが、(a) ドキュメントに「config.py は信頼できる場所に root 所有で置く」ことを明記、(b) systemd ユニット生成時に config パスの所有者/パーミッションを doctor でチェックする項目を追加すると堅牢。
- **理由**: 「ローカルファースト前提なら許容」だが、運用ガイドが無いと現場で 0644 の共有パスに置かれ得る。

---

## 5. UX

### 5-1. HTML レポートが記録済みホスト名を使わない

- **場所**: `report.py:549-550`
  ```python
  meta_hostname: str | None = None      # ← 常に None のまま
  title = hostname_label or meta_hostname or "procdiag レポート"
  ```
- **問題**: recorder は `meta` テーブルに `hostname` を保存している（`recorder.py:204`）のに、`render_html` は store を受け取らず meta を読まない。`config.report.hostname_label` を設定しない限りタイトルは常に「procdiag レポート」。
- **改善案**: `render_html` か呼び出し側で `store.meta().get("hostname")` を渡し、`hostname_label or meta_hostname or 既定` の順で解決。複数ホストのレポートを並べたとき区別できる。
- **理由**: 工場で複数 PC を診断する用途（`hostname_label` 例「ライン制御PC」が示す通り）では、どのホストのレポートか一目で分かることが重要。

### 5-2. `is_python` 判定が広すぎる

- **場所**: `memory.py:169` `is_python = "python" in name.lower() or "py" in name.lower()`
- **問題**: `"py"` 部分一致は `copy`, `scapy`, `happy`, `spyder`, `pypy` などにも誤マッチし、無関係プロセスに py-spy 提案が出る。
- **改善案**: `name.lower() in {"python", "python3", "python2"} or name.lower().startswith("python")` のように厳格化。
- **理由**: 提案の信頼性は本ツールの中核価値。誤った道具（py-spy）を勧めると一気に信用を失う。

### 5-3. `check` の終了コード 2（info のみ）が CI を落とし得る

- **場所**: `cli.py:238-244`（0=正常 / 1=warning・critical / 2=info のみ）
- **現状**: docstring に明記済みで意図的。ただし `set -e` や単純な `if procdiag check; then` では「info しか無い健全寄りの状態」でも失敗扱いになる。
- **改善案**: 仕様は維持しつつ README / `--help` に「終了コード 2 は info あり（異常ではない）」を目立たせる。または `--exit-zero-on-info` のようなオプションを用意。
- **理由**: 自動化に組み込む運用が想定される以上、終了コードの意味は強く周知すべき。

### 5-4. `humanize_bytes` が SI（1000 基準）

- **場所**: `core/util.py:61`（`KB=1000`）
- **問題**: `free -h` / `df -h` は 1024 基準。`procdiag status` の表示が OS ツールと数 % ずれ、突き合わせ時に違和感。
- **改善案**: メモリ/ディスクは 1024 基準（KiB/MiB 表記）に揃えるか、表示を OS 慣行に合わせる。
- **理由**: 「手元の `df` と数字が合う」ことは運用者の安心材料。小さいが体験に効く。

---

## 6. 優先度まとめ

| # | 区分 | 項目 | 影響 | 工数 | 優先度 |
|---|------|------|------|------|--------|
| 1-1 | バグ | `check --format html` がチャート空（store クローズ後に `build_charts`） | 機能が静かに壊れる | 小 | **最高** |
| 1-2 | 配線 | `config.rules.extra_rules` が未配線で黙殺 | 公開拡張点が無効 | 小 | **高** |
| 4-2 | エラー/UX | `gap_seconds` が件数なのに「秒」表示 | 観測品質の誤情報 | 小 | **高** |
| 4-1 | エラー | 広域 except が不具合を隠す（build_charts / size_budget） | 検知不能な失敗 | 中 | 高 |
| 3-5 | 保守 | デッドコード `live_keys` 削除 | 可読性 | 極小 | 中 |
| 3-6 | 速度/保守 | MEM-LEAK の無駄な `sys_rollup_1m` 全件読み | 軽い無駄 | 極小 | 中 |
| 5-2 | UX | `is_python` の `"py"` 部分一致を厳格化 | 誤提案 | 極小 | 中 |
| 2-1 | 速度 | 同一テーブルの複数ルール個別フルスキャン | 拡張時に線形劣化 | 中 | 中 |
| 3-1 | 保守 | 連続飽和ラン検出の重複を `trend` に抽出 | 修正漏れ防止 | 中 | 中 |
| 3-2 | 保守 | リーク検出の定型／確信度ラダー共通化 | 修正漏れ防止 | 中 | 中 |
| 3-3 | 保守 | `proc_registry` 名前引きを `Storage` ヘルパ化 | 散在排除 | 小 | 中 |
| 5-1 | UX | HTML タイトルに記録済み hostname を反映 | 多ホスト識別 | 小 | 中 |
| 2-2 | 速度 | 単純統計を SQL 集計へ | 自己負荷低減 | 小 | 低〜中 |
| 2-3 | 速度 | cgroup `last_seen` UPDATE の間引き | 定常負荷低減 | 小 | 低 |
| 3-4 | 保守 | スパイク行のタプル添字を名前アクセスへ | 結合度低減 | 小 | 低 |
| 5-3 | UX | 終了コード 2 の周知 / オプション化 | CI 事故防止 | 小 | 低 |
| 5-4 | UX | `humanize_bytes` を 1024 基準へ | OS ツールと整合 | 小 | 低 |
| 4-3 | セキュリティ | `exec()` config の運用ガイド／doctor チェック | 権限事故防止 | 小 | 低 |

---

### 補足: 既に良くできている点（変更不要）

- サンプラー／ルールの `try/except` defensive wall（`core/sampler.py:127`, `engine.py:190`）は「1 失敗でループ/診断を殺さない」目的に対して適切。§4-1 はその網が**広すぎる**特定箇所のみが対象で、方針自体は妥当。
- `TierScheduler` のドリフトレス・スケジューリングと 3×interval の gap 再アンカー（`scheduler.py:108-129`）。
- `rollup_incremental` のウォーターマーク方式（O(新規データ) で冪等, `storage.py:329`）。
- read-only を SQLite URI（`mode=ro`）で OS レベル強制（`storage.py:254`）し、write/read 経路を物理分離。
- PSI 非対応・非 root・cgroup v1 などの降格を `ctx.notes` と `doctor` で利用者に明示する一貫した姿勢。
