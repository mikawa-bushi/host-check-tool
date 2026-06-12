# V2-A: OOM-KILL-01 — OOM 検出ルール (EXT-01)

フェーズ: v0.2(3並列)/ 依存なし(v0.1.1 のコードベースが前提)
参照: `tasks/README.md`(共通規約。フェーズ表は v0.1 のもので読み替え)→
`procdiag_extension_proposal.md` EXT-01 → 既存実装の手本:
`procdiag/diagnosis/memory.py`(ルールの書式)、`engine.py`、`tests/synth.py`、
`tests/test_rules_memory_cpu.py`(テストの書式)

## 所有ファイル(これ以外に触れない)

```
procdiag/diagnosis/crash.py                      # 新規
procdiag/diagnosis/__init__.py                   # crash の import を1行追加のみ
procdiag/diagnosis/templates/OOM-KILL-01.j2      # 新規
tests/test_rules_crash.py                        # 新規
tests/synth.py                                   # 末尾追記のみ (既存シグネチャ変更不可)
```

並行タスクが memory.py / recorder.py / cli.py / config.py を編集中。絶対に触れない。

## 実装するルール: OOM-KILL-01 (category="crash")

「なぜ落ちる」への直接回答。**DB のみで完結する決定論検出**+任意の journal 突合。

### 判定ロジック(クラス定数化してテストから調整可能に)

window 内の各 proc_id について:
1. `last_ts` = proc_samples の最終 ts。**記録は続いているのにプロセスが消えた**:
   `window.end_ts - last_ts > 60s`(マシン全体の停止と区別)
2. 短命プロセスの除外: 生存期間 `last_ts - first_ts >= 600s`
3. 有意な消費者だった: 最終 rss >= mem_total の 1%
   (mem_total は同時刻近傍の sys_samples の `mem_used + mem_available` で近似。近似である旨を docstring に明記)
4. **メモリ危機との相関**: sys_samples の `[last_ts-180s, last_ts+60s]` に
   `psi_mem_full10 >= 1.0` または `swap_out > 0` または
   `mem_available < mem_total の 5%` のいずれかが存在
5. 1〜4 すべて成立 → Finding(severity=WARNING, confidence=MEDIUM,
   subject=プロセス名, evidence: last_seen / last_rss / 危機指標の実値 / 生存期間)
6. 同時多発は slope…ではなく last_rss 降順で最大 5 件まで

### journal による確証度昇格(任意・降格前提)

- `journalctl -k -q --no-pager --since <ts> --until <ts>` を timeout=5 で subprocess 実行し
  `oom-kill|Out of memory|Killed process` を検索。プロセス名(または PID)が一致したら
  confidence=HIGH + evidence["journal_line"] に該当行
- journalctl 不在・権限なし・timeout は**静かに MEDIUM のまま**(例外を漏らさない)
- subprocess 呼び出しはモジュール関数 `_read_oom_journal(since, until) -> list[str]` に
  切り出し、テストから monkeypatch 可能にする

### その他

- `min_observation_hours = 0.0`(瞬時型)。what/why/manual_commands
  (`journalctl -k | grep -i oom`, `dmesg | grep -i oom` 等)を既存ルールの流儀で
- テンプレート 5 部構成。恒久対処に「systemd の MemoryMax / OOMScoreAdjust 設定、
  スワップ設計の見直し」を含める
- `diagnosis/__init__.py` には既存の import 行に倣い `from . import crash` を追加するだけ

## tests/synth.py への追記(末尾のみ)

```python
def add_oom_scenario(store, *, name="victim", die_at_hours, total_hours,
                     rss_bytes, crisis=True) -> int   # 戻り: proc_id
    # die_at_hours までの proc_samples + 以降も total_hours まで続く sys_samples。
    # crisis=True なら消滅直前に psi_mem_full10>=1.0 と mem_available 急減を入れる
```

## テスト要求(test_rules_crash.py)

- 検出側: 危機相関ありで WARNING/MEDIUM、journal モック一致で HIGH
- 非検出側: ①危機なしで消えた(正常停止) ②短命プロセス ③記録終了と同時に消えた
  (リブート相当) ④rss が小さい — それぞれ沈黙
- journalctl 失敗(FileNotFoundError / timeout)でも MEDIUM で完走
- engine 経由で proposal に 5 部構成見出しが全部含まれる
- 既存テストを壊さない: `tests/test_engine.py` も実行して緑を確認

## 完了条件
`.venv/bin/python -m pytest tests/test_rules_crash.py tests/test_engine.py -q` 全緑
+ black + 共通規約の最終報告。
