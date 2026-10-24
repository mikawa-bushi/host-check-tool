# Changelog

本プロジェクトの主な変更点を記録します。形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に準拠し、[セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## [Unreleased]

### Fixed
- **CPU-SATURATION-01 が実データで常に失敗していた**: 飽和区間にスパイクイベントが 1 件でもあると `sqlite3.Row.get()` で `AttributeError` となり、エンジンの防御壁に握り潰されて検出結果が出なかった (v0.1.0 から)。加えてレコーダーが書く `top_processes` の形式 (`{"by_cpu": [...]}`) を読めていなかった。記録形式を `procdiag/spikes.py` に一本化し、各スパイクの上位プロセス (proc_id→名前解決) を集計して犯人プロセスを示すようにした。
- 設定 `rules.journal_size_warn_gb` が DISK-JOURNAL-01 に反映されず、常に既定の 4GB で判定していた。`Rule.configure(config)` フックを追加し、エンジンが評価前に設定を渡すようにした。
- `procdiag list-samplers` / `procdiag explain` に thermal サンプラーが表示されなかった (サンプラーパッケージの登録漏れ)。
- HTML レポートのディスクチャート見出しでマウントポイント名が二重エスケープされていた。
- README の systemd 手順のユニット名を実体 (`procdiag-record.service`) に合わせた。
- `procdiag doctor` の温度センサ未検出時の説明を、実装済みの THERMAL-01 が無効になる旨に更新。

### Changed
- `status` / `export` / `prune` が `--config` を受け付け、DB の場所や保持期間を設定ファイルから解決するようになった (`--db` 指定が優先)。
- `procdiag status` が sys_samples 全行を読み込まず SQL 集計 (期間・自己負荷・直近イベント) で表示するようになった。長期記録の DB でも即座に応答する。
- `procdiag --version` が `procdiag.__version__` を表示するようになった (レポート/JSON の版数表記と一致)。

### Internal (挙動不変のリファクタリング)
- サンプラーの行生成を位置タプル手書きから `storage.make_row(table, **columns)` による列名指定に変更 (列の過不足は即例外)。
- 累積カウンタ→差分の非負クランプを `core.util.CounterDelta` に集約 (system / cgroup サンプラー)。
- `ProcessSampler.collect` を PID キャッシュ更新・oneshot 読取・I/O 読取・識別子登録・I/O 差分の各ステップに分割。
- `Recorder` の Tier ジョブを `_run_sampler(name)` に統合。スパイク行の書き込みは `Storage.record_spike`、メタ情報は `Storage.write_meta` 経由に。
- PSS 精査対象の選定ロジックを recorder から `diagnosis.memory.select_pss_targets` へ移し、MEM-LEAK / FD-LEAK / THREAD-LEAK と共通の `trend.group_series` / `fit_each` / `days_until` を使用。
- エンジンの観測品質集計を `Storage.self_cpu_stats` / `missing_io_ratio` に置き換え、`Storage.conn` の直接参照を削減。
- `report.py`: SVG 描画の共通部 (枠・座標変換・しきい値線・時刻ラベル) を共通化し、`render_html` をセクション単位の関数に分割。チャートのしきい値線はルールクラスの値を参照 (出力は従来とバイト単位で同一)。
- `cli.py`: `--db` / `--config` / `--quiet` オプション定義、レジストリ読み込み、check/report の診断処理、終了コード判定を共通関数化。
- `doctor.py`: 各チェックの名前/ラベル付与と例外の降格処理をデコレータに集約 (出力文言は不変)。
- バイト単位 (`KiB`/`MiB`/`GiB`) を `core.util` に集約。未使用 import・変数を除去し、ruff の検査対象に pyflakes (`F`) を追加。

## [0.3.0] - 2026-06-14

### Changed
- **終了コードの挙動変更** (#6): `procdiag check` が info レベルのみの Finding のとき、終了コード **2** を返すようになった（従来は誤って 0 を返しており、ドキュメント記載の「2 = info のみ」が到達不能だった）。CI で従来どおり 0 を期待する場合は新オプション `--exit-zero-on-info` を付ける。warning/critical は引き続き 1、問題なしは 0。
- `humanize_bytes`（`status` 等のサイズ表示）を SI(1000) から **binary(1024)** 基数に変更し、`df -h` / `free -h` と数値が一致するようにした (#6)。

### Added
- `procdiag check --exit-zero-on-info`: info のみのとき終了コード 0 を返す CI 向けオプション (#6)。

### Fixed
- HTML レポートのタイトルが、`report.hostname_label` 未設定時に記録済みの `meta.hostname` を使うようになった（従来は常に既定タイトル）。複数ホストのレポートを区別しやすくなる (#6)。
- メモリリーク提案の「Python プロセス」判定を厳格化（`startswith("python")`）。`copy` / `scapy` / `pypy` などへの誤マッチで py-spy 提案が出る問題を解消 (#6)。

## [0.2.4] - 2026-06-14

### Fixed
- `build_charts`: raw `sys_samples` のみの DB（ロールアップ前）で、PSI・ディスク・スパイクのチャートが無言で欠落していた。`sqlite3.Row` に存在しない `.get()` を呼んでいたためで、広域 `except` がその例外を握り潰していた (#5)。

### Changed
- 堅牢化 (#5)。
  - `build_charts` / `enforce_size_budget` の広域 `except Exception` を想定 I/O 例外（`sqlite3.Error` / `OSError`）に限定し、失敗を `logger` と `ops_events`（`db_guard_error`）に記録。想定外の例外は伝播させ、不具合を隠さない。
  - `procdiag doctor` に「設定ファイルの安全性」チェックを追加（world-writable な `config.py` を degraded として警告）。
- `config.py` は `exec()` で実行される点について、安全な配置（root 所有・非 world-writable）のガイドを docs に追記 (#5)。

## [0.2.3] - 2026-06-14

### Changed
- 性能改善（結果不変） (#3)。
  - read-only セッションで `Storage.rows()` の結果をクエリ単位でキャッシュし、複数ルールが同一ウィンドウを共有（テーブルの重複スキャン解消）。
  - 観測品質の self_cpu 平均/最大を全行ロードから SQL 集計に変更。
  - cgroup `last_seen` の UPDATE をユニット毎 60 秒に間引き（記録時の定常書き込みを削減）。

## [0.2.2] - 2026-06-14

### Changed
- 内部リファクタ（挙動不変）で保守性を改善 (#4)。
  - CPU-SINGLECORE-01 / CPU-SATURATION-01 の連続飽和ラン検出を `trend.find_sustained_episodes` に共通化。
  - MEM-LEAK / FD-LEAK / THREAD-LEAK の確信度ラダーを `trend.confidence_from_fit` に共通化。
  - `proc_registry` / `cgroup_registry` の名前引きを `Storage.proc_name` / `Storage.cg_unit_path` に統一。
  - スパイク記録のタプル添字アクセスを `TABLE_COLUMNS` 列名アクセスへ。
  - 未使用コード（`live_keys`、MEM-LEAK の冗長な `sys_rollup_1m` 読み）を削除。

## [0.2.1] - 2026-06-13

### Fixed
- `procdiag check --format html`: `build_charts` が `Storage` の `with` ブロックを抜けた後（接続クローズ後）に呼ばれており、生成 HTML の時系列チャートが常に「データなし」になっていた。`report` コマンドと同じくブロック内で構築するよう修正 (#1)。
- `config.rules.extra_rules`: 設定で案内されているユーザー定義ルールの import パスがどこからも読み込まれず黙殺されていた。`Engine.run` 冒頭で読み込むよう配線。無効なパスでも診断は継続し、失敗は推論ログ（`--verbose` / JSON）に記録される (#1)。
- 観測品質の「欠測 gap 合計」がイベント件数を秒として表示していた。各 gap イベントに記録された遅延秒を合計する正しい意味に修正 (#1)。

## [0.2.0] - 2026

### Added
- クラッシュ診断の強化（OOM-KILL-01 ほか）と、導入・運用時の安全装置（`doctor`、DB サイズガード等）。

## [0.1.1] - 2026

### Fixed
- E2E テストのフレーク修正。
- PyPI ページ向けに README を更新。

## [0.1.0] - 2026

### Added
- 初回リリース。3 階層サンプラー + スパイクトリガ + 自己計測による常駐記録、決定論ルールによる診断、端末 / JSON / 自己完結 HTML レポート。
