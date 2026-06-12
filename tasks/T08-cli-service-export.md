# T08: cli.py + service.py + export.py — click CLI・systemd・エクスポート

フェーズ: P2(並列)/ 依存: T01
参照: `tasks/README.md` → 設計書 §3, §8 / 実装計画 §2.2(コマンド対応表・終了コード)
netdiag 参照: `/tmp/network-check-tool/netdiag/cli.py` — **構成・書き味の手本。
click group / _resolve_version / service group / 遅延import のパターンをそのまま写像**

## 所有ファイル(これ以外に触れない)

```
procdiag/cli.py
procdiag/service.py
procdiag/export.py
tests/test_cli.py
```

## 並行タスクとの結合点 — 重要

T05(recorder)/T06(engine)/T07(report)が並行実装中。それらに依存するコマンド
(record/check/report/status の一部)は **netdiag cli と同じく関数内遅延 import** で書く:
```python
@cli.command()
def record(...):
    from .recorder import Recorder      # ← モジュール先頭でなくここで
```
これで cli.py 自体の import とその他コマンドのテストは依存先なしで通る。
遅延先の契約(シグネチャ)は各タスクのゴールファイルに固定済み:
- `Recorder(config, db_path=…, duration=…).run() -> int`
- `Engine(config, verbose=…).run(store, only=…, skip=…) -> Diagnosis`
- `report.render_terminal(diag, color=…, verbose=…)` / `render_json(diag)` /
  `render_html(diag, charts, hostname_label=…)` / `build_charts(store)`

テストでは record/check/report は実体を呼ばない(--help と引数検証のみ)。
実体結合の検証は T09 が行う。

## 実装要求

### cli.py — コマンド一覧(設計書 §3 + 実装計画 §2.2)
`@click.group()` + `version_option`(netdiag の `_resolve_version` を
`host-check-tool` 配布名で写像)。共通オプションの流儀も netdiag に合わせる。

| コマンド | 内容 |
|---|---|
| `init [DIR]` | DIR(既定 .)に `config.py`(config.CONFIG_TEMPLATE)と `data/` を生成。既存configは上書きせず警告 |
| `record [--duration 24h] [--config PATH] [--db PATH]` | load_config → Recorder。duration は core.util.parse_duration。Ctrl-C 案内を表示 |
| `check [--db] [--config] [--only IDs] [--skip IDs] [--format text\|json\|html] [--output FILE] [--verbose] [--no-color]` | Storage(read_only)→Engine→render_*。**終了コード: 0=Finding無し / 1=critical・warningあり / 2=infoのみ**(netdiag check と同義)。DB無し→「先に procdiag record を実行してください」(netdiag history の文言流儀) |
| `advise` | check の**非表示エイリアス**(`cli.add_command(check, name="advise")` 相当。hidden=True) |
| `report [--db] [--config] [--out report.html] [--json report.json]` | check のラッパ: HTML(build_charts込み)とJSONを両方書き出し、書き先を echo |
| `status [--db] [--format text\|json]` | Storage.counts()+meta+ops_events 直近: 期間・サンプル数・DB容量・自己負荷(sys_samplesのself_cpu avg/max)・直近イベント5件。record 非実行でもDBがあれば動く |
| `explain [NAME]` | 引数なし: 登録済みルール(rule_id順)とサンプラー(tier順)を2セクションで一覧。引数あり: rule_id(大文字)or サンプラー名(小文字)を解決し what/why/manual_commands を表示(netdiag explain の書式を写像) |
| `list-rules` | rule_id / category / title の一覧(カテゴリごと) |
| `list-samplers` | tier ごとに name/title/interval_key(netdiag list-probes 写像) |
| `export [--db] [--format csv\|json] [--rollup 1min\|raw] [--out FILE\|-]` | sys_rollup_1m(または sys_samples)を export.py 経由で出力 |
| `prune [--db] [--raw 48h] [--rollup 90d]` | Storage.retention を手動実行し削除件数を echo(netdiag prune 写像) |
| `service print [--system] [--duration]…` / `service install …` | service.py に委譲 |

explain/list系のルール読込: `from .diagnosis import engine` の eager import はせず、
`from .core.rule import all_rules` + `from . import plugins; plugins.load_plugins()` +
**`importlib.import_module("procdiag.diagnosis")` を try/except で**(P2時点では
diagnosis が未完成でも explain がサンプラー分だけで動くように。ImportError時は
「ルールモジュール未導入」と注記表示)。samplers側も同様に
`importlib.import_module("procdiag.samplers.system")` 等を suppress(ImportError) で
試みる(T09 が samplers/__init__ を書いたら全部載る)。

### service.py
netdiag cli の `_unit_text`/`_exec_start`/`service install` を写像しつつ設計書 §4.8:
- ユニット名 `procdiag-record.service`。ExecStart は `procdiag record --config … --db …`
  (`shutil.which("procdiag")` フォールバック `sys.executable -m procdiag.cli`)
- `[Service]` に **`CPUQuota=2%`**, `Nice=10`, `Restart=on-failure`,
  `KillSignal=SIGTERM`, `TimeoutStopSec=30`(フラッシュ猶予)
- `install` は user unit(~/.config/systemd/user/)に書き、**systemctl は実行せず**
  有効化コマンドを表示(netdiag と同じ思想・同じ表示構成)
- `print --system` で system unit を stdout へ

### export.py
```python
def export_rows(store: Storage, *, table: str = "sys_rollup_1m",
                fmt: str = "csv", out: IO[str]) -> int   # 戻り: 行数
```
csv は stdlib csv、json は1行1オブジェクトの配列。列名ヘッダは
`storage.TABLE_COLUMNS[table]`。rollup="raw" 時は sys_samples。

## テスト要求(test_cli.py — click.testing.CliRunner)

- `--help` が全コマンドを列挙、`--version` が出る
- init: tmp_path で config.py と data/ が生成され、再実行で上書きしない。
  生成された config.py が `load_config` で読めること
- explain/list-samplers/list-rules: テスト内で `@register_sampler`/`@register` した
  フェイクを表示できる(diagnosis 未導入でも落ちない)
- status: T01 の Storage で作った tmp DB に meta+数行入れて要約が出る/DB無しで
  優しいエラー(exit≠0, トレースバックでなく日本語1-2行)
- export: csv/json 両形式の出力検証(ヘッダ・行数・json.loads)
- prune: 期限切れ行が消えた件数表示
- service print: ユニットテキストに CPUQuota=2% と ExecStart が含まれる。
  install は HOME を tmp_path に向けて(monkeypatch)ファイルが書かれ
  systemctl が**実行されない**こと
- record/check: `--help` のみ(実体は呼ばない)

## 完了条件
`.venv/bin/python -m pytest tests/test_cli.py -q` 全緑 + black + 最終報告。
