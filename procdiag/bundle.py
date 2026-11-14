"""``procdiag bundle`` — package everything needed to analyse a host elsewhere.

Production-floor PCs are usually on a closed network: the practical way to get
an expert's opinion is to carry the data out on a USB stick.  One command
writes a single ``.tar.gz`` with:

* ``procdiag.db``   — a consistent single-file copy of the recording (taken with
  SQLite's online backup, so it is safe while ``record`` keeps writing)
* ``report.html`` / ``report.json`` — the diagnosis of that copy
* ``snapshot.json`` — the host's state at bundling time
* ``doctor.json`` / ``status.json`` — observation capabilities and DB overview
* ``config.py``     — the configuration used (when present)
* ``README.txt``    — what is inside and how to analyse it on another PC

Process command lines may carry credentials (``--password=…``), so they are
removed from the copied DB unless ``keep_cmdline`` is set; process *names* are
kept, and no rule reads the command line, so the diagnosis is unaffected.
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import DEFAULT_CONFIG_PATH, ProcdiagConfig


@dataclass
class BundleResult:
    """Where the bundle was written and what it contains."""

    path: Path
    files: List[str] = field(default_factory=list)
    has_db: bool = False
    cmdline_redacted: bool = False


def default_bundle_name(hostname: str) -> str:
    """``procdiag-bundle-<host>-<YYYYmmdd-HHMMSS>.tar.gz``."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in hostname)
    return f"procdiag-bundle-{safe}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"


def copy_db(src: Path, dst: Path, *, keep_cmdline: bool) -> None:
    """Copy *src* into a self-contained single file at *dst*.

    The online backup API reads a consistent snapshot even while the recorder
    is writing; switching the copy out of WAL mode folds everything into one
    file so no ``-wal`` / ``-shm`` companions are needed.
    """
    source = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
        if not keep_cmdline:
            target.execute("UPDATE proc_registry SET cmdline_sample = NULL")
            target.commit()
            target.execute("VACUUM")  # do not leave the old text in free pages
    finally:
        source.close()
        target.close()


def create_bundle(
    config: ProcdiagConfig,
    *,
    config_path: Optional[str] = None,
    out: Optional[Path] = None,
    keep_cmdline: bool = False,
    snapshot_sample_sec: float = 1.0,
) -> BundleResult:
    """Write the bundle and return where it went (see the module docstring)."""
    from .diagnosis import Engine
    from .doctor import run_doctor
    from .report import build_charts, render_html, render_json
    from .snapshot import render_json as render_snapshot_json
    from .snapshot import run_snapshot
    from .status import collect_status
    from .storage import Storage

    hostname = socket.gethostname()
    out = Path(out) if out else Path(default_bundle_name(hostname))
    top = out.name.removesuffix(".tar.gz")
    result = BundleResult(path=out)

    with tempfile.TemporaryDirectory(prefix="procdiag-bundle-") as tmp:
        root = Path(tmp) / top
        root.mkdir()

        def write(name: str, text: str) -> None:
            (root / name).write_text(text, encoding="utf-8")
            result.files.append(name)

        db_src = Path(config.db_path)
        if db_src.exists():
            db_copy = root / "procdiag.db"
            copy_db(db_src, db_copy, keep_cmdline=keep_cmdline)
            result.files.append("procdiag.db")
            result.has_db = True
            result.cmdline_redacted = not keep_cmdline
            with Storage(db_copy, read_only=True) as store:
                diag = Engine(config).run(store)
                shown = diag.window
                charts = build_charts(
                    store,
                    since=shown.start_ts if shown else None,
                    until=shown.end_ts if shown else None,
                    config=config,
                )
                meta_hostname = store.meta().get("hostname")
                write(
                    "status.json",
                    json.dumps(collect_status(store), ensure_ascii=False, indent=2),
                )
            write(
                "report.html",
                render_html(
                    diag,
                    charts,
                    hostname_label=config.report.hostname_label,
                    meta_hostname=meta_hostname,
                ),
            )
            write("report.json", render_json(diag))

        items = run_snapshot(config, sample_sec=snapshot_sample_sec)
        write("snapshot.json", render_snapshot_json(items, hostname=hostname))

        checks = run_doctor(config, config_path=config_path)
        write(
            "doctor.json",
            json.dumps([c.to_dict() for c in checks], ensure_ascii=False, indent=2),
        )

        cfg_file = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        if cfg_file.exists():
            shutil.copyfile(cfg_file, root / "config.py")
            result.files.append("config.py")

        write("README.txt", _readme(hostname, result))

        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, "w:gz") as tar:
            tar.add(root, arcname=top)
    return result


def _readme(hostname: str, result: BundleResult) -> str:
    created = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "procdiag 診断バンドル",
        "======================",
        f"ホスト名      : {hostname}",
        f"作成日時      : {created}",
        f"procdiag 版数 : {__version__}",
        "",
        "収録ファイル:",
    ]
    descriptions = {
        "procdiag.db": "記録データ (SQLite)。別 PC で再診断できます",
        "report.html": "診断レポート (ブラウザで開く)",
        "report.json": "診断結果 (機械可読)",
        "status.json": "記録期間・サンプル数などの概況",
        "snapshot.json": "バンドル作成時点のホストの状態",
        "doctor.json": "観測能力 (PSI / cgroup / 権限など) の確認結果",
        "config.py": "使用した設定ファイル",
    }
    for name in result.files:
        lines.append(f"  {name:<14} {descriptions.get(name, '')}")
    if not result.has_db:
        lines.append(
            "  (記録 DB なし — procdiag record で記録すると傾向の診断も同梱されます)"
        )
    if result.cmdline_redacted:
        lines += [
            "",
            "※ 秘密情報の持ち出しを防ぐため、DB からプロセスのコマンドラインを",
            "  削除しています (プロセス名は残っており、診断結果には影響しません)。",
        ]
    lines += [
        "",
        "別 PC での解析:",
        "  pip install host-check-tool",
        "  procdiag check  --db procdiag.db --verbose",
        "  procdiag check  --db procdiag.db -o report.html",
        "",
    ]
    return "\n".join(lines)
