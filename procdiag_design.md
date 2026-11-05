# procdiag 設計書

Linux向けプロセス資源診断ツール — 一定期間の記録から、決定論的ルールで改善提案を生成する

バージョン: Draft 1.0 (2026-06-11)
ステータス: 実装前設計(Claude Codeでの実装を想定)

---

## 1. 概要

### 1.1 目的

工場のエッジPC・ラインサーバーなど「IT人材が常駐しない環境のLinuxマシン」を対象に、
CPU・メモリ・ディスクの使用状況を一定期間低負荷で記録し、
**決定論的な検出ルール**に基づいて運用レベルの改善提案を日本語レポートとして出力する。

### 1.2 ポジショニング

- netdiag(ネットワーク診断)の兄弟ツール。「PCが重い・落ちる・ディスクが満杯」の原因切り分けを担う
- Glances/netadata等のリアルタイム監視ダッシュボードとは異なり、「記録→診断→提案」の事後診断ワークフロー
- py-spy/memray等のコードプロファイラとは異なり、運用レベル(プロセス・サービス・ディスク)の診断に徹する
- 記録メトリクスはCSV/JSONエクスポートでmonitor-appに連携可能

### 1.3 設計原則(スイート共通)

1. **宣言的なconfig一枚**: Pydantic検証付き `config.py` で全挙動を定義
2. **オフライン・閉域網ファースト**: 外部通信ゼロ。依存は最小限(psutil + Jinja2 + Pydantic)
3. **3コマンドで動く**: `pip install` → `procdiag init` → `procdiag record`
4. **LLM非依存**: 提案はルール×テンプレートで決定論的に生成。出力は機械可読(JSON)にし、外部でのLLM活用は利用者に委ねる
5. **観測者効果の最小化と透明性**: 自身のオーバーヘッドを計測・報告し、予算超過時は自動減速

### 1.4 非目標 (Non-Goals)

- リアルタイムダッシュボード(必要ならmonitor-appに流す)
- コードレベルのプロファイリング(Pythonプロセス検出時にpy-spyの利用を「提案」するに留める)
- アラート通知・常駐エージェント基盤(将来検討)
- Windows/macOS対応(Linux専用)
- eBPF等のroot必須・カーネル依存の高度な計測

---

## 2. 動作環境

| 項目 | 要件 | 備考 |
|---|---|---|
| OS | Linux (kernel 4.20+) | PSI利用のため。古いカーネルではPSIなしで降格動作 |
| cgroup | v2 (unified hierarchy) | v1環境ではcgroup層をスキップし降格動作 |
| Python | 3.10+ | |
| 権限 | 非root動作を基本 | 他ユーザープロセスの一部情報は欠損として降格 |
| init | systemd推奨 | `install-service` はsystemd前提。なくてもCLI直接実行可 |

### 2.1 権限による降格マトリクス

| データ | 非root | root | 降格時の挙動 |
|---|---|---|---|
| PSI (/proc/pressure/*) | ○ | ○ | カーネル非対応時はCPU%閾値トリガに切替 |
| cgroup統計 (/sys/fs/cgroup) | ○ | ○ | v1時はプロセス視点のみ |
| 自プロセス・同一ユーザーの/proc/[pid]/io | ○ | ○ | — |
| 他ユーザーの/proc/[pid]/io | × | ○ | io列をNULL記録、レポートに欠損率を明記 |
| /proc/[pid]/smaps_rollup (他ユーザー) | × | ○ | PSS精査をスキップしRSSで代用 |

**原則**: 取れないデータは静かに失敗せず「欠損として記録」し、レポートに欠損率と理由を明記する。

---

## 3. CLI仕様

```
procdiag init [DIR]              # config.py とデータディレクトリの雛形を生成
procdiag record [--duration 24h] [--config config.py] [--db data.db]
                                 # 記録を開始(フォアグラウンド)。duration省略時は無期限
procdiag report [--db data.db] [--out report.html] [--json report.json]
                                 # HTMLレポート + 機械可読JSONを生成
procdiag advise [--db data.db]   # 検出ルールを実行し提案をターミナル/JSONに出力
                                 # (reportに包含されるが単独実行も可)
procdiag status [--db data.db]   # 記録中DBの概況(期間、サンプル数、自己負荷)を表示
procdiag install-service         # systemdユニットファイルを生成・登録(--user対応)
procdiag export [--format csv|json] [--rollup 1min]
                                 # monitor-app等への連携用エクスポート
```

- すべてのコマンドは終了コードで成否を返す(CI・スクリプト組込みを考慮)
- `record` はSIGTERM/SIGINTでバッファをフラッシュして正常終了(systemd stopと整合)

---

## 4. サンプリング設計

### 4.1 全体構成: 3層 + イベント駆動

| 層 | 対象 | 既定間隔 | 取得方法 | 実測コスト目安 |
|---|---|---|---|---|
| Tier 1 | システム全体 + PSI | 1秒 | psutil + /proc/pressure 直読み | < 1 ms/回 |
| Tier 2a | cgroup(サービス視点) | 5秒 | /sys/fs/cgroup 直読み | 数ms/回 |
| Tier 2b | 全プロセス走査(犯人特定) | 10秒 | psutil (Processキャッシュ) | 約15-20ms/200proc |
| Tier 3 | パーティション容量・watch_dirs・journald | 60秒 / 1時間 | statvfs, os.scandir | 走査対象に依存 |
| トリガ | PSI閾値超過 → 2a+2bを即時臨時実行 + イベント記録 | イベント駆動 | — | 発火時のみ |

実測根拠(Linuxコンテナ、psutil 7.x):
- システム全体メトリクス取得: 中央値 0.19 ms
- プロセス走査: 約 0.075 ms/プロセス(io_counters込みで約 0.086 ms/プロセス)
- 200プロセス環境推定: 15-17 ms/回 → 10秒間隔で約0.17%、合計負荷は通常時0.5%未満

### 4.2 Tier 1: システム全体(1秒)

取得項目:
- `psutil.cpu_percent(percpu=True)` — コア別。**最大コア使用率**を別途保持(シングルコア飽和検出用)
- `psutil.virtual_memory()` — total/available/used
- `psutil.swap_memory()` — used/sin/sout(スワップイン・アウトの増分が重要)
- `psutil.disk_io_counters(perdisk=True)` — read/write bytes, busy_time
- `/proc/loadavg`
- **PSI**: `/proc/pressure/cpu`, `/proc/pressure/memory`, `/proc/pressure/io`
  - 各ファイルの `some avg10 / avg60 / avg300` と `total`(累積µs)をパース
  - memory/ioは `full` 系列も取得

### 4.3 Tier 2a: cgroup走査(5秒)

- `/sys/fs/cgroup/system.slice/*/`, `/sys/fs/cgroup/user.slice/`(必要に応じ階層を辿る)を列挙
- 各ユニットから読むファイル:
  - `cpu.stat` → usage_usec(累積。差分でCPU使用率を算出)
  - `memory.current`, `memory.peak`(カーネル対応時), `memory.swap.current`
  - `io.stat`(対応時)→ rbytes/wbytes
  - `pids.current`
- **狙い**: cgroupは短命の子プロセスの消費も親に積算するため、ポーリングの隙間で生死するプロセスの会計漏れを防ぐ
- Dockerコンテナ(`system.slice/docker-*.scope` 等)も同じ仕組みで自動的に対象になる
- cgroup列挙はrecord開始時+5分ごとに更新(ユニットの増減に追従)

### 4.4 Tier 2b: プロセス走査(10秒)

- `psutil.Process` オブジェクトを **PIDキャッシュとして保持**し、同一インスタンスを再利用する
  - 理由: `cpu_percent()` は前回呼び出しとの差分計測。使い捨てでは常に0が返る
  - 消えたPIDは `NoSuchProcess` 捕捉時にキャッシュから除去
  - 新規PIDは走査時に追加(初回サンプルのcpu_percentは記録しない)
- `Process.oneshot()` コンテキストで属性をまとめ読み
- 取得属性: name, cmdline, cpu_percent, memory_info(rss/vms), num_threads, num_fds, io_counters(可能なら), status, create_time
- **プロセスの同一性**: 記録キーは `(name, sha1(cmdline正規化)[:12])`。プロセス台帳テーブルに正規化し、サンプル行は台帳IDのみ持つ
  - cmdline正規化: 一時的な引数(PID、日付入りパス等)による台帳爆発を防ぐため、数字列をプレースホルダに置換するオプションを設ける(config: `cmdline_normalize: bool = True`)
- **PSS精査モード**: リーク疑い(後述ルール)が立ったプロセスに限り、`/proc/[pid]/smaps_rollup` からPSSを追加取得(Tier 2bの周期に同期、対象は同時最大5プロセス)

### 4.5 Tier 3: ディスク(60秒 / 1時間)

- 60秒: `psutil.disk_usage()` を全マウントポイントに対して実行(pseudo-fs除外: tmpfs, proc, sysfs等)
- 1時間(config可変): `watch_dirs` のディレクトリサイズを `os.scandir` 再帰で集計
  - 大規模ディレクトリ対策: 1回の走査に時間予算(既定10秒)を設け、超過時は深さを制限して概算値+「概算」フラグを記録
- journald使用量: `/var/log/journal` のサイズ集計(read権限がある場合)

### 4.6 スパイクトリガ(イベント駆動)

Tier 1のPSI値が閾値を超えた瞬間に、Tier 2a+2bを**即時臨時実行**し、イベントテーブルに全文脈を保存する。

既定閾値(config可変):
```
cpu:    some avg10 > 20.0
memory: some avg10 > 5.0   または full avg10 > 1.0
io:     some avg10 > 30.0
```

- PSI非対応カーネルでは代替トリガ: CPU全体>85%、メモリavailable<10%、スワップアウト増分>0
- **デバウンス**: 同一トリガの再発火は最短30秒間隔(config: `spike_cooldown_sec`)
- イベント記録内容: 発火時刻、トリガ種別、PSIスナップショット、その瞬間のプロセス上位10件(CPU順・メモリ順)、cgroup上位5件
- 連続発火が `spike_storm_threshold`(既定10回/5分)を超えたら「飽和状態」として1イベントに集約(イベントテーブルの肥大防止)

### 4.7 スケジューリングと時刻管理

- 単一スレッドのスケジューラループ + `time.monotonic()` ベース
  - 各Tierの次回実行時刻を `next_t += interval` で管理(sleepの積み上げによるドリフトを排除)
  - 実装はasyncio不使用のシンプルなループで良い(I/O待ちがほぼないため)
- 壁時計時刻(`time.time()`)はサンプルのタイムスタンプとしてのみ使用
- **欠測の明示**: monotonic差分が期待間隔の3倍を超えた場合(サスペンド、高負荷による遅延)、gapレコードを記録。診断材料として扱う(「この時間帯はマシン自体が応答不能だった」)
- recordプロセス自身のCPU時間・RSSも毎Tier1周期で記録(自己計測)

### 4.8 自動減速(オーバーヘッド予算)

- 直近60秒の自己CPU使用率を移動平均で監視
- `overhead_budget_pct`(既定1.0%)を超過したら:
  1. Tier 2bの間隔を1.5倍に拡大(上限60秒)
  2. 減速イベントをDBに記録(レポートに明記)
  3. 予算内に戻って10分経過したら段階的に復元
- `install-service` 生成のsystemdユニットに `CPUQuota=2%` を設定し、カーネル側でも強制(二重の安全網)

---

## 5. ストレージ設計

### 5.1 方式

- SQLite単一ファイル(WALモード、`synchronous=NORMAL`)
- **書き込みバッチ化**: 全Tierのサンプルをメモリ上のリングバッファに蓄積し、60秒ごとに1トランザクションでまとめ書き
  - 理由: 毎秒fsyncは自身のI/O観測を汚し、HDD/SDカードの摩耗要因になる
  - SIGTERM/SIGINT時およびスパイクイベント発生時は即時フラッシュ
- スキーマバージョンを `meta` テーブルに記録(マイグレーション対応)

### 5.2 スキーマ(DDL概要)

```sql
-- メタ情報
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- schema_version, hostname, kernel, boot_time, procdiag_version, 開始時config(JSON) 等

-- プロセス台帳
CREATE TABLE proc_registry (
  proc_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  cmdline_hash TEXT NOT NULL,
  cmdline_sample TEXT,            -- 代表cmdline(表示用)
  first_seen REAL, last_seen REAL,
  UNIQUE(name, cmdline_hash)
);

-- cgroupユニット台帳
CREATE TABLE cgroup_registry (
  cg_id INTEGER PRIMARY KEY,
  unit_path TEXT UNIQUE NOT NULL, -- 例: system.slice/nginx.service
  first_seen REAL, last_seen REAL
);

-- Tier1: システムサンプル(1秒)
CREATE TABLE sys_samples (
  ts REAL PRIMARY KEY,
  cpu_total REAL, cpu_max_core REAL,
  mem_used INTEGER, mem_available INTEGER,
  swap_used INTEGER, swap_in INTEGER, swap_out INTEGER,
  load1 REAL,
  psi_cpu_some10 REAL, psi_mem_some10 REAL, psi_mem_full10 REAL,
  psi_io_some10 REAL, psi_io_full10 REAL,
  disk_read_bytes INTEGER, disk_write_bytes INTEGER,
  self_cpu REAL, self_rss INTEGER  -- 自己計測
);

-- Tier2a: cgroupサンプル(5秒)
CREATE TABLE cg_samples (
  ts REAL, cg_id INTEGER,
  cpu_usec_delta INTEGER,         -- 前回からの増分
  mem_current INTEGER, mem_swap INTEGER,
  io_rbytes_delta INTEGER, io_wbytes_delta INTEGER,
  pids INTEGER,
  PRIMARY KEY (ts, cg_id)
);

-- Tier2b: プロセスサンプル(10秒)
CREATE TABLE proc_samples (
  ts REAL, proc_id INTEGER, pid INTEGER,
  cpu_pct REAL, rss INTEGER, pss INTEGER,  -- pssは精査時のみ非NULL
  num_threads INTEGER, num_fds INTEGER,
  io_read_delta INTEGER, io_write_delta INTEGER,  -- 欠損はNULL
  PRIMARY KEY (ts, proc_id, pid)
);

-- Tier3: ディスクサンプル
CREATE TABLE disk_samples (
  ts REAL, mountpoint TEXT, total INTEGER, used INTEGER,
  PRIMARY KEY (ts, mountpoint)
);
CREATE TABLE dir_samples (
  ts REAL, path TEXT, size_bytes INTEGER, is_estimate INTEGER,
  PRIMARY KEY (ts, path)
);

-- スパイクイベント
CREATE TABLE spike_events (
  event_id INTEGER PRIMARY KEY,
  ts REAL, trigger_type TEXT,     -- cpu / memory / io / storm
  psi_snapshot TEXT,              -- JSON
  top_processes TEXT,             -- JSON(CPU順・メモリ順 各10件)
  top_cgroups TEXT                -- JSON
);

-- 欠測・運用イベント
CREATE TABLE ops_events (
  ts REAL, kind TEXT, detail TEXT -- gap / throttle_down / throttle_restore / flush 等
);

-- ロールアップ(1分)
CREATE TABLE sys_rollup_1m (
  ts_min REAL PRIMARY KEY,
  cpu_avg REAL, cpu_max REAL, cpu_p95 REAL,
  cpu_max_core_max REAL,
  mem_used_avg INTEGER, mem_used_max INTEGER,
  psi_cpu_max REAL, psi_mem_max REAL, psi_io_max REAL,
  swap_out_sum INTEGER
);
CREATE TABLE proc_rollup_1m (
  ts_min REAL, proc_id INTEGER,
  cpu_avg REAL, cpu_max REAL,
  rss_avg INTEGER, rss_max INTEGER,
  fds_max INTEGER, threads_max INTEGER,
  PRIMARY KEY (ts_min, proc_id)
);
-- cgroup版ロールアップも同様(cg_rollup_1m)
```

### 5.3 ロールアップと保持

- ロールアップは書き込みバッチのフラッシュ時に増分実行(別プロセス不要)
- **平均だけに潰さない**: avg / max / p95 を必ず併存(p95はバケット内ソートで算出。1分=60サンプルなので軽量)
- 保持ポリシー(config可変):

| データ | 既定保持 | 削除方法 |
|---|---|---|
| 生サンプル(Tier1/2) | 48時間 | フラッシュ時にDELETE(1時間ごと) |
| 1分ロールアップ | 90日 | 同上 |
| spike_events / ops_events | 無期限 | ユーザー任意 |
| disk/dir_samples | 90日 | 同上 |

- 容量見積り(200プロセス環境): 生48時間 ≈ 150-200MB、ロールアップ90日 ≈ 100MB前後。`status` コマンドでDB容量と予測を表示

---

## 6. 検出ルールエンジン

### 6.1 アーキテクチャ

- ルール = 「検出関数 + 提案テンプレート + メタデータ」の組
- 各ルールは独立したPythonクラス(`Rule` 基底クラスを継承)。判定はDBのロールアップ/イベントに対するクエリと統計処理
- 出力は `Finding` オブジェクトのリスト:

```python
@dataclass
class Finding:
    rule_id: str            # 例: "MEM-LEAK-01"
    severity: str           # info / warning / critical
    subject: str            # 対象(プロセス名、ユニット名、マウントポイント)
    evidence: dict          # 根拠データ(数値、期間、決定係数等)— JSON化可能
    detected_at: tuple      # 該当期間 (start_ts, end_ts)
    confidence: str         # high / medium / low(根拠の強さを明示)
```

- 提案文はJinja2テキストテンプレートで `Finding` から生成。フォーマットは固定5部構成:
  **[検出] → [根拠] → [暫定対処] → [恒久対処] → [影響予測]**
- ルールはエントリポイント(`procdiag.rules`)で登録し、ユーザー定義ルールの追加を可能にする(config: `extra_rules: list[str]`)

### 6.2 初期ルールカタログ(v1.0で実装する13ルール)

| ID | 検出内容 | 主な判定ロジック |
|---|---|---|
| MEM-LEAK-01 | プロセスのメモリリーク疑い | proc_rollup_1mのrss_avgに対する線形回帰。傾き>閾値 かつ R²>0.9 かつ 観測≥6時間。PSS精査で確証度を昇格 |
| MEM-PRESSURE-01 | 慢性的メモリ不足 | psi_mem_some10 のmaxが閾値超の時間帯が日次で反復 + swap_out_sum>0 |
| CPU-SINGLECORE-01 | シングルコア飽和 | cpu_max_core_max≈100% が継続する一方 cpu_avg が低い。該当時間帯のプロセス上位と突合。Pythonプロセスならpy-spy提案を付記 |
| CPU-SATURATION-01 | CPU全体の飽和 | psi_cpu_some10>20が継続。spike_eventsの犯人プロセスを列挙 |
| CPU-PERIODIC-01 | 周期スパイク(タスク衝突) | spike_eventsの発火時刻の周期性検出(同時刻±2分に日次/時次で反復)。cron/systemd timer一覧との突合を提案に含める |
| IO-BOTTLENECK-01 | I/Oボトルネック | psi_io_some10>30が継続 + 該当時間帯のio_write/read上位プロセス特定 |
| DISK-FULL-01 | ディスク満杯予測 | disk_samplesのusedに対する線形外挿。「残りN日」を算出(増加が線形でない場合は直近7日傾向を優先) |
| DISK-GROWTH-01 | 増加源ディレクトリ特定 | dir_samplesの増分ランキング。DISK-FULL-01と同一パーティションなら関連付け |
| DISK-JOURNAL-01 | journald肥大 | journalサイズ>閾値(既定4GB)。SystemMaxUse設定の提案 |
| FD-LEAK-01 | fdリーク疑い | fds_maxの単調増加(線形回帰)。ulimit到達までの予測日数 |
| THREAD-LEAK-01 | スレッド数の単調増加 | threads_maxの線形回帰 |
| SWAP-THRASH-01 | スワップスラッシング | swap_in/outが同時間帯に高頻度 + psi_mem_full10>0 |
| ZOMBIE-01 | ゾンビプロセス蓄積 | status=zombieのプロセス数が増加傾向 |

### 6.3 誤検知の抑制方針

- 線形回帰系ルールは最低観測時間(既定6時間)未満では発火しない
- すべてのFindingに `confidence` を付与し、レポートでは根拠(R²、観測期間、サンプル数)を必ず併記
- 「再起動を跨いでパターンが再現するか」を確証度の昇格条件にする(proc_registry単位で再起動を検出可能)

---

## 7. レポート生成

### 7.1 出力物

- `report.html`: 単一HTMLファイル(CSS/JSインライン、外部CDN参照なし=閉域網対応)
  - 構成: ①サマリ(健全性スコアとFinding一覧) ②提案詳細(5部構成テンプレート) ③時系列チャート(CPU/メモリ/PSI/ディスク。SVGをサーバーサイド生成、軽量なインラインJSでツールチップ程度) ④スパイクイベント一覧 ⑤観測品質(自己負荷、欠損率、欠測gap、減速履歴)
- `report.json`: Finding全件+主要統計の機械可読版(外部でのLLM要約・チケット起票・monitor-app連携用)
- ターミナル出力(`advise`): richは使わず標準ライブラリで簡易整形(依存最小化)

### 7.2 「観測品質」セクション(必須)

レポートの信頼性を担保するため、以下を必ず明記する:
- 観測期間、欠測gap合計、自己オーバーヘッド(平均/最大)
- io_counters等の欠損率と理由(権限)
- システム全体CPUとプロセス合計CPUの差分 → 「未捕捉の短命プロセス活動: 約N%」(cgroup会計でカバーされる場合はその旨)

---

## 8. 設定(config.py)

```python
from procdiag.config import *

config = ProcdiagConfig(
    db_path="./procdiag.db",
    sampling=SamplingConfig(
        system_interval=1.0,
        cgroup_interval=5.0,
        process_interval=10.0,
        disk_interval=60.0,
        dir_scan_interval=3600.0,
        watch_dirs=[
            WatchDir(path="/var/log", time_budget_sec=10),
            WatchDir(path="/opt/myapp/data"),
        ],
        cmdline_normalize=True,
    ),
    trigger=TriggerConfig(
        psi_cpu_some10=20.0,
        psi_mem_some10=5.0,
        psi_io_some10=30.0,
        spike_cooldown_sec=30,
        spike_storm_threshold=10,   # 回/5分
    ),
    budget=BudgetConfig(
        overhead_budget_pct=1.0,
        throttle_factor=1.5,
        throttle_max_interval=60.0,
    ),
    retention=RetentionConfig(
        raw="48h", rollup="90d",
    ),
    rules=RulesConfig(
        disabled=[],                # 例: ["ZOMBIE-01"]
        extra_rules=[],             # ユーザー定義ルールのimportパス
        min_observation_hours=6.0,
        journal_size_warn_gb=4.0,
    ),
    report=ReportConfig(
        lang="ja",
        hostname_label=None,        # レポート表題用(例: "ライン制御PC")
    ),
)
```

- すべてPydanticモデルで検証。不正値は起動時に日本語エラーで停止
- `procdiag init` が上記の雛形(コメント付き)を生成

---

## 9. パッケージ構成

```
procdiag/
├── pyproject.toml            # 依存: psutil, pydantic>=2, jinja2
├── README.md
├── src/procdiag/
│   ├── __init__.py
│   ├── cli.py                # argparseベース(click等は使わない=依存最小化)
│   ├── config.py             # Pydanticモデル群
│   ├── sampler/
│   │   ├── scheduler.py      # monotonicベースの単一ループ
│   │   ├── tier1_system.py   # psutil + PSIパーサ
│   │   ├── tier2a_cgroup.py  # cgroup v2リーダー
│   │   ├── tier2b_process.py # Processキャッシュ管理
│   │   ├── tier3_disk.py     # statvfs + scandir走査
│   │   ├── trigger.py        # PSI閾値監視・デバウンス・ストーム集約
│   │   └── selfmeter.py      # 自己計測・自動減速
│   ├── storage/
│   │   ├── db.py             # スキーマ・マイグレーション・バッチライタ
│   │   └── rollup.py         # 増分ロールアップ・保持削除
│   ├── rules/
│   │   ├── base.py           # Rule基底・Findingデータクラス
│   │   ├── memory.py         # MEM-*, SWAP-*
│   │   ├── cpu.py            # CPU-*
│   │   ├── io_disk.py        # IO-*, DISK-*
│   │   ├── leak.py           # FD-*, THREAD-*, ZOMBIE-*
│   │   └── templates/        # 提案文Jinja2テンプレート(ルールIDごと)
│   ├── report/
│   │   ├── html.py           # 単一HTML生成(SVGチャート含む)
│   │   ├── json_out.py
│   │   └── templates/
│   ├── service.py            # systemdユニット生成
│   └── export.py             # CSV/JSONエクスポート
└── tests/
    ├── unit/                 # PSIパーサ、回帰検出、正規化等の純粋ロジック
    ├── fixtures/             # /proc・/sys/fs/cgroupのモックツリー、合成DB
    └── integration/          # 短時間record→report のE2E
```

---

## 10. テスト戦略

1. **純粋ロジックの単体テスト**: PSIパース、cmdline正規化、線形回帰判定、外挿予測、デバウンス — 全て入出力が決まる関数として書き、フィクスチャで網羅
2. **擬似/proc・/sys**: ファイルツリーのフィクスチャを用意し、リーダー層はルートパスを注入可能に設計(`/proc` `/sys/fs/cgroup` をconfigで差替え)→ コンテナ環境やCIでcgroup構成に依存せずテスト可能
3. **合成DBによるルールテスト**: 「リークありデータ」「周期スパイクデータ」等を生成するヘルパを作り、各ルールの検出・非検出(誤検知)の両側を検証
4. **E2E**: 実環境で `record --duration 60s` → `report` が正常完走し、自己負荷が予算内であることをアサート
5. **負荷テスト**: ダミープロセスを500個起動した状態でのTier2bコストとDB容量を計測

---

## 11. 実装マイルストーン(Claude Code向けの推奨順序)

1. **M1: 骨格** — config.py(Pydantic)、CLI骨格、DBスキーマ、`init` コマンド
2. **M2: サンプラー核心** — scheduler + Tier1(PSI含む) + バッチライタ + 自己計測。この時点で `record` が動き `status` で確認可能
3. **M3: プロセス・cgroup層** — Tier2a/2b、プロセス台帳、スパイクトリガ
4. **M4: ディスク層とロールアップ** — Tier3、増分ロールアップ、保持削除
5. **M5: ルールエンジン** — base + 優先4ルール(MEM-LEAK-01, DISK-FULL-01, CPU-PERIODIC-01, CPU-SINGLECORE-01)+ テンプレート
6. **M6: レポート** — JSON出力 → HTML(SVGチャート)→ 観測品質セクション
7. **M7: 残りルール9本 + export + install-service**
8. **M8: 負荷テスト・チューニング・README整備**

各マイルストーンは独立してテスト可能な状態で完了させること。

---

## 12. 未決事項(実装時に判断)

- [ ] ツール名の最終決定(本書では仮称 procdiag。PyPI重複確認要)
- [ ] cgroup v1環境(古いDocker等)をどこまでサポートするか(v1.0ではスキップ+警告を推奨)
- [ ] dir_samples走査のinotify化(v1.0は周期走査で十分と判断、v2候補)
- [ ] HTMLチャートの実装方式: 完全サーバーサイドSVG vs 軽量チャートライブラリ同梱(閉域網要件からCDN不可。インラインSVG推奨)
- [ ] proc_samplesのPRIMARY KEYにpidを含めるか(同名同cmdlineの複数インスタンス区別。本書はpid含む設計)
- [ ] 国際化(v1.0は日本語のみ。テンプレート分離はlang対応を見越した構造に)
