# Changelog

本プロジェクトの主な変更点を記録します。形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に準拠し、[セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

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
