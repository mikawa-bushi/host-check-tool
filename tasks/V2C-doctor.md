# V2-C: `procdiag doctor` — 観測能力の事前診断 (EXT-09)

フェーズ: v0.2(3並列)/ 依存なし(v0.1.1 のコードベースが前提)
参照: `tasks/README.md`(共通規約)→ `procdiag_extension_proposal.md` EXT-09 →
設計書 §2.1(降格マトリクス — **この表の実行時可視化が本機能**)→
既存実装: `procdiag/cli.py`(コマンドの書式)、`procdiag/samplers/system.py`
(PSI 判定)、`cgroup.py`(v2 判定)、`tests/test_cli.py`(CliRunner の書式)

## 所有ファイル(これ以外に触れない)

```
procdiag/doctor.py            # 新規
procdiag/cli.py               # doctor コマンド追加のみ (既存コマンドに触れない)
tests/test_doctor.py          # 新規
```

並行タスクが recorder.py・storage.py・config.py・diagnosis/ を編集中。絶対に触れない。

## 仕様

### procdiag/doctor.py

```python
@dataclass
class DoctorCheck:
    name: str          # "psi", "cgroup_v2", "root", "io_counters", "smaps",
                       # "journal", "sensors", "db_disk"
    label: str         # 日本語表示名
    status: str        # "ok" | "degraded" | "info"
    detail: str        # 日本語。降格時は §2.1 の「降格時の挙動」に相当する影響説明

def run_doctor(config: ProcdiagConfig, *, proc_root=Path("/proc"),
               cgroup_root=Path("/sys/fs/cgroup")) -> list[DoctorCheck]
```

チェック項目(各 1 関数に分け、proc_root/cgroup_root 注入でテスト可能に):
1. **psi**: `proc_root/pressure/cpu` が読める → ok。不可 → degraded
   「PSI 非対応 — スパイクトリガは CPU%/メモリ残量の代替条件で動作します」
2. **cgroup_v2**: `cgroup_root/cgroup.controllers` 存在 → ok。不可 → degraded
   「cgroup v2 なし — サービス視点 (Tier2a) の記録はスキップされます」
3. **root**: `os.geteuid() == 0` → info「root: 全プロセスの I/O・PSS を取得可能」/
   非 root → info「非 root: 他ユーザープロセスの I/O・PSS は欠損として記録されます」
4. **io_counters**: psutil で最大 80 プロセスをサンプル走査し読取成功率を実測。
   100% → ok / それ未満 → degraded「I/O 欠損率 約 N% — IO-BOTTLENECK-01 の
   確証度が下がります (cgroup 統計で部分代替)」
5. **smaps**: 自プロセスの `proc_root/<pid>/smaps_rollup` が読める → ok / 不可 → degraded
   「PSS 精査不可 — メモリリークの確証度昇格が無効になります」
6. **journal**: `/var/log/journal` が存在し読める → ok / 不可 → degraded
   「journal 読取不可 — DISK-JOURNAL-01 と OOM 確証度昇格が制限されます」
7. **sensors**: `psutil.sensors_temperatures()` が非空 → ok / 空 → info
   「温度センサなし (将来のハードウェア健全性層は降格動作)」
8. **db_disk**: `config.db_path` の親パーティションの空き容量を表示。
   空き < 1GB → degraded「記録 DB の置き場が逼迫しています」/ それ以外 → ok。
   既存 DB があればサイズも表示
- 各チェックは try/except で包み、予期しない例外は degraded + 理由文字列に
  (doctor 自身が落ちては本末転倒)

### cli.py — doctor コマンド

```
procdiag doctor [--config PATH] [--db PATH] [--format text|json]
```
- text: ✔/⚠/ℹ 記号 + label + detail の一覧(check の出力書式に倣う)。
  末尾に「⚠ N 件の降格があります。記録は可能ですが該当データは欠損します」または
  「✔ すべての観測能力が利用可能です」
- json: `[{name,label,status,detail}...]` を ensure_ascii=False で
- **終了コード: degraded なし=0 / あり=2**(check の 0/1/2 と整合する「警告=2」)
- config/db は status コマンドと同じ解決順(--db > config の db_path)

## テスト要求(test_doctor.py)

- 擬似ツリー(tests/fixtures/ の既存 proc_tree / cgroup_tree を再利用してよい。
  新規 fixture を作る場合は tests/fixtures/doctor_* 配下)で:
  PSI あり/なし、cgroup v2 あり/なしの status 切替
- io_counters: psutil をモックして 100% / 50% の両方
- 予期しない例外を投げるチェックでも doctor が完走し degraded になる
- CliRunner: text 出力に記号と件数サマリ、json が json.loads 可能、
  降格ありで exit 2 / なし(全モック ok)で exit 0
- 実マシンでも `run_doctor(ProcdiagConfig())` が例外なく 8 件返す(スモーク1件)
- 回帰確認: `tests/test_cli.py` が緑のまま

## 完了条件
`.venv/bin/python -m pytest tests/test_doctor.py tests/test_cli.py -q` 全緑
+ black + 共通規約の最終報告。
