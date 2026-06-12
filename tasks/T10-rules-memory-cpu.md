# T10: ルール追加 — MEM-PRESSURE-01 / SWAP-THRASH-01 / CPU-SATURATION-01

フェーズ: P4(並列)/ 依存: T09
参照: `tasks/README.md` → 設計書 §6.2(該当3行), §6.3 / 既存実装:
`procdiag/diagnosis/memory.py`, `cpu.py`, `engine.py`, `trend.py`, `tests/synth.py`
(T06 が確立したパターンに**完全に倣う**こと)

## 所有ファイル(これ以外に触れない)

```
procdiag/diagnosis/memory.py       # 追記のみ (既存 MEM-LEAK-01 を変更しない)
procdiag/diagnosis/cpu.py          # 追記のみ (既存2ルールを変更しない)
procdiag/diagnosis/templates/MEM-PRESSURE-01.j2
procdiag/diagnosis/templates/SWAP-THRASH-01.j2
procdiag/diagnosis/templates/CPU-SATURATION-01.j2
tests/test_rules_memory_cpu.py
```

`tests/synth.py` は**読み取り+ヘルパ追記のみ可**(既存シグネチャ変更不可)。
`diagnosis/__init__.py`・`engine.py` は触らない。

## 実装するルール(設計書 §6.2 の判定ロジック)

- **MEM-PRESSURE-01**(memory.py): sys_rollup_1m の psi_mem_max が閾値(5.0)超の
  時間帯が**日次で反復**(trend.detect_recurrence / detect_periodicity を活用)し、
  かつ swap_out_sum > 0 → WARNING。evidence: 該当時間帯・反復日数・swap量
- **SWAP-THRASH-01**(memory.py): sys_samples(または rollup)で swap_in と swap_out が
  同一時間帯に高頻度 + psi_mem_full10 > 0 → CRITICAL。evidence: 該当期間・in/out量
- **CPU-SATURATION-01**(cpu.py): psi_cpu_some10 > 20 が継続(15分以上)→
  該当時間帯の spike_events から犯人プロセス(top_processes JSON)を集計して列挙 →
  WARNING。evidence: 継続時間・上位プロセス

PSI欠損DB(psi列が全None)では3ルールとも沈黙(誤検知より無検出)。
what/why/manual_commands(`free -h`, `vmstat 1`, `sar -B` 等)とテンプレート5部構成は
既存ルールの書式に合わせる。

## テスト要求
synth.py(必要ならヘルパ追記)で各ルール検出側・非検出側(§6.3: 観測不足・
閾値未満・PSI欠損)の両側。engine 経由で proposal が埋まることも1ケース。

## 完了条件
`.venv/bin/python -m pytest tests/test_rules_memory_cpu.py tests/test_engine.py -q` 全緑
(既存テストを壊さない)+ black + 最終報告。
