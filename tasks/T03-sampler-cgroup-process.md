# T03: samplers/cgroup.py + process.py — Tier2a/2b 犯人特定層

フェーズ: P2(並列)/ 依存: T01
参照: `tasks/README.md` → 設計書 §4.3, §4.4, §2.1(降格) / 実装計画 §3, §4.2
netdiag 参照: `/tmp/network-check-tool/netdiag/core/probe.py`(防護壁の流儀)

## 所有ファイル(これ以外に触れない)

```
procdiag/samplers/cgroup.py
procdiag/samplers/process.py
tests/test_sampler_cgroup_process.py
tests/fixtures/cgroup_tree/          # 擬似 /sys/fs/cgroup ツリー
```

`procdiag/samplers/__init__.py` は触らない。テストは直接 import。

## 契約(T01 提供 — 変更禁止)

- `procdiag.core.sampler`: `Sampler`, `SampleBatch`, `RunContext`, `register_sampler`
- `procdiag.core.util.normalize_cmdline`
- `Storage.cg_id_for(unit_path, ts) -> int` / `Storage.proc_id_for(name, hash, sample, ts) -> int`
  — **ただしサンプラーはStorageを直接触らない**。台帳解決のため RunContext 経由で
  コールバックを受ける設計にする: 本タスクで各サンプラーの `__init__` に
  `resolve_cg: Callable[[str, float], int]` / `resolve_proc: Callable[[str, str, str, float], int]`
  を取らせ、recorder(T05)が Storage のメソッドを束縛して渡す。
  テストでは連番を返すスタブで十分
- 列順契約:
  - `cg_samples`: `ts, cg_id, cpu_usec_delta, mem_current, mem_swap,
    io_rbytes_delta, io_wbytes_delta, pids`
  - `proc_samples`: `ts, proc_id, pid, cpu_pct, rss, pss, num_threads, num_fds,
    io_read_delta, io_write_delta`(欠損は None)

## 実装要求

### CgroupSampler (`@register_sampler`, name="cgroup", tier="T2a", interval_key="cgroup_interval")
- `setup(ctx)`: `ctx.cgroup_root/"cgroup.controllers"` の存在で v2 判定 →
  `ctx.capabilities["cgroup_v2"]`。v1/なしなら notes に日本語降格理由、collectは常に[]
- ユニット列挙: `system.slice/*/`(`.service`/`.scope`)と `user.slice` 直下。
  列挙結果は5分キャッシュ(設計書: 開始時+5分ごと更新)
- 各ユニットから `cpu.stat`(usage_usec)、`memory.current`, `memory.swap.current`,
  `io.stat`(rbytes/wbytes合算)、`pids.current` を読む。ファイル欠如・読取失敗は
  その列 None(部分降格)。cpu/io は**前回値との差分**を記録(初回は記録しない)
- 全ファイルアクセスは `ctx.cgroup_root` 起点(テスト注入のため)

### ProcessSampler (`@register_sampler`, name="process", tier="T2b", interval_key="process_interval")
設計書 §4.4 を忠実に:
- `psutil.Process` を **PIDキャッシュ** `dict[int, psutil.Process]` で保持・再利用
  (cpu_percent の差分計測のため)。消えたPIDは `NoSuchProcess` で除去。
  新規PIDの初回 cpu_percent は記録しない(行ごと飛ばすのではなく cpu_pct=None で記録)
- `Process.oneshot()` 内で name, cmdline, cpu_percent, memory_info.rss, num_threads,
  num_fds, io_counters, status を読む。`AccessDenied` は io列のみ None にして続行し、
  **欠損カウンタ**を自インスタンスに集計(`self.io_denied`/`self.io_total` —
  観測品質の欠損率算出にT05/T06が使う)
- proc_id は `normalize_cmdline(cmdline, replace_digits=ctx.config.sampling.cmdline_normalize)`
  → `resolve_proc(name, hash, sample, ts)` で解決
- **PSS精査モード**: `self.pss_targets: set[int]`(proc_id集合、最大5)を公開属性とし、
  集合に入っているプロセスのみ `ctx.proc_root/<pid>/"smaps_rollup"` から PSS(kB→bytes)を
  読んで pss 列に入れる(読めなければ None)。集合への追加はT06ルール側の将来連携用で、
  本タスクでは「入っていれば読む」機構だけ実装
- zombieプロセス: status=="zombie" の数を数え `self.last_zombie_count` に保持
  (ZOMBIE-01ルールが将来使う。proc_samplesにも通常どおり記録)

## テスト要求

- cgroup: 擬似ツリー(`system.slice/nginx.service/{cpu.stat,memory.current,...}` を
  数ユニット分)で、collect 2回 → 差分列が正しい/欠如ファイルがNone/列数一致。
  v2でないツリー(cgroup.controllers無し)で capabilities=False・collect=[]
- process: 実psutilで自プロセスを含む走査が動き、proc_samples行の列数一致・
  2回目collectで自プロセスのcpu_pctが非None。AccessDenied経路は io_counters を
  モンキーパッチして欠損カウンタ増を確認。PSS: proc_root擬似ツリーに
  smaps_rollup を置き pss_targets 指定時のみ読まれること
- 台帳スタブ: 同一 (name,hash) が同じ proc_id に解決されること(スタブ側で検証)

## 完了条件
`.venv/bin/python -m pytest tests/test_sampler_cgroup_process.py -q` 全緑 + black + 最終報告。
