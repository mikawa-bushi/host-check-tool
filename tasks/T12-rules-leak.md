# T12: ルール追加 — FD-LEAK-01 / THREAD-LEAK-01 / ZOMBIE-01

フェーズ: P4(並列)/ 依存: T09
参照: `tasks/README.md` → 設計書 §6.2(該当3行), §6.3 / 既存実装:
`procdiag/diagnosis/leak.py`(T06作成のスタブ), `memory.py`(MEM-LEAK-01 が
線形回帰ルールの手本), `trend.py`, `tests/synth.py`

## 所有ファイル(これ以外に触れない)

```
procdiag/diagnosis/leak.py         # スタブを実装で置き換える
procdiag/diagnosis/templates/FD-LEAK-01.j2
procdiag/diagnosis/templates/THREAD-LEAK-01.j2
procdiag/diagnosis/templates/ZOMBIE-01.j2
tests/test_rules_leak.py
```

`tests/synth.py` は読み取り+ヘルパ追記のみ可。

## 実装するルール(設計書 §6.2)

- **FD-LEAK-01**: proc_rollup_1m の fds_max を proc_id ごとに fit_linear。
  単調増加(slope>0.5/h 目安・定数化)かつ R²>0.9 → WARNING。
  evidence に **ulimit 到達までの予測日数**(`resource.getrlimit(RLIMIT_NOFILE)` の
  soft値…は対象プロセスのものが取れないため、evidence には一般既定 1024/4096 への
  到達日数を併記し、proposal で `cat /proc/<pid>/limits` の確認を促す)
- **THREAD-LEAK-01**: 同様に threads_max の fit_linear → WARNING
- **ZOMBIE-01**: proc_samples で status... は記録していない(列にない)ため、
  **proc_registry の name が `<defunct>` を含む台帳数の増加**では検出できない点に注意。
  実装は: proc_samples を proc_id で集計し、`proc_registry.name` が defunct系、
  または **同一 ts のサンプル中 cpu_pct=0・rss=0・num_threads=0 のプロセス数**を
  ゾンビ近似として時系列化し、増加傾向(fit_linear slope>0)で INFO。
  近似である旨を confidence=low と proposal に明記。
  ※ もし T03 実装で zombie が別の形で記録されていれば(`ops_events` 等)そちらを
  優先してよい。実装前に `procdiag/samplers/process.py` を読んで確認すること

## テスト要求
synth.py(fds/threads の slope ヘルパは T06 の add_proc_series が対応済みのはず。
不足なら追記)で検出・非検出両側 + 観測時間不足ガード。

## 完了条件
`.venv/bin/python -m pytest tests/test_rules_leak.py tests/test_engine.py -q` 全緑
+ black + 最終報告。
