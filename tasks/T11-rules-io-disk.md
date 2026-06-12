# T11: ルール追加 — IO-BOTTLENECK-01 / DISK-GROWTH-01 / DISK-JOURNAL-01

フェーズ: P4(並列)/ 依存: T09
参照: `tasks/README.md` → 設計書 §6.2(該当3行), §6.3 / 既存実装:
`procdiag/diagnosis/io_disk.py`, `engine.py`(fold関数), `tests/synth.py`
(T06 のパターンに完全に倣う)

## 所有ファイル(これ以外に触れない)

```
procdiag/diagnosis/io_disk.py      # 追記のみ (既存 DISK-FULL-01 を変更しない)
procdiag/diagnosis/templates/IO-BOTTLENECK-01.j2
procdiag/diagnosis/templates/DISK-GROWTH-01.j2
procdiag/diagnosis/templates/DISK-JOURNAL-01.j2
tests/test_rules_io_disk.py
```

`tests/synth.py` は読み取り+ヘルパ追記のみ可。`engine.py` は触らない
(fold の対応表定数に GROWTH→FULL が既にあるか確認し、**無ければ最終報告で指摘**。
engine を編集しないこと)。

## 実装するルール(設計書 §6.2)

- **IO-BOTTLENECK-01**: psi_io_some10 > 30 が継続(15分以上)→ 該当時間帯の
  proc_samples io_write/read_delta 上位プロセスを特定 → WARNING。
  io列が全None(非root欠損)なら confidence を low に落として cgroup の
  io_*_delta 上位で代替、それも無ければ沈黙
- **DISK-GROWTH-01**: dir_samples の path ごと増分ランキング(期間内 last-first)。
  日次増分が閾値(既定 100MB/day、定数)超 → INFO/WARNING。
  evidence: path・daily_growth・観測日数。DISK-FULL-01 と同一パーティション なら
  subject にマウントポイントを含め fold 対象になる形にする
- **DISK-JOURNAL-01**: dir_samples のうち path が `/var/log/journal`(前方一致)の
  最新サイズ > config.rules.journal_size_warn_gb(既定4GB)→ WARNING。
  proposal に SystemMaxUse 設定手順(設計書 §6.2)

## テスト要求
synth.py で各ルールの検出・非検出両側。IO-BOTTLENECK の io欠損→cgroup代替→沈黙の
3段降格。DISK-GROWTH + DISK-FULL 共存時に engine 経由で fold される(related に入る)
ことを1ケース(fold表に未登録で失敗する場合はテストを skip にして最終報告で指摘)。

## 完了条件
`.venv/bin/python -m pytest tests/test_rules_io_disk.py tests/test_engine.py -q` 全緑
+ black + 最終報告。
