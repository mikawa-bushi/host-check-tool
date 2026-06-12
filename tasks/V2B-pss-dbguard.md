# V2-B: PSS 精査の自動配線 (EXT-02) + DB 自己防衛 (EXT-11)

フェーズ: v0.2(3並列)/ 依存なし(v0.1.1 のコードベースが前提)
参照: `tasks/README.md`(共通規約)→ `procdiag_extension_proposal.md` EXT-02, EXT-11 →
既存実装: `procdiag/recorder.py`(ジョブ登録の流儀)、`procdiag/storage.py`、
`procdiag/samplers/process.py`(pss_targets 機構)、`procdiag/diagnosis/memory.py`
(MEM-LEAK-01)、`procdiag/diagnosis/trend.py`(fit_linear)、設計書 §4.4, §5.3, §6.2

## 所有ファイル(これ以外に触れない)

```
procdiag/recorder.py                             # ジョブ2つ追加
procdiag/storage.py                              # enforce_size_budget 追加
procdiag/config.py                               # 新フィールド + CONFIG_TEMPLATE 更新
procdiag/diagnosis/memory.py                     # MEM-LEAK-01 の PSS 昇格のみ追記
procdiag/diagnosis/templates/MEM-LEAK-01.j2      # PSS 根拠の条件付き行を追加
tests/test_pss_wiring.py                         # 新規
tests/test_db_guard.py                           # 新規
```

並行タスクが diagnosis/crash.py・__init__.py・cli.py・tests/synth.py を編集中。
**tests/synth.py は読むだけ(追記も不可)**。テストデータは自テストファイル内で
`storage.buffer()` 直書きで構築すること。

## Part 1: PSS 精査の自動配線 (EXT-02)

設計 §4.4 の未配線部分。`ProcessSampler.pss_targets`(実装済み・最大5)に
書き込む者を作る。

### config.py
- `SamplingConfig.pss_precheck_interval: float = 3600.0`(0 で無効)。
  正数バリデーションは既存フィールドの流儀で。CONFIG_TEMPLATE にもコメント付きで追加

### recorder.py — ジョブ "pss_precheck"
- `pss_precheck_interval` 間隔で登録(値が 0 以下なら登録しない)
- 処理: `from .diagnosis.trend import fit_linear` を**関数内 import** し、
  proc_rollup_1m の直近 6 時間から proc_id ごとの rss_avg 系列を取得
  (`storage.rows`/`series` を使用)→ fit_linear(min_points=60, min_span_hours=2.0 程度の
  プレチェック緩条件。定数化)→ slope > 1MB/h かつ r2 > 0.8 の候補を slope 降順で
  最大 5 件 → `sampler.pss_targets` を**置き換え**(add でなく set 全交換。
  リーク解消したプロセスを追い続けない)
- process サンプラーに `pss_targets` 属性が無ければ何もしない
  (test_recorder のフェイクを壊さないこと — `hasattr` ガード)
- 対象集合が変化したら `ops_events("pss_scrutiny", "proc_id=… name=…")` を記録

### diagnosis/memory.py — MEM-LEAK-01 の確証度昇格
rss の回帰が成立した Finding について、proc_samples.pss(非NULL)の系列を取得:
- 12 点以上あり、pss の fit_linear が slope > 0.5MB/h かつ r2 > 0.8
  → `confidence = HIGH`、evidence["pss_slope_mb_per_h"] を追加
- 12 点以上あり、pss の slope <= 0.1MB/h
  → `confidence = LOW`、evidence["pss_note"] = "PSSでは増加が確認できず (共有メモリ/キャッシュの可能性)"
- 点数不足 → 従来どおり(変更なし)
テンプレートの [根拠] に `{% if evidence.pss_slope_mb_per_h %}…{% endif %}` の
条件行を追加(既存の出力を変えない)。

## Part 2: DB 自己防衛 (EXT-11)

M8 実測(1,000 プロセスで 48h ≈ 1.4GB)を踏まえた容量ガード。

### config.py
- `RetentionConfig.db_max_size_mb: float | None = 2048.0`(None で無効)
- `RetentionConfig.min_free_disk_mb: float = 512.0`
- CONFIG_TEMPLATE に追加

### storage.py
```python
def enforce_size_budget(self, *, max_bytes: int | None,
                        min_free_bytes: int | None) -> dict:
```
- 現サイズ = DB 本体 + `-wal` の合計。空き = `shutil.disk_usage(DB親dir).free`
- 予算内なら何もしない `{"triggered": False}`
- 超過時は**決定論的なはしご**で段階削減(各段ごとに再判定し、足りたら停止):
  ① 生サンプル保持を 24h 相当へ → ② 12h → ③ 6h(下限)
  → ④ ロールアップ 30d → ⑤ 14d。実装は既存 `retention()` を期間引数で呼ぶ
- 各段で `ops_events("db_guard", "段の内容と削減後サイズ")` を記録
- 全段実施後に 1 回だけ VACUUM(発動はまれなので許容)
- 戻り値: `{"triggered": True, "steps": [...], "size_before": n, "size_after": m}`

### recorder.py
- 既存の 1 時間ごと retention 処理の直後に、config 値(MB→bytes 換算)で
  `enforce_size_budget` を呼ぶ。db_max_size_mb が None なら min_free のみ判定

## テスト要求

- test_pss_wiring.py: 合成 rollup(リークあり/なし)で候補抽出が上位5件・しきい値どおり/
  pss_targets が全交換される/属性なしフェイクで no-op/ops_events 記録。
  MEM-LEAK 昇格: pss 系列あり→HIGH、pss 横ばい→LOW+note、pss なし→従来どおり
- test_db_guard.py: 小さな max_bytes を与えて発動→はしごが順に効き ops_events に
  段が残る/予算内なら triggered=False/min_free のモック(monkeypatch shutil.disk_usage)
- 回帰確認: `tests/test_recorder.py tests/test_config.py tests/test_engine.py
  tests/test_rules_memory_cpu.py` がすべて緑のまま

## 完了条件
`.venv/bin/python -m pytest tests/test_pss_wiring.py tests/test_db_guard.py
tests/test_recorder.py tests/test_config.py tests/test_engine.py tests/test_rules_memory_cpu.py -q`
全緑 + black + 共通規約の最終報告。
