# Changelog

本プロジェクトの主な変更点を記録します。形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に準拠し、[セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## [Unreleased]

### Added
- **`procdiag snapshot`**: 記録なしで数秒のうちに現在のホスト状態を網羅チェック (CPU・メモリ・I/O の圧迫、ディスク容量・inode・読み取り専用化、ファイルハンドル、ゾンビ・上位プロセス、failed / 自動再起動したサービス、直近 24 時間のカーネル異常、温度、時刻同期、記録の稼働状況)。warning / critical があれば exit 1。判定しきい値は各ルールの値 (`rules.overrides` 込み) を流用。
- **`procdiag bundle`**: 記録 DB (記録中でも一貫したコピー)・HTML/JSON レポート・snapshot・doctor・status・config を 1 つの tar.gz にまとめる (USB 持ち出し解析用)。秘密情報を含み得るプロセスのコマンドラインは既定で削除 (`--keep-cmdline` で保持)。DB が無くても snapshot / doctor だけのバンドルを作成。
- **ルールのしきい値上書き `rules.overrides`**: 各ルールの数値属性を `{"CPU-SATURATION-01": {"psi_threshold": 30}}` の形で上書きできる。存在しないルール ID・項目は起動時にエラー (exit 4)。`procdiag explain <RULE-ID>` が調整可能なしきい値と既定値を表示。HTML のしきい値線も上書き後の値で描画。
- 新ルール 5 件 (スキーマ版 4):
  - **DISK-INODE-01**: inode の使用率・増加傾向 (容量に空きがあっても書けない状態)
  - **FS-READONLY-01**: 記録中にファイルシステムが読み取り専用へ切り替わった (FS エラーの典型)
  - **UNCLEAN-SHUTDOWN-01**: 停止処理なしの再起動 (電源断・カーネルパニック等) と記録プロセスの強制終了
  - **SERVICE-RESTART-01**: systemd の自動再起動 (NRestarts) の増加・failed 遷移からクラッシュループを検出
  - **KERNEL-ERROR-01**: カーネルログの I/O エラー・FS エラー・ハングタスク・MCE・segfault・USB 切断・温度制限
- 新サンプラー: `service` (systemd、5 分毎、変化時のみ記録) / `kernel_log` (journalctl -k を前回の続きから、5 分毎、異常行のみ記録)。設定 `sampling.service_interval` / `sampling.kernel_log_interval`。
- 診断結果に**評価範囲**を表示: 観測時間不足で評価できなかったルールと必要な残り時間を端末・HTML の先頭に出し、JSON にも `evaluated_rules` / `skipped_rules` / `errored_rules` を追加。
- 閉域網向けオフライン導入: `bin/build_wheelhouse.sh` (導入先の Python 版・CPU 向け wheel 一式と install.sh を作成) と手順書 `docs/offline-install.md`。
- CI に pytest (Python 3.10 / 3.12、カバレッジ 85% 以上) と ruff を追加 (`.github/workflows/tests.yml`)。

### Fixed
- **短時間の記録でも「問題は見つかりませんでした」と表示されていた**: 15 ルール中 8 件が観測時間不足で評価されていなくても、その旨は `--verbose` でしか分からなかった。評価範囲を常に表示し、一部未評価のときは「評価できた範囲では問題は見つかりませんでした」(HTML は「問題なし (一部未評価)」) とした。
- **記録開始のたびに自己負荷の自動減速が誤発火していた**: 起動直後の一回限りのコスト (初回の全プロセス走査など、実測 1 秒目に約 9%) が 1% の予算を超え、毎回 `throttle_down` が記録されてプロセス走査間隔が 10 分以上広がっていた。起動後 `budget.warmup_sec` (既定 60 秒) は判定から除外。
- ZOMBIE-01 が cpu/rss/threads=0 の近似でしか判定できなかった。プロセス状態 (oneshot 内でほぼ無料) からゾンビ数を実測して記録し、確信度を MEDIUM に (旧 DB は従来の近似で継続)。
- FD-LEAK-01 が固定値 (1024/4096) で上限到達日数を見積もっていた。プロセスの実際の RLIMIT_NOFILE を記録して使用 (取得できない場合のみ既定値)。提案文が台帳 ID を PID として `/proc/<id>/limits` を案内していた誤りも修正。
- WAL 形式でない DB (他ツールでコピーした DB など) を読み取り専用で開けなかった。
- スキーマ版が新しすぎる DB を開いて失敗したときに SQLite 接続が閉じられていなかった。
- CI の分岐フィルタ `'*'` が `/` を含むブランチ名に一致せず、`feature/...` 等への push で CI が動いていなかった (`'**'` に修正)。
- **CPU-SATURATION-01 が実データで常に失敗していた**: 飽和区間にスパイクイベントが 1 件でもあると `sqlite3.Row.get()` で `AttributeError` となり、エンジンの防御壁に握り潰されて検出結果が出なかった (v0.1.0 から)。加えてレコーダーが書く `top_processes` の形式 (`{"by_cpu": [...]}`) を読めていなかった。記録形式を `procdiag/spikes.py` に一本化し、各スパイクの上位プロセス (proc_id→名前解決) を集計して犯人プロセスを示すようにした。
- 設定 `rules.journal_size_warn_gb` が DISK-JOURNAL-01 に反映されず、常に既定の 4GB で判定していた。`Rule.configure(config)` フックを追加し、エンジンが評価前に設定を渡すようにした。
- `procdiag list-samplers` / `procdiag explain` に thermal サンプラーが表示されなかった (サンプラーパッケージの登録漏れ)。
- HTML レポートのディスクチャート見出しでマウントポイント名が二重エスケープされていた。
- README の systemd 手順のユニット名を実体 (`procdiag-record.service`) に合わせた。
- `procdiag doctor` の温度センサ未検出時の説明を、実装済みの THERMAL-01 が無効になる旨に更新。

### Changed
- スキーマ版 4: `disk_samples` に inode 数・読み取り専用フラグ、`proc_registry` に FD 上限の列を追加し、`proc_counts` / `service_events` / `kernel_events` / `sessions` テーブルを新設。既存 DB は書き込み時に自動移行 (列追加は再実行しても安全)、読み取り専用で開いた旧 DB では新ルールが黙ってスキップされる。
- `procdiag record` が SIGHUP (端末を閉じた) でも正常停止するようになった。
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
- ルール生成と設定適用 (不正な上書き時は既定値に戻して理由を返す) を `core.configure_rule` に集約し、engine / report / snapshot で共用。
- journalctl / systemctl の呼び出しを `procdiag.systemd.run` に、カーネルログ行の分類を `procdiag.kernlog` に集約 (OOM-KILL-01・KERNEL-ERROR-01・kernel_log サンプラー・snapshot で共用)。
- doctor の JSON 項目を `DoctorCheck.to_dict` に集約 (`doctor --format json` と bundle の doctor.json で共用)。CLI の ASCII 表示判定を `_use_ascii` に共通化。
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
