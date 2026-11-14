# procdiag — local-first Linux ホスト資源診断ツール

`procdiag` は**事後診断**ツール。リアルタイムダッシュボードではなく、
**一定期間かけて記録し、決定論的なルールで改善提案を生成する**。

答える問いは「**なぜこの PC が重い・落ちる・ディスクが満杯か**」。
Glances/netdata 等のリアルタイム監視とも、py-spy 等のコードプロファイラとも異なり、
運用レベル (プロセス・サービス・ディスク) の原因切り分けと改善提案に徹する。

```bash
# インストール
pip install host-check-tool

# 3コマンドで診断
procdiag init
procdiag record --duration 24h
procdiag check
```

---

## Design in one picture

```
samplers/          storage/            diagnosis/          report/
(事実の記録)         (時系列DB)           (解釈)               (提示)

SystemSampler ─┐
CgroupSampler ─┤   SQLite WAL        ルールエンジン        結論ファースト text
ProcessSampler─┼──▶ バッファ書き  ──▶  (Finding生成)   ──▶ JSON (scripts/CI)
DiskSampler   ─┘   増分ロールアップ    Finding: 原因/       自己完結HTML (offline)
                   保持ポリシー        提案/確信度
              ↑
        SpikeTrigger
        (PSIスパイク検出
         → Tier2a/2b即時起動)
```

3層は意図的に疎結合:

- **Samplers** はOSから「事実」を記録するだけで解釈しない。
  失敗は1ティックの欠損として降格し、全体を止めない。
- **Storage** は WAL + バッファ書き + 増分ロールアップで自身の I/O フットプリントを最小化する。
- **Diagnosis** は Storage に問い合わせて決定論的ルールで Finding を生成する。
  `min_observation_hours` 未満の記録には発火しない。
- **Report** は HTML を完全自己完結で生成する (CDN 参照ゼロ、閉域網対応)。

---

## Quick start

```bash
# カレントディレクトリに config.py と data/ を生成
procdiag init

# config.py を必要に応じて編集してから記録を開始
procdiag record --duration 24h

# 別ターミナルで状況確認
procdiag status

# 記録終了後に診断
procdiag check

# HTML レポートを書き出す (拡張子が .json なら JSON)
procdiag check -o report.html
```

---

## コマンドリファレンス

| コマンド | 目的 |
|---|---|
| `procdiag quick` | 初めての人向け。snapshot・短い記録 (既定 1 時間、`--duration`)・診断を 1 回で行い、記録 DB と HTML レポートを `--out` (既定 `./procdiag-quick-<日時>`) に置く。Ctrl-C で記録を早めに終えても診断する。snapshot か診断に warning/critical があれば exit 1 |
| `procdiag snapshot` | 記録なしで現在のホスト状態を網羅チェックする (warning/critical で exit 1) |
| `procdiag init [DIR]` | `config.py` と `data/` を生成する |
| `procdiag record` | 常駐ループで 3 層サンプリングを実行する |
| `procdiag check` | 記録済み DB に診断ルールを実行し改善提案を出力する。`-o report.html` / `-o report.json` でファイルに書き出す |
| `procdiag compare` | 対策の前後 (1 つの DB の 2 期間、または 2 つの DB) を同じルール・しきい値で比べる |
| `procdiag status` | 記録期間・サンプル数・自己負荷の概況を表示する |
| `procdiag doctor` | 記録開始前にホストの観測能力 (PSI/cgroup v2/権限/I-O読取率/カーネルログの読取/センサ/DB の置き場の書き込み権限と空き容量) を診断する。空き容量は `retention.min_free_disk_mb` と比べる。降格ありで exit 2 |
| `procdiag explain [名前]` | ルールまたはサンプラーの詳細を表示する。引数なしでルールをカテゴリ別、サンプラーを Tier 別に一覧表示する |
| `procdiag export` | 記録データを CSV または JSON でエクスポートする |
| `procdiag prune` | 保持ポリシーを手動実行する。記録中にも実行できる |
| `procdiag bundle` | 記録 DB・レポート・snapshot・doctor を 1 つの tar.gz にまとめる (持ち出し解析用) |
| `procdiag service print` | systemd ユニットファイルを標準出力に表示する |
| `procdiag service install` | user systemd ユニットを `~/.config/systemd/user/` に配置する |

### record の主要オプション

```
--duration 24h   # 記録時間 (例: 60s / 1h / 24h)。省略時は無期限
--config FILE    # 設定ファイル (既定: ./config.py。他のユーザーが書き換えられるときは実行しない)
--db FILE        # DB ファイルパス (config の db_path を上書き)
```

`db_path` の相対パスは、設定ファイルのあるフォルダからの位置、`~` はホームフォルダに展開する。
設定ファイルが無いときの既定 `./procdiag.db` と、`--db` の相対パスは、カレントフォルダからの位置である。
`--config ""` / `--db ""` のように空の値を渡すと、既定に戻さず終了コード 4 で終わる。
`--since ""` / `--until ""` も同じである。

### service print / install

systemd は `$HOME` (user ユニット) や `/` (system ユニット) から起動し、`ExecStart=` を独自の引用規則で分けるため、
ユニットに書く `--config` / `--db` は、実行したフォルダからの絶対パスにして引用する。
`--duration` は `60s` / `24h` / `90d` の形式だけ受け付け、誤りはユニットを書かずに終了コード 4 で終わる。
`--config` も `--db` も無い system ユニットは、`StateDirectory=procdiag` を付けて `/var/lib/procdiag/procdiag.db` に記録する。

ユニットは `Restart=on-failure` だが、`record` が終了コード 4 (設定・DB・オプションの誤り) で終わったときは
`RestartPreventExitStatus=4` で再起動しない。再起動しても直らず、10 秒ごとに失敗を繰り返すだけだからである。
終了コード 1 (DB に書けない) は、空きができれば直るので再起動する。

DB に書き込めない間 (ディスク満杯・I/O エラー・読み取り専用の DB ファイル) は、書けるようになるまで
記録をメモリに残す。残すのは新しい方から 5 万行 (約 14 MB、200 プロセスのホストで約 30 分) まで。
それより古い記録は捨て、捨てた行数と期間を、次に書けたときに ops_events (`data_lost`) に残す。
`check` の観測品質の備考と `status` の直近イベントに出る。書けない間も、保持ポリシーと DB サイズ
ガードは毎時動く。記録を始めるときに DB に書き込めないとき、停止するときに残りの記録を書けなかった
ときは、理由を表示して終了コード 1 で終わる。

### check の主要オプション

```
--format text|json|html   # 出力フォーマット (既定: --output の拡張子から判断し、それ以外は text)
-o, --output FILE         # ファイルに書き出す (.html/.htm → html、.json → json)
--only MEM-LEAK-01,...    # 指定ルールのみ実行
--skip ZOMBIE-01,...      # 指定ルールをスキップ
--verbose                 # 推論過程を表示
```

`--only` / `--skip` (`compare` も同じ) と `config.rules.disabled` のルール ID は、`procdiag explain` の一覧どおり大文字で書く。存在しない ID を書いたとき、`rules.disabled` で無効にしたルールを `--only` に書いたとき、実行するルールが 1 つも残らないときは、診断せずに終了コード `4` で終わる。

### 終了コード (check)

| コード | 意味 |
|---|---|
| `0` | Finding なし (正常) |
| `1` | critical / warning の Finding あり |
| `2` | info レベルの Finding のみ |
| `3` | 観測データなし (DB にサンプルが1件もない＝記録が回っていない、または `--since` / `--until` の期間に記録がない) |
| `4` | 前提条件エラー (DB ファイルが無い・開けない、設定ファイルが無い・読み込めない、出力先に書けない、コマンドラインの誤り等)。`1` の異常検出と区別される |

CI で info を失敗扱いにしたくない場合は `procdiag check --exit-zero-on-info` を使うと、info のみのときに `0` を返す（`1` / `0` の判定は変わらない）。

画面と HTML の判定の言い方は終了コードに合わせる。`0` で 1 つ以上のルールを評価できたときだけ「(評価できた範囲では) 問題は見つかりませんでした」と出し、info のみ (`2`) のときは「info の検出が N 件あります」と提案も出す。記録が無い (`3`) ときと、どのルールも評価できなかったとき (観測時間が足りないなど。終了コードは `0`) は、問題なしとは言わず、その旨を出す。

JSON の `healthy` は、記録があり、1 つ以上のルールを評価でき、warning / critical の Finding が無いときだけ `true` になる (info は `healthy` を下げない。info を失敗扱いにするのは終了コードの側)。記録が無いときは `false`。測っていない観測品質 (自己負荷・I/O 欠損率) は 0 ではなく `null` で、画面と HTML には「未計測」と出る。

プロセス名・マウントポイント・サービス名・カーネルログの行など、ホストから来た文字列の制御文字と改行は、画面・`-o` のファイル・HTML では `\x1b` や `\n` のように見える形で出力する (画面を消す、タイトルを書き換える、「問題は見つかりませんでした」の行を偽装する、を防ぐ)。提案のコマンドに入るホストの名前はシェル用に引用符で囲み、サービス名として使える名前にだけ `systemctl restart` を案内する。

`4` は他のコマンドでも同じ意味で使う。`status` / `export` / `bundle` / `prune` / `record` / `init` は、DB が開けない (新しい版・壊れている・SQLite でない・ディレクトリ)・出力先が無い、または書けない・`prune --raw` / `--rollup` の形式が違う・`init` の作成先に作れないときに、理由を 1 行で示して `4` で終わる。
未知のコマンドやオプション、値の抜け、`--format` の誤り、余分な引数も `4` で、`2` (info のみ) と取り違えない。
`-o` / `--output` / `--out` の出力先は診断の前に確かめ、ファイルは書き終えてから置く (途中で失敗しても壊れたファイルを残さない)。`bundle --out` にはファイルを指定する。
記録を止めた後の DB は、書き込めないフォルダにあっても読める。記録が動いている間は、記録と同じユーザーで実行する。
`procdiag export | head` のように読み手が先に閉じたときは、何も表示せず `141` で終わる。

### 診断する期間 (check)

`check` は、既定では生データ (既定の保持期間 48 時間) の残っている期間を診断する。1 分集計はそれより長く (既定 90 日) 残るので、それより前の記録があるときは、観測品質の備考にそのことが出る。観測期間の行には、診断した期間の開始と終了の時刻が出る (JSON では `window`)。

観測期間の時間 (JSON では `quality.span_hours`) は、記録のあった時間だけを数える。システムの記録 (生データまたは 1 分集計) が 5 分を超えて途切れた間 (PC の電源を切っていた夜・記録を止めていた間・スリープ・時計が進められた間) は観測していないので、ルールの最低観測時間にも数えず、`欠測 gap 合計` に入れ、観測品質の備考に途切れた時刻を示す。記録の設定 (DB に残る `sampling.system_interval`) の 3 倍が 5 分より長い記録では、その長さを超えて途切れた間を数える (6 分ごとの記録なら 18 分)。それより短い遅れは、記録側が残した遅延 (`ops_events` の `gap`) を `欠測 gap 合計` に足す。増加の傾き (MEM-LEAK-01 / FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01) は途切れをまたいで引かず、途切れで分かれた区間ごとの水準からの増え方で求める。電源を入れ直した翌日に水準だけが違うプロセスはリークにならず、毎日起動し直しても日中に増え続けるプロセスはリークとして出る。

いまより 5 分以上先の時刻を持つ記録は診断に使わず、観測品質の備考にその時刻を示す。記録中に時計が戻されると (RTC を地方時で持つ PC を起動後に NTP が合わせたなど)、戻される前に書いた記録は先の時刻を持つ。記録側が時計の巻き戻しを検出したとき (`ops_events` の `clock_skew`) も、その時刻を備考に示す。`service print --system` の system ユニットは、時刻同期 (`time-sync.target`) の後に起動する。時刻同期のサービスが無いホストでは、この順序は何もしない。既定の user ユニットには時刻同期を待つ方法が無い (`time-sync.target` は system のサービスマネージャーにしか無い) ので、上の扱いだけになる。

HTML レポートのチャートとスパイクイベント一覧は、診断した期間だけを描く。期間が日をまたぐときは軸の時刻に日付 (月/日) が付く。スパイクイベント一覧は新しい 100 件までで、それより多いときは全件数を添える。1 分や 2 分の記録では 1 分集計の点が足りないので、生データから描く。

`--since` / `--until` で期間を選ぶと、1 分集計しか残っていない時間帯も含めて診断する。期間は `--until` の時刻を含まない。時刻はオフセットなしならこのホストのローカルタイム。期間に記録が 1 件も無いときは、記録のある範囲を表示して終了コード `3` で終わる。

```
procdiag check --since "2026-10-01 00:00"
procdiag check --since "2026-10-01 00:00" --until "2026-10-08 00:00" --format json
```

生データを読むルール (CPU-SATURATION-01 など) は、期間のうち生データのある時間帯だけで評価する。そうなったときは、備考にそのルールの一覧が出る。

期間が長いほど、診断に時間とメモリを使う。合成データでの計測例では、200 プロセスを 7 日分診断すると約 6 秒・0.9 GiB、30 日分では約 31 秒・3.7 GiB だった (生データの期間だけなら約 1.7 秒・0.3 GiB)。現場の PC で長い期間を診断するときは、空きメモリを確かめてから実行する。

### compare — 対策の効果を確かめる

「記録 → 診断 → 対策 → 効果の確認」の最後の段。2 つの期間をそれぞれ `check` と同じエンジン・同じ設定で診断し、(ルール, 対象) ごとに前後を突き合わせる。

```
procdiag compare --at "2026-10-28 09:00"                        # 1 つの DB を対策の時刻で分ける
procdiag compare --before 2026-10-20..2026-10-27 --after 2026-10-28..
procdiag compare before.db after.db                              # 2 つの DB (既定はそれぞれの全期間)
--only / --skip / --config / --format text|json / --output / --verbose / --no-color / --exit-zero-on-info
```

- **期間**: `START..END` は半開区間 (END を含まない)。オフセットの無い時刻はこのホストのローカルタイム。`--at` の修正前の期間は修正後と同じ長さで、全ルールの最低観測時間 (既定 24 時間) より短くはしない。期間は記録のある範囲 (生データと 1 分ロールアップの和) に切り詰める。期間の長さは `check` と同じく記録のあった時間で比べ、記録の途切れた間は数えない。期間の行には記録のあった時間と、途切れた時刻を示す (JSON の `span_hours` / `raw_hours` も記録のあった時間)。1 つの DB で修正前と修正後の期間が重なる指定はエラー (exit 4)。
- **判定**: 修正前に検出 → 修正後に検出なしなら「解消」、新たに出れば「新規」、両方で検出なら重大度、同じ重大度なら値の 20% 以上の変化で「改善」「悪化」「変化なし」を決める。確信度は使わない (journal の照合や起動時刻などホスト依存の読み取りに左右されるため)。
- **検出が無いこと**: 値を持たないルール (リーク系以外) では、検出が無い期間がもう一方の期間と同じ長さ以上のときだけ、検出が無いことを「問題が無い」と扱う。1 日に数回起きる問題は、短い期間ではたまたま起きないことがあるため。修正後が修正前より短ければ、修正前の検出は「解消」ではなく「判定不能」(`too_short`、あと何時間必要かを示す)。修正前が修正後より短ければ、修正後の検出は「新規」ではなく「判定不能」。
- **測定ルール**: MEM-LEAK-01 / FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01 は「全対象の値を測る (observe)」と「しきい値で判定する (judge)」に分かれており、検出しなかった期間にも値が残る。修正後の値がしきい値の半分以下で「解消」、それより上で修正前から 20% 以上下がっていれば「改善 (しきい値未満)」(値がしきい値以上なら「改善 (しきい値以上)」)、そうでなければ値どうしを比べる (「変化なし」「悪化」)。値がしきい値以上でも R² が低いと judge は検出しないが、compare はこれを「問題が残っている」と扱う。対象プロセスは (名前, コマンドラインのハッシュ) で突き合わせ、残りが名前ごとに 1 対 1 のときだけ名前で突き合わせる。名前だけで突き合わせた行は、両方の値を示したうえで「判定不能」(`paired_by_name`) とする。コマンドラインが違えば別のプログラムかもしれないため。
- **判定不能**: 片側で記録時間が足りない (ルールの最低観測時間、または上の期間の長さ)・ルールの読む表の行が期間内に無い (生データの保持期間外など)・DB の版が古い・ルールの実行エラー・対象のプロセスが測れない・名前だけで突き合わせた場合。理由は JSON の `why` (例: `too_short`, `not_recorded`, `schema_missing`, `subject_not_observed`, `paired_by_name`) と日本語の説明で示す。
- **注意書き**: 記録ホスト・DB の版・記録時の設定の違い、期間の一部にしか生データが無いこと。
- HTML 出力は未対応 (`text` / `json` のみ)。

### 終了コード (compare)

| コード | 意味 |
|---|---|
| `0` | 修正後に critical / warning が残っておらず、修正前の critical / warning はすべて「解消」か「改善 (しきい値未満)」と判定できた |
| `1` | 修正後に critical / warning が残っている (修正後も検出された、または値が「変化なし」「悪化」「改善 (しきい値以上)」)、または新たに出た |
| `2` | 修正後に info のみ残る (`--exit-zero-on-info` で 0 に畳める) |
| `3` | どちらかの期間に記録が無い、または修正前の critical / warning が修正後に判定不能 (理由は行ごとに出る。記録時間不足なら、記録を続ければ判定できる) |
| `4` | 前提条件エラー (期間指定の誤り・1 つの DB で期間が重なる・DB が無い・DB の版が新しすぎる・DB が壊れている・出力先に書けない・コマンドラインの誤り・設定ファイルが無い・読み込めない) |

優先順位は 1 > 3 > 2 > 0。CI では `3` を「まだ判定できない (記録を続ける)」として扱える。修正前が info だけの検出は判定不能でも `3` にしない。

---

## セキュリティ運用上の注意（設定ファイル）

`config.py` は読み込み時に **`exec()` で実行される**（netdiag 由来の設計で、設定に Python 式を書けるのが利点）。
これは、`config.py` への書き込み権限が、`procdiag` を実行するユーザー（特に systemd 常駐サービス）の
権限での **任意コード実行** と等価であることを意味する。次を守ること:

- `config.py` は **信頼できる場所** に置き、**root 所有・非 world-writable** にする（推奨 `chmod 0600`、複数ユーザーで読む必要がある場合のみ `0644`）。
- 誰でも書き込める場所（`/tmp` や 0777 の共有ディレクトリ等）には置かない。
- `--config` を省いたときに読むカレントフォルダの `config.py` は、他のユーザーが書き換えられるとき（自分と root 以外の所有、
  world-writable、誰でも書き込めるフォルダにあって sticky ビットが無い、または sticky でも所有者が自分と root 以外）に限り、
  実行せず終了コード 4 で終わる。他人が置いた `/tmp/config.py` を `/tmp` での実行で動かさないためである。
  自分の責任で使うファイルは `--config` で明示する（明示したファイルは実行する）。
- 配置後に `procdiag doctor` を実行すると、`設定ファイルの安全性` チェックが、上と同じ基準（所有者・world-writable・
  置き場のフォルダ）で他のユーザーが書き換えられる `config.py` を `degraded` として警告する。

---

## アーキテクチャマップ

```
host-check-tool/
├── procdiag/
│   ├── cli.py            # Click CLI (record / check / status / ...)
│   ├── config.py         # Pydantic 設定モデル (ProcdiagConfig)
│   ├── recorder.py       # 常駐ループ (Recorder) — サンプラーを束ねる
│   ├── scheduler.py      # TierScheduler — 多間隔ジョブの軽量実行
│   ├── storage.py        # SQLite 時系列ストア (WAL/バッファ/ロールアップ)。make_row で名前付き行を生成
│   ├── spikes.py         # spike_events の JSON ペイロード契約 (記録側と読取側で共有)
│   ├── plugins.py        # entry_points ベースのプラグイン読み込み
│   ├── doctor.py         # 記録前の観測能力チェック (procdiag doctor)
│   ├── snapshot.py       # 記録なしの即時ヘルスチェック (procdiag snapshot)
│   ├── bundle.py         # 持ち出し用バンドル (procdiag bundle)
│   ├── status.py         # 記録 DB の概況 (status / bundle で共用)
│   ├── systemd.py        # systemctl / timedatectl / journalctl の読み取り専用ラッパー
│   ├── kernlog.py        # カーネルログ (journal のカーネルの行) の異常行の分類
│   ├── export.py         # CSV / JSON エクスポート
│   ├── service.py        # systemd ユニット生成
│   ├── report.py         # terminal / JSON / HTML+SVG レポート生成
│   ├── samplers/
│   │   ├── __init__.py   # eager import → @register_sampler 登録
│   │   ├── system.py     # Tier-1: PSI / CPU / Mem / IO / Load
│   │   ├── cgroup.py     # Tier-2a: cgroup v2 サービス統計
│   │   ├── process.py    # Tier-2b: 全プロセス走査 + PID キャッシュ
│   │   ├── disk.py       # Tier-3: ディスク使用量 / ディレクトリサイズ
│   │   ├── thermal.py    # Tier-3: CPU 温度 / クロック (センサ有りのみ)
│   │   ├── service.py    # Tier-3: systemd の自動再起動・failed (systemd 有りのみ)
│   │   ├── kernel_log.py # Tier-3: カーネルログの異常 (journal 読取可のみ)
│   │   ├── trigger.py    # SpikeTrigger — PSI スパイク検出・デバウンス
│   │   └── selfmeter.py  # SelfMeter — 自己オーバーヘッド計測・自動減速
│   ├── core/
│   │   ├── sampler.py    # Sampler 基底クラス・レジストリ・RunContext
│   │   ├── rule.py       # Rule 基底クラス (configure / evaluate)・レジストリ
│   │   ├── result.py     # Finding / Diagnosis / ObservationQuality 型
│   │   └── util.py       # parse_duration / CounterDelta / バイト単位 / ...
│   └── diagnosis/
│       ├── engine.py     # Engine — ルールを順に実行し Diagnosis を組み立てる
│       ├── trend.py      # 線形回帰・外挿・周期検出・系列ヘルパ (純関数群)
│       ├── cpu.py        # CPU-PERIODIC-01 / CPU-SINGLECORE-01 / CPU-SATURATION-01
│       ├── memory.py     # MEM-LEAK-01 / MEM-PRESSURE-01 / SWAP-THRASH-01 (+ PSS 事前選定)
│       ├── io_disk.py    # DISK-FULL-01 / IO-BOTTLENECK-01 / DISK-GROWTH-01 / DISK-JOURNAL-01 / DISK-INODE-01 / FS-READONLY-01
│       ├── leak.py       # FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01
│       ├── crash.py      # OOM-KILL-01 / UNCLEAN-SHUTDOWN-01 / SERVICE-RESTART-01 / KERNEL-ERROR-01
│       └── thermal.py    # THERMAL-01
└── tests/
    ├── test_e2e.py       # E2E: init→record→check の通し検証
    ├── test_recorder.py  # Recorder / TierScheduler の単体テスト
    ├── test_sampler_*.py # 各サンプラーの単体テスト
    └── fixtures/         # /proc・/sys/fs/cgroup のモックツリー
```

---

## 3 層 + トリガの説明

プロセス名・コマンドライン・マウント先・cgroup のユニット名に UTF-8 でないバイトがあっても
(Shift_JIS の引数、カーネルが 15 バイトで切った日本語のプロセス名など)、そのまま記録する。
そのバイトは `\x91` のような 16 進の表記になる (例: `1号機温度\xe7\x9b`)。

### Tier-1 — システム全体 (1 秒)

`SystemSampler` が毎秒取得する:

| 列 | 内容 |
|---|---|
| `cpu_total`, `cpu_max_core` | 全コア平均 / 最大コア使用率 |
| `mem_used`, `mem_available` | 使用メモリ / 利用可能メモリ |
| `swap_used`, `swap_in`, `swap_out` | スワップ使用量・増分 |
| `load1` | `/proc/loadavg` から 1 分ロードアベレージ |
| `psi_cpu_some10` 等 | `/proc/pressure/*` の PSI 値 (カーネル 4.20+ 必須) |
| `disk_read_bytes`, `disk_write_bytes` | 全ディスク I/O 増分 |
| `self_cpu`, `self_rss` | procdiag 自身のオーバーヘッド |

### Tier-2a — cgroup v2 サービス統計 (5 秒)

`CgroupSampler` が `/sys/fs/cgroup/system.slice/*/` 下の `.service` / `.scope` 毎に:

- `cpu.stat` から CPU 使用量 (マイクロ秒、増分)
- `memory.current`, `memory.swap.current` からメモリ使用量
- `io.stat` から I/O バイト数 (増分)
- `pids.current` から PID 数

cgroup v2 が存在しない環境では自動的にスキップされ、demotion note が記録される。

### Tier-2b — プロセス走査 (10 秒)

`ProcessSampler` が psutil で全プロセスを走査:

- PID キャッシュを保持して `cpu_percent()` の差分を正確に計測
- `io_counters()` へのアクセス拒否は欠損として記録 (非 root 環境では一般的)
- `proc_registry` テーブルにプロセス台帳を upsert

### Tier-3 — ディスク (60 秒 / 1 時間)

`DiskSampler` が:

- `collect()`: 60 秒毎にパーティション使用量・inode 使用量・読み取り専用フラグを記録 (`disk_samples`)
- `collect_dirs()`: 1 時間毎に `watch_dirs` を走査 (`dir_samples`)。
  時間予算内に完了しない場合は `is_estimate=1` で打ち切り結果を記録。

### Tier-3 — CPU 温度・クロック (10 秒)

`ThermalSampler` が CPU 温度と現在 / 最大クロックを `thermal_samples` に記録する:

- 温度 (`temp_max_c`) は CPU 自身のセンサー (coretemp・k10temp・zenpower・cpu_thermal など) の
  最高値。NVMe・GPU・Wi-Fi など CPU 以外のセンサーは、どれほど熱くても使わない。
- CPU のセンサーが無いときは、PC では ACPI の温度ゾーン (acpitz)、ARM ボードでは SoC の
  温度ゾーン (soc_thermal) を使う。どれも無ければ温度は空になり、THERMAL-01 は高温を判定できない。
  `snapshot` の「CPU 温度」も同じ値を表示する。
- 0.6.0 までの版で記録した DB の `temp_max_c` は、CPU 以外も含むすべてのセンサーの最高値。

### Tier-3 — サービス / カーネルログ (5 分)

- `ServiceSampler`: systemd が数える自動再起動回数 (`NRestarts`) の増加と failed 遷移だけを
  `service_events` に記録する (cgroup の作り直しは timer 起動の oneshot でも起きるため使わない)。
  確認の間に `NRestarts` が下がったとき (手動の再起動や `systemctl reset-failed` で 0 に戻る) は、
  下がった後の値をその間の再起動回数として数える。0 に戻った後に前回の値以上まで増えると、
  増えた分しか数えられない。
  `Restart=always`、`RestartForceExitStatus=` のあるユニットは正常終了の後も再起動されるため、
  systemd がジャーナルに残す「Failed with result」の行 (systemd 240 より前は MESSAGE_ID の無い
  同じ文の行) で見分け、異常終了の後の再起動を `kind=restart`、それ以外を `kind=restart_clean`
  として記録する (`Restart=on-success` は常に `restart_clean`)。行は再起動より `RestartSec=` だけ
  前に出るので、前回の確認より前の行もさかのぼって照らし合わせる。ジャーナルを読めない
  (root または systemd-journal / adm グループが必要) とき、`LogLevelMax=` が warning より低く
  この行が出ないときは `kind=restart_maybe_clean` として記録する。最初の確認で既に failed の
  ユニットは遷移として数えず、`kind=failed_at_start` として記録する。
- `KernelLogSampler`: journal のカーネルの行を前回の続き (cursor) から読み、I/O エラー・FS エラー・
  ハングタスク・MCE・segfault・USB 切断・温度制限の行だけを `kernel_events` に記録する。
  cursor は記録した行と同じフラッシュで `meta` の `kernel_log_cursor` に保存し、同じ DB への
  次の `record` はその続きから読む。記録を止めていた間の行と、再起動・電源断の前のブートの
  最後の行も、journal に残っていれば記録される。
  読み取りには root または systemd-journal / adm グループが必要。
- どちらも停止するときにもう一度読む。前回の読み取りから停止までの最大 5 分の出来事も残る。

### 記録セッション

`record` の各実行は `sessions` に開始時刻・boot 時刻を記録し、60 秒毎に生存時刻を更新、
正常停止 (SIGTERM / SIGINT / SIGHUP / `--duration` 満了) で停止時刻を記録する。
停止記録のないまま別の boot の記録が始まっていれば、電源断などの異常停止
(UNCLEAN-SHUTDOWN-01) と判定できる。

### SpikeTrigger — PSI スパイク検出

Tier-1 の各ティックで `SystemSnapshot` を評価:

- PSI 利用可能: `cpu/mem/io` の `some/full avg10` が閾値を超えたら即発火
- PSI 非対応カーネル: CPU 使用率・メモリ比率・swap_out 増分でフォールバック
- デバウンス: 同種トリガは `spike_cooldown_sec` 秒以内に再発火しない
- ストーム集約: 5 分以内に `spike_storm_threshold` 回を超えたら "storm" 1 件に集約

発火時は Tier-2a/2b を即時起動し `spike_events` テーブルに記録する。

### SelfMeter — 自己計測と自動減速

- 60 秒スライディングウィンドウで procdiag 自身の CPU 使用率を移動平均
- 平均が `overhead_budget_pct` (既定 1%) を超えると Tier-2b 間隔を `throttle_factor` 倍に拡大
- 10 分間連続して予算内に収まると間隔を徐々に復元

---

## 観測品質の思想

procdiag が「観測できなかった事実」を隠さないよう、以下を設計原則とする:

1. **部分降格**: 1 プロセスの `io_counters` 取得に失敗しても他のプロセスの記録は継続する
2. **欠損率の明示**: `io_denied / io_total` の比率をレポートの ObservationQuality 節に表示する
3. **自己透明性**: `self_cpu`・`self_rss` を通常の計測列に含め、診断者が観測コストを把握できる
4. **min_observation_hours**: 十分な観測時間がない状態でルールを発火させない
5. **demotion notes**: PSI 非対応・cgroup v1 環境での降格理由を日本語で記録する
6. **入力データの明示**: 各ルールは必要な表・列 (`requires_schema`) と期間内に行が必要な表 (`requires_rows`) を宣言し、満たさないルールはエンジンが実行せず「記録なしで未評価」(JSON `uncovered_rules`) として示す。古い版の DB や生データが削除された期間で「問題なし」と読まれるのを防ぐ。センサや監視ディレクトリが無く一度も記録されない表 (`thermal_samples` / `dir_samples`) は対象外

---

## ルールカタログ (組み込みルール)

| Rule ID | カテゴリ | 概要 |
|---|---|---|
| `MEM-LEAK-01` | memory | proc_rollup_1m の rss_avg に線形回帰をかけ、メモリリークの疑いを検出。枯渇までの日数は、記録の最後の MemAvailable をそのプロセスの増加率で使い切るまでの日数 (他のプロセスの増減は考えない) |
| `MEM-PRESSURE-01` | memory | psi_mem_max が閾値を超える状態が 10 分以上途切れず続き、その前後 1 分を含めて swap_out が 1 MiB 以上ある日 (圧迫日) が、観測日の 6 割以上かつ 2 日以上で反復する慢性メモリ不足を検出。日はこのホストのローカル日付で、psi の記録が 6 時間以上ある日だけを観測日として数える。慢性的な圧迫は続くものなので、一瞬の圧迫や、日中に散らばった短い圧迫、圧迫と無関係なスワップでは検出しない。観測日が 2 日に足りない記録は「観測不足で未評価」として、あと何時間記録すればよいかを表示する (問題なしとは読まれない) |
| `SWAP-THRASH-01` | memory | swap_in/out の同時発生 + psi_mem_full>0 のスラッシングを検出 (瞬時型)。5 分以上空いた発生は別の回として数え、スワップ量はバイトで表示する |
| `CPU-SATURATION-01` | cpu | psi_cpu_some10>20 の継続を検出し、spike_events から犯人プロセスを列挙 |
| `CPU-SINGLECORE-01` | cpu | cpu_max_core_max ≥ 95% かつ cpu_avg < 50% のボトルネックコアを検出 |
| `CPU-PERIODIC-01` | cpu | spike_events を負荷の始まりごとにまとめ、日次・時次の定時処理をホストのローカル時刻で検出 (記録した日・時間の半分以上で同じ時刻 ±2 分に始まり、偶然の一致の確率が 1% 未満のときだけ。偶然の確率は、その時刻の前の 1 時間・後の 1 時間・全体のうち負荷の始まりがいちばん多い頻度で見積もるので、勤務時間や昼休みに偏る負荷も周期にしない。1 回の長い負荷も周期にしない) |
| `IO-BOTTLENECK-01` | io_disk | psi_io_some10>30 の継続 + I/O 上位プロセス特定 (io 欠損時は cgroup で代替) |
| `DISK-FULL-01` | io_disk | disk_samples を線形外挿し、容量到達までの残り日数を算出 |
| `DISK-GROWTH-01` | io_disk | dir_samples の増分ランキングで増加源を特定 (DISK-FULL に折りたたみ) |
| `DISK-JOURNAL-01` | io_disk | journald のサイズ超過を検出し SystemMaxUse 設定を提案 |
| `OOM-KILL-01` | crash | プロセス台帳の途絶×直前のメモリ危機の相関で OOM kill を検出。journal 読取可なら確証度を昇格 |
| `FD-LEAK-01` | leak | fds_max の線形回帰で fd リークと ulimit 到達予測日数を算出 |
| `THREAD-LEAK-01` | leak | threads_max の単調増加を検出。上限 (`/proc/sys/kernel/threads-max`) は記録に含まれないため、記録したホストでチェックしたときだけ読んで到達日数を出す |
| `ZOMBIE-01` | leak | プロセス走査ごとのゾンビ数の増加傾向を検出。傾き 0.5/h 以上・R² 0.7 以上で、検出時点でゾンビが残っているときだけ (一瞬のゾンビや回収済みは対象外)。v4 より前の DB は cpu_pct=0・rss=0・スレッド数 1 以下の行 (カーネルスレッドも含む) で近似し confidence=low |
| `DISK-INODE-01` | io_disk | inode 使用率の高止まり・増加を検出 (容量に空きがあっても書けない状態) |
| `FS-READONLY-01` | io_disk | 記録中にファイルシステムが読み取り専用へ切り替わったことを検出 |
| `UNCLEAN-SHUTDOWN-01` | crash | 記録セッション履歴から、停止処理なしの再起動 (電源断・パニック) を検出 |
| `SERVICE-RESTART-01` | crash | systemd の自動再起動 (NRestarts) の増加・failed 遷移からクラッシュループを検出 (正常終了の後と分かっている再起動は再起動ループとせず、確信度 low の warning。ジャーナルを読めず見分けられなかった再起動は数えるが、確信度 medium) |
| `KERNEL-ERROR-01` | crash | カーネルログの I/O エラー・FS エラー・ハングタスク・MCE・segfault・USB 切断を集計 |
| `THERMAL-01` | cpu | 高温下での継続的なクロック低下 (サーマルスロットリング) を検出。同じ時刻の CPU 温度とクロック比を突き合わせ、80°C 以上かつ最大の 85% 未満の記録が 3 回以上続いた期間を数える |

最低観測時間はルールごとに異なる (瞬時型 0h 〜 日次反復型 24h)。日次反復型 (MEM-PRESSURE-01) は観測が 6 時間以上ある日が 2 日以上必要で、足りない記録は日付の区切り方によって「あと N 時間」の未評価になる (24 時間ちょうどの記録では起こりうる)。48 時間記録すれば確実に評価できる。線形回帰系ルールの
既定 6 時間は `config.rules.min_observation_hours` で一括調整できる (0 で無効)。
観測時間 (最初と最後の記録の間隔から、記録が途切れた間を除いたもの) は記録した
時間より最大 2 分近く短くなる (1 分集計は分の始まりの時刻を持ち、停止したときの途中の
1 分は集計されない) ため、最低観測時間に 2 分以内で届かない場合も足りているとみなす。
観測時間不足で評価できなかったルールは、診断結果に「評価範囲」として必ず表示される。

各ルールのしきい値 (クラス属性の数値) は `config.rules.overrides[<RULE-ID>]` で上書きでき、
一覧と既定値は `procdiag explain <RULE-ID>` で確認できる。

record 常駐中は毎時の軽量プレチェックがリーク疑いプロセス (最大5件) を選び、
smaps_rollup から PSS を自動精査する — MEM-LEAK-01 は PSS でも増加が確認できた場合に
確証度を high へ昇格し、PSS が横ばいなら「共有メモリの可能性」として low へ下げる。
また記録 DB 自身の容量 (`retention.db_max_size_mb`、既定 2GB) と置き場の空き
(`retention.min_free_disk_mb`、既定 512MB) を毎時監視する。超過したときは、まず削除済みの領域と
WAL を VACUUM で返し、それで収まらないときだけ保持期間を段階的に短縮する (生データ 24 時間 →
12 時間 → 6 時間、続けて 1 分集計 30 日 → 14 日)。各段階のあとで VACUUM 後の見込みサイズを測り、
収まった段階で止める。生データの段階は 1 分集計の保持期間 (`retention.rollup`) を変えない。
実行した段階は ops_events の `db_guard`、結果は `db_guard_reclaim` に記録する。
VACUUM には、後のサイズの約 2 倍の空きが要る。空きが少ないことが理由のときは、VACUUM が実行できる
段階まで縮めて初めて容量が返るので、そこまで縮める。どの段階でも実行�