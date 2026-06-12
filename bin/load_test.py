#!/usr/bin/env python3
"""M8 load test: measure Tier-2b cost and DB growth with N dummy processes.

Design §10-5 requires measuring the process-scan (Tier 2b) cost and DB size
with ~500 extra processes alive.  This script:

1. spawns N dummy ``sleep`` processes,
2. micro-benchmarks ``ProcessSampler.collect()`` in isolation,
3. runs a real ``Recorder`` session for ``--duration`` seconds and reads the
   self-measurement columns and the resulting DB size back from storage.

Run from the repo root with the project venv::

    .venv/bin/python bin/load_test.py --procs 500 --duration 60
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from procdiag.config import ProcdiagConfig
from procdiag.core.sampler import RunContext
from procdiag.recorder import Recorder
from procdiag.samplers.process import ProcessSampler
from procdiag.storage import Storage


def spawn_dummies(n: int) -> list[subprocess.Popen]:
    procs = []
    for _ in range(n):
        procs.append(
            subprocess.Popen(
                ["sleep", "600"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
    return procs


def kill_dummies(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            pass


def bench_tier2b(rounds: int = 12) -> dict:
    """Measure ProcessSampler.collect() cost in isolation (stub registry)."""
    counter = iter(range(10**9))
    sampler = ProcessSampler(resolve_proc=lambda *a: next(counter))
    ctx = RunContext(ProcdiagConfig())
    sampler.setup(ctx)
    sampler.collect(ctx)  # warm-up: populate the PID cache / cpu_percent base

    times_ms = []
    n_rows = 0
    for _ in range(rounds):
        t0 = time.perf_counter()
        batches = sampler.collect(ctx)
        times_ms.append((time.perf_counter() - t0) * 1000.0)
        n_rows = sum(len(b.rows) for b in batches)
        time.sleep(0.2)
    return {
        "median_ms": statistics.median(times_ms),
        "p95_ms": sorted(times_ms)[int(len(times_ms) * 0.95) - 1],
        "rows": n_rows,
    }


def run_record(duration: float, db_path: Path) -> dict:
    cfg = ProcdiagConfig(db_path=str(db_path))
    rec = Recorder(cfg, duration=duration)
    rc = rec.run()

    with Storage(db_path, read_only=True) as store:
        counts = store.counts()
        self_cpu = [
            r["self_cpu"]
            for r in store.rows("sys_samples")
            if r["self_cpu"] is not None
        ]
    steady = self_cpu[10:]  # drop the startup transient (registry warm-up)
    return {
        "rc": rc,
        "db_bytes": counts.get("db_bytes", 0),
        "sys_rows": counts.get("sys_samples", 0),
        "proc_rows": counts.get("proc_samples", 0),
        "self_cpu_avg": statistics.fmean(self_cpu) if self_cpu else 0.0,
        "self_cpu_max": max(self_cpu) if self_cpu else 0.0,
        "self_cpu_steady": statistics.fmean(steady) if steady else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--procs", type=int, default=500)
    ap.add_argument("--duration", type=float, default=60.0)
    args = ap.parse_args()

    import psutil

    base_nproc = len(psutil.pids())
    print(f"ベースライン: {base_nproc} プロセス。ダミー {args.procs} 個を起動...")
    dummies = spawn_dummies(args.procs)
    try:
        time.sleep(1.0)
        total = len(psutil.pids())
        print(f"起動後: {total} プロセス\n")

        print("--- Tier2b (ProcessSampler.collect) 単体ベンチ ---")
        b = bench_tier2b()
        per_proc_us = b["median_ms"] * 1000.0 / max(total, 1)
        pct_at_10s = b["median_ms"] / 10_000.0 * 100.0
        print(
            f"  1回の走査     : 中央値 {b['median_ms']:.1f} ms / p95 {b['p95_ms']:.1f} ms"
        )
        print(f"  記録行数      : {b['rows']} 行/回")
        print(f"  プロセス単価  : {per_proc_us:.0f} µs/プロセス")
        print(f"  10秒間隔負荷  : {pct_at_10s:.3f}% (Tier2b 由来の理論値)\n")

        print(f"--- 実 record ({args.duration:.0f}秒, 既定間隔) ---")
        with tempfile.TemporaryDirectory() as td:
            r = run_record(args.duration, Path(td) / "load.db")
        bytes_per_sec = r["db_bytes"] / max(args.duration, 1)
        proj_48h_mb = bytes_per_sec * 48 * 3600 / 1e6
        print(f"  終了コード    : {r['rc']}")
        print(f"  行数          : sys={r['sys_rows']}  proc={r['proc_rows']}")
        print(
            f"  自己CPU       : 平均 {r['self_cpu_avg']:.2f}%  最大 {r['self_cpu_max']:.2f}%"
            f"  定常 (起動10秒除外) {r['self_cpu_steady']:.2f}%"
        )
        print(f"  DB容量        : {r['db_bytes']/1e6:.1f} MB ({args.duration:.0f}秒)")
        print(f"  48時間外挿    : 約 {proj_48h_mb:.0f} MB (保持削除を考慮しない上限)")

        budget = ProcdiagConfig().budget.overhead_budget_pct
        verdict = (
            "OK (予算内)"
            if r["self_cpu_steady"] <= budget
            else "予算超過 — 自動減速域 (定常で予算を超える場合はT2b間隔が自動拡大)"
        )
        print(
            f"\n判定: 自己CPU 定常 {r['self_cpu_steady']:.2f}% / 予算 {budget:.1f}% → {verdict}"
        )
        return 0
    finally:
        kill_dummies(dummies)
        print(f"\nダミー {len(dummies)} プロセスを終了しました。")


if __name__ == "__main__":
    raise SystemExit(main())
