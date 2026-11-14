# Changelog

本プロジェクトの主な変更点を記録します。形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に準拠し、[セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## [Unreleased]

### Fixed
- 全体設定 `rules.min_observation_hours` を 6 時間より短くすると、MEM-LEAK-01 / FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01 は評価済みと表示されるのに、6 時間より短い記録ではリークを報告しなかった。評価するかどうかは設定した時間で決めていたが、ルールの計算は 6 時間のままだった。ルールの計算にも設定した時間を使う。6 時間より長くしたときは、記録の途中から動き始めたプロセスなど、設定した時間に満たないプロセスの系列ではリークを報告しなくなる (同じ時間を `rules.overrides` でルールごとに設定したときと同じ結果)。
- 名前や引数に UTF-8 として読めないバイトを含むプロセスが 1 つ動いていると、その間、全プロセスの記録 (proc_samples / proc_counts) が黙って止まっていた。Shift_JIS の引数のほか、普通の UTF-8 の日本語名でも、カーネルが 15 バイトで文字の途中を切ると起きる。`check` はリーク・OOM・ゾンビのルールを評価できないまま「評価できた範囲では問題は見つかりませんでした」と出していた。読めないバイトを `\x91` のような 16 進の表記にして記録する (例: `1号機温度\xe7\x9b`)。
- マウント先のパスに UTF-8 として読めないバイトがあると、以後の記録の書き込みがすべて失敗し、何も記録されなかった。同じように 16 進の表記にして記録する。
- `procdiag compare` で、修正後の期間が修正前より短いと、1 日に数回起きるサービスの再起動やカーネルのエラーなどがたまたま起きず、「解消」と表示して終了コード 0 で終わっていた。リーク系以外のルールでは、修正後の期間が修正前と同じ長さ以上のときだけ「解消」とする。短いときは「判定不能」(終了コード 3) とし、あと何時間記録すれば比べられるかを示す。`--at` では修正後を 24 時間記録すれば比べられる。修正前の方が短いときも、修正後にだけ出た検出を「新規」とは呼ばず「判定不能」とする。
- `procdiag compare` で、リークしていたプロセスが修正後に動いていないと、同じ名前で別のコマンドラインのプロセスと名前だけで突き合わせ、「解消」と表示して終了コード 0 で終わっていた。名前だけで突き合わせた行は、両方の値を示したまま「判定不能」(JSON の `why` は `paired_by_name`) とし、そのことを行の下に書く。
- `procdiag compare` で、行は「変化なし」「悪化」なのに、結論は「修正後に warning / critical の検出はありません」で、終了コードは 0 だった。しきい値の 5.8 倍の値にも「改善 (しきい値未満)」と付いていた。修正後の値がしきい値以上なのに R² が低くて検出されなかったときは、「改善 (しきい値以上)」と表示する。「変化なし」「悪化」「改善 (しきい値以上)」は修正後も問題が残っているものとして、結論を「修正後もまだ問題があります」にし、終了コードを 1 にする。
- システムのジャーナルを読めないユーザー (root でも systemd-journal / adm グループでもない) が `procdiag snapshot` や `procdiag quick` を実行すると、カーネルログが「異常なし」と出ていた。journalctl は読めなくても、空の出力で正常終了することがあるため。記録と同じ方法で読めるかを確かめ、読めなければ「読み取れません」と出す。
- `procdiag snapshot` で systemctl が失敗すると (D-Bus が止まっている、応答しないなど)、「failed のユニット」と「自動再起動したサービス」が「✔ なし」と出ていた。「確認できません」と出し、手で確かめるコマンドを示す。
- カーネルログに温度による制限の行だけがあると、`procdiag snapshot` は warning にして終了コード 1 で終わっていた。`procdiag check` の KERNEL-ERROR-01 は同じ行を info にする。snapshot も KERNEL-ERROR-01 と同じ重大度を使う。
- `procdiag doctor` のジャーナルの確認が、どのユーザーでも「読取可能」と出ていた (/var/log/journal の権限しか見ていなかった)。記録と同じ方法でカーネルログを読めるかを確かめる。読めないと無効になるのは KERNEL-ERROR-01 の記録と OOM-KILL-01 の確証度の昇格なので、そう表示する。ジャーナルの読み取りと関係のない DISK-JOURNAL-01 を「有効」と書かなくなった。
- `procdiag doctor` が、書き込めないフォルダや読み取り専用の DB でも「記録に十分な容量があります」と出し、まとめの行で「記録は可能」と言っていた。`record` はそこで失敗する。書き込めるかを確かめ、書き込めなければ降格にして、まとめの行で「記録 DB の置き場を先に確認してください」と出す。空き容量のしきい値は 1 GB 固定をやめ、`retention.min_free_disk_mb` (既定 512 MB) と比べる。この値を下回ると、記録は古いデータを早めに削除し始める。また、`doctor --db` と `bundle --db` が、存在しない DB のフォルダを作っていた。作らなくなった。
- `procdiag doctor` が I/O 欠損率を先頭の 80 プロセスだけで測っていた。非 root ではそのほとんどが読めないカーネルスレッドなので、実際は 80% 台でも約 100% と出ていた。記録と同じく全プロセスで測る。
- THERMAL-01 が、期間中の最高温度と期間中のクロックの中央値という、別々の時刻の値を比べていた。省電力で待機する PC が全速のまま一時だけ熱くなると「サーマルスロットリングの疑い」と警告し、8 時間 92°C でクロックが半分に落ちていても、残りの時間が全速なら何も報告しなかった。記録ごとに温度とクロックを突き合わせ、80°C 以上で最大クロックの 85% 未満の記録が 3 回以上続いた期間を報告する。報告には合計時間も出す。
- CPU 温度として、NVMe・GPU・Wi-Fi などを含むすべての温度センサーの最高値を記録・表示していた。SSD が熱いだけで THERMAL-01 が CPU のサーマルスロットリングを疑い、`procdiag snapshot` は「CPU 温度」に SSD の温度を出して冷却の確認を勧め、終了コード 1 で終わっていた。CPU のセンサー (coretemp・k10temp・zenpower・cpu_thermal など) だけを読み、CPU のセンサーが無いときは ACPI と SoC の温度ゾーン (acpitz・soc_thermal) を使う。どれも無ければ温度は記録しない。0.6.0 までに記録した DB の温度は、すべてのセンサーの最高値のまま。

## [0.6.0] - 2026-11-10

### Added
- **`procdiag quick`**: 初めての人がこれだけ実行すれば診断まで終わるコマンド。いまの状態の確認 (snapshot)、短い記録 (既定 1 時間、`--duration`)、その記録の診断 (check と同じルール) を続けて行い、記録 DB と HTML レポートを `--out` (既定 `./procdiag-quick-<日時>`) に置く。記録中の Ctrl-C は記録を早めに終えるだけで、それまでの記録を診断する。最後に、snapshot で warning / critical だった項目をもう一度表示する。終了コードは check と同じで、snapshot に warning / critical があるときも 1。出力先を使えないときと、レポートを書けなかったときは 4。`procdiag --help` の冒頭で案内する。
- `procdiag check` に `--since` / `--until` を追加。診断する期間を選べる (`--until` の時刻は含まない)。1 分集計しか残っていない時間帯も診断でき、生データの無い時間帯があるときは、生データだけで評価したルールを観測品質の備考に示す。期間に記録が無いときは終了コード 3。

### Changed
- `check` の観測期間に、診断した期間の開始と終了の時刻を表示する。JSON に `window` (`start` / `end`、データが無いときは `null`) を追加。既存のキーは変更なし。
- 既定の期間 (生データの残っている期間) より前の記録があるときは、そのことと `--since` の使い方を観測品質の備考に出す。これまでは、1 分集計が何十日分あっても、診断したのが直近の生データの期間だけだと分からなかった。
- `check -o report.html` (HTML) で期間を指定したときは、グラフもその期間だけを描く。
- `procdiag check --output` が拡張子から出力形式を決めるようになった (`.html` / `.htm` → HTML、`.json` → JSON、それ以外は text)。これまでは `--format` を付け忘れると `check --output report.html` が端末向けのテキストを .html ファイルに書いていた。`--format` を指定すればそちらが優先する。`--output` の短縮形 `-o` を追加。
- `procdiag explain` (引数なし) の一覧を、ルールはカテゴリ別にタイトルつきで、サンプラーは Tier 別に記録間隔の設定名とタイトルつきで表示するようにした (`list-rules` / `list-samplers` と同じ表示)。

### Removed
- 重複していたコマンドを削除。削除した名前を実行すると、代わりのコマンドを示して終了コード 4 (前提条件エラー) で終わる。check の 2 (info のみ) と区別するため。
  - `procdiag report` → `procdiag check -o report.html` (JSON は `-o report.json`)
    - `report` は所見があっても終了コード 0 で終わっていた。`check` は critical / warning があると 1、info だけなら 2 で終わる。cron などで常に 0 が必要なら `|| true` を付ける。
  - `procdiag list-rules` / `procdiag list-samplers` → `procdiag explain`
  - `procdiag advise` (`check` の隠し別名) → `procdiag check`

### Fixed
- `procdiag --help` のコマンド一覧に `check` が出ていなかった。隠し別名 `advise` を隠す設定が、同じコマンドの `check` まで隠していた。
- 設定ファイルが無い・読めない・文法が誤っている・`config` 変数が無い・`config` の型が違うときに、各コマンドがエラーメッセージではなく Python のトレースバックを出して終了コード 1 で止まっていた。メッセージを表示して終了コード 4 で終わる。
- `rules.overrides` で MEM-LEAK-01 / FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01 の最低観測時間 (`min_observation_hours`) を 6.0 に上書きしても、全体設定 `rules.min_observation_hours` が 6.0 以外だと上書きが無視され、全体設定の時間が使われていた。
- `procdiag compare --at` で、区切り時刻の直前 1 ミリ秒以内に記録されたサンプルが、修正前と修正後のどちらの期間にも入っていなかった。
- 記録や `--since` / `--until` の期間が、ルールに必要な観測時間ちょうどだと時間不足として扱われていた。6 時間ちょうどの記録では、MEM-LEAK-01 / FD-LEAK-01 / THREAD-LEAK-01 が評価済みと表示されるのにリークを報告しなかった。6 時間ちょうどの期間ではリーク系 4 ルールが、24 時間ちょうどの期間では MEM-PRESSURE-01 が、観測時間不足でスキップされていた。`procdiag compare` でも、6 時間ちょうどの期間ではリーク系 4 ルールが評価されず、リークがあっても「どちらの期間にも問題は検出されませんでした」と表示して終了コード 0 で終わっていた。観測時間は最初と最後の記録の間隔で測るので、記録した時間より最大 2 分近く短くなる。必要な時間まで 2 分以内のときは足りているとみなす。評価したルールは、この差のためにリークを見逃したり確信度を下げたりしない。

## [0.5.0] - 2026-11-06

### Added
- **`procdiag compare`**: 対策の前後 (1 つの DB を `--at` で分ける / `--before` `--after` で期間を指定する / 2 つの DB) を同じルール・しきい値で比べ、検出ごとに 解消 / 改善 / 変化なし / 悪化 / 新規 / 判定不能 を示す。リーク系 4 ルールは検出が消えた期間でも値を測り、「RSS 増加率 38.0 → 0.2 MB/h」のように前後の数値を並べる。記録時間不足・生データの保持期間外・古い版の DB などで判定できないものは理由つきで「判定不能」とし、終了コード 3 で知らせる。出力は text / json (`"format": "procdiag-compare/1"`)。
- タグ `vX.Y.Z` の push で PyPI へ公開する GitHub Actions ワークフロー (`.github/workflows/publish.yml`)。trusted publishing (OIDC) を使うため PyPI のトークンをどこにも保存しない。タグと `pyproject.toml` の版が一致し、タグのコミットが `main` 上にあって CI に通っていることを確かめてからビルド・公開する。配布ファイルには PEP 740 の来歴証明 (attestations) を付ける。

### Changed
- `check` の JSON に `uncovered_rules` (入力データが無く実行しなかったルールと理由) を追加。既存のキーは変更なし。端末・HTML の評価範囲でも「記録データが無く未評価」として数え、一覧を表示する。
- 観測品質の「欠測 gap 合計」と「減速イベント」を DB 全体ではなく観測期間内で数えるようにした。期間より長く記録した DB では値が小さくなる。
- スキーマ版 3 より前の DB で THERMAL-01 が「実行エラー」になっていたのを「記録なしで未評価」にした。
- CI で試す Python を 3.10 / 3.12 から、対応範囲の最古と最新の 3.10 / 3.14 に変更。

### Fixed
- **古い版の DB を読むと一部のルールが黙って「問題なし」になっていた**: スキーマ版 4 より前の DB では SERVICE-RESTART-01 / KERNEL-ERROR-01 / DISK-INODE-01 / FS-READONLY-01 / UNCLEAN-SHUTDOWN-01 が必要な表・列が無いまま評価済みとして扱われていた。各ルールが必要な表・列を宣言し、エンジンが満たさないルールを「記録なしで未評価」にする。
- **生データが削除された期間やロールアップしかない DB で、生データを読むルールが評価済みになっていた**: CPU-SATURATION-01 / IO-BOTTLENECK-01 / SWAP-THRASH-01 / OOM-KILL-01 / ZOMBIE-01 などが読む表に期間内の行が無くても「問題なし」に数えていた。期間内に行が無いルールは「記録なしで未評価」にする。
- `docs/procdiag.md` の終了コード表に 3 (観測データなし) と 4 (前提条件エラー) が載っていなかった。
- README の詳細ドキュメントへのリンクが相対パスで、PyPI のページからたどれなかった。

## [0.4.0] - 2026-11-05

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
- **起動直後に記録を始めると最初の 5 分間 cgroup (サービス単位) が記録されなかった**: サービス一覧のキャッシュ時刻の初期値 0 を、起動からの経過秒である `time.monotonic()` と比べていたため、OS 起動後 5 分以内に開始すると (systemd で起動時に自動開始する標準構成が該当) 初回の一覧取得が飛ばされていた。起動直後の CI ランナーでテストが失敗して発覚。
- ZOMBIE-01 が cpu/rss/threads=0 の近似でしか判定できなかった。プロセス状態 (oneshot 内でほぼ無料) からゾンビ数を実測して記録し、確信度を MEDIUM に (旧 DB は従来の近似で継続)。
- FD-LEAK-01 が固定値 (1024/4096) で上限到達日数を見積もっていた。プロセスの実際の RLIMIT_NOFILE を記録して使用 (取得できない場合のみ既定値)。提案文が台帳 ID を PID として `/proc/<id>/limits` を案内していた誤りも修正。
- **OOM-KILL-01 の journal 照合が UTC 以外のホストで時間帯をずれて検索していた**: UTC の時刻文字列を journalctl に渡しており、journalctl はそれを現地時刻と解釈するため、日本時間のホストでは 9 時間前の範囲を照合して確証度の昇格 (HIGH) がほぼ働いていなかった。時刻を `@epoch` 形式で渡すよう修正し、OOM 行の判定もカーネルログ分類 (`kernlog`) と共通化。
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
- CI を GitHub Actions の無料枠に収まるよう整理: Black のワークフローを `tests.yml` (CI) に統合して `black --check` を 1 回だけ実行し、失敗時に Black で整形して自動コミットするジョブは廃止 (整形は手元の `black .` / pre-commit で行う)。PR との二重実行・ドキュメントのみの push での実行をやめ、同じブランチの古い実行は中断、ジョブは 10 分で打ち切る。手動実行 (`workflow_dispatch`) に対応。
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
