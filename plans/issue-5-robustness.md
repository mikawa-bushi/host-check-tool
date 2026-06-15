# Plan: issue-5 — 堅牢化（§4-1 / §4-3）

## Goal (issue #5)
「沈黙して誤った成功」を返す広域 except を限定し失敗を記録。`exec()` 設定の安全性を明文化・点検。

## 変更設計

### S1 広域 except の限定化＋失敗記録
- `report.build_charts` (report.py): 全体を包む `except Exception: pass` を
  `except (sqlite3.Error, OSError) as exc:` に限定し `logger.warning(...)` で記録。
  想定外（バグ由来）の例外は伝播させる。disk 除算は既に `total==0` ガード済で値例外なし。
  - report.py に `import logging` / `import sqlite3` / `logger` を追加。
- `Storage.enforce_size_budget` (storage.py): retention 失敗の `except Exception: pass` を
  `except (sqlite3.Error, OSError)` に限定し、`logger.warning` ＋ `record_event("db_guard_error", ...)`
  で観測可能にする（retention 連続失敗が ops_events に残る）。VACUUM 失敗も同様に限定＋log。
  - storage.py に `import logging` / `logger` を追加。

### S2 exec 設定のセキュリティ運用ガイド（docs）
`docs/procdiag.md` に「設定ファイルの安全性」節を追加: `config.py` は `exec()` で実行されるため
信頼できる場所に root 所有・非 world-writable で置くこと、systemd 常駐時の注意を明記。

### S3 doctor に config 権限チェック追加
`doctor._check_config_security(config_path)` を追加し `run_doctor(..., config_path=None)` の
末尾チェックに組み込む（8→9 チェック）。
- 解決パス: `config_path` or `DEFAULT_CONFIG_PATH`。
- 不在 → info「設定ファイルなし（既定値）」。
- world-writable (mode & 0o002) → degraded「誰でも書込可能。exec() されるためコード実行リスク」。
- それ以外 → ok。
- cli doctor は `run_doctor(cfg, config_path=config_path)` を渡す。
- test_doctor の `len(checks) == 8` 2 箇所を 9 に更新。

## テスト
- `test_report.py`: 閉じた store で build_charts が空 ChartData を返す（sqlite3.Error は捕捉）/
  store.series が ValueError を投げると build_charts が伝播する（限定の確認）。
- `test_retention.py`: retention を sqlite3.Error で失敗させると enforce_size_budget が
  `db_guard_error` ops_event を残す。
- `test_doctor.py`: `_check_config_security` の不在=info / world-writable=degraded / 通常=ok。
  既存 `len(checks)` を 9 に更新。
- 既存スイート緑。

## Out of scope
findings/JSON/終了コード仕様は不変（doctor は degraded 追加で従来通り exit 2）。§2/§3/§5。
defensive wall（1失敗でループを殺さない）方針自体は維持し、広すぎる箇所のみ限定。
