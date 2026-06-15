"""``procdiag`` command-line interface.

Conclusion-first host-resource diagnosis: ``procdiag record`` collects
metrics, ``procdiag check`` (alias ``advise``) runs all rules and prints
the root cause and next steps.  The remaining commands support that core
workflow (status, export, explain, prune) or system integration (service).

Design mirrors netdiag's ``cli.py`` verbatim where possible:

- ``click.group`` + ``version_option`` using ``_resolve_version``
- Thin command handlers: all logic delegated to modules via *deferred* imports
- Delayed imports for T05/T06/T07 modules (recorder, engine, report) so this
  file's own import and the tests of non-record commands succeed even before
  those parallel tasks are complete.
- ``load_plugins()`` called once at group invocation (same as netdiag).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

import click

from . import __version__
from .plugins import load_plugins

# ---------------------------------------------------------------------------
# Version resolution (mirrors netdiag _resolve_version)
# ---------------------------------------------------------------------------


def _resolve_version() -> str:
    """Installed package version, falling back to the in-tree ``__version__``.

    Works both when pip-installed (distribution metadata present) and from a
    source checkout.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("host-check-tool")
        except PackageNotFoundError:
            return __version__
    except ImportError:  # pragma: no cover
        return __version__


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=_resolve_version(), prog_name="procdiag")
def cli() -> None:
    """procdiag — local-first Linux ホスト資源診断ツール。"""
    load_plugins()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("dir", default=".", type=click.Path())
def init(dir: str) -> None:
    """DIR に config.py と data/ ディレクトリを生成します (既定: カレントディレクトリ)。"""
    from .config import CONFIG_TEMPLATE

    target = Path(dir)
    target.mkdir(parents=True, exist_ok=True)
    config_path = target / "config.py"
    data_dir = target / "data"

    if config_path.exists():
        click.echo(
            f"警告: {config_path} はすでに存在します。上書きしません。", err=True
        )
    else:
        config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        click.echo(f"config.py を生成しました: {config_path}")

    data_dir.mkdir(exist_ok=True)
    click.echo(f"data/ ディレクトリを生成しました: {data_dir}")
    click.echo(
        "\n次のステップ:\n"
        "  1. config.py を必要に応じて編集してください\n"
        "  2. procdiag record --config config.py で記録を開始してください"
    )


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--duration", default=None, help="記録時間 (例: 24h, 60s)。省略時は無期限。"
)
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(),
    help="設定ファイルのパス (既定: ./config.py)。",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(),
    help="記録先 DB ファイルのパス (config の db_path を上書き)。",
)
def record(
    duration: Optional[str], config_path: Optional[str], db_path: Optional[str]
) -> None:
    """CPU・メモリ・ディスクを記録します (フォアグラウンド)。Ctrl-C で安全に停止します。"""
    from .recorder import Recorder  # T05 — deferred

    from .config import load_config
    from .core.util import parse_duration

    cfg = load_config(Path(config_path) if config_path else None)
    if db_path:
        cfg = cfg.model_copy(update={"db_path": db_path})

    duration_sec: Optional[float] = None
    if duration:
        try:
            duration_sec = parse_duration(duration)
        except ValueError as exc:
            click.echo(f"エラー: {exc}", err=True)
            sys.exit(2)

    click.echo("記録を開始します。停止するには Ctrl-C を押してください。")
    recorder = Recorder(cfg, duration=duration_sec)
    sys.exit(recorder.run())


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(),
    help="診断対象の DB ファイル (既定: config の db_path)。",
)
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(),
    help="設定ファイルのパス。",
)
@click.option("--only", default=None, help="カンマ区切りのルール ID のみ実行。")
@click.option("--skip", default=None, help="カンマ区切りのルール ID をスキップ。")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "html"]),
    default="text",
    help="出力フォーマット。",
)
@click.option(
    "--output", type=click.Path(), default=None, help="レポートをファイルに書き出す。"
)
@click.option("--verbose", is_flag=True, help="推論過程を表示する。")
@click.option("--no-color", is_flag=True, help="ANSI カラーを無効化する。")
@click.option(
    "--exit-zero-on-info",
    is_flag=True,
    help="info のみの Finding のとき終了コード 0 を返す (既定は 2)。CI 向け。",
)
def check(
    db_path: Optional[str],
    config_path: Optional[str],
    only: Optional[str],
    skip: Optional[str],
    fmt: str,
    output: Optional[str],
    verbose: bool,
    no_color: bool,
    exit_zero_on_info: bool,
) -> None:
    """記録済みデータに診断ルールを実行し、改善提案を出力します。

    終了コード: 0=問題なし / 1=critical・warning あり / 2=info のみ
    """
    from .diagnosis.engine import Engine  # T06 — deferred
    from . import report as report_mod  # T07 — deferred

    from .config import load_config
    from .storage import Storage

    cfg = load_config(Path(config_path) if config_path else None)
    resolved_db = db_path or cfg.db_path

    if not Path(resolved_db).exists():
        click.echo(
            "エラー: DBファイルが見つかりません。先に procdiag record を実行してください。",
            err=True,
        )
        sys.exit(1)

    only_list = [s.strip() for s in only.split(",")] if only else None
    skip_list = [s.strip() for s in skip.split(",")] if skip else None

    charts = None
    meta_hostname: Optional[str] = None
    with Storage(resolved_db, read_only=True) as store:
        engine = Engine(cfg, verbose=verbose)
        diag = engine.run(store, only=only_list, skip=skip_list)
        if fmt == "html":
            # Build charts while the DB connection is still open — doing this
            # after the `with` block would read a closed connection and yield
            # empty charts (the report command builds them in-block too).
            charts = report_mod.build_charts(store)  # type: ignore[attr-defined]
            # Also read while the connection is open; only the html renderer
            # uses it (stays None for json/text, which never call render_html).
            meta_hostname = store.meta().get("hostname")

    if fmt == "json":
        text = report_mod.render_json(diag)
    elif fmt == "html":
        text = report_mod.render_html(
            diag,
            charts,
            hostname_label=cfg.report.hostname_label,
            meta_hostname=meta_hostname,
        )
    else:
        color = sys.stdout.isatty() and not no_color
        text = report_mod.render_terminal(diag, color=color, verbose=verbose)

    if output:
        Path(output).write_text(text, encoding="utf-8")
        click.echo(f"レポートを書き出しました: {output}")
    else:
        click.echo(text)

    # Exit codes: 0=healthy / 1=critical or warning / 2=info-only findings.
    # --exit-zero-on-info collapses the info-only case to 0 for CI pipelines.
    has_warn_or_critical = any(
        f.severity.value in ("warning", "critical") for f in diag.findings
    )
    if has_warn_or_critical:
        sys.exit(1)
    if diag.findings and not exit_zero_on_info:
        sys.exit(2)
    sys.exit(0)


# Register ``advise`` as a hidden alias for ``check`` (design doc §3).
cli.add_command(check, name="advise")
# Mark the alias as hidden so it doesn't appear in --help.
_advise_cmd = cli.commands["advise"]
_advise_cmd.hidden = True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--db", "db_path", default=None, type=click.Path(), help="診断対象の DB ファイル。"
)
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(),
    help="設定ファイルのパス。",
)
@click.option(
    "--out",
    "html_out",
    default="report.html",
    type=click.Path(),
    help="HTML レポートの出力先 (既定: report.html)。",
)
@click.option(
    "--json",
    "json_out",
    default=None,
    type=click.Path(),
    help="JSON レポートの出力先 (省略時はスキップ)。",
)
def report(
    db_path: Optional[str],
    config_path: Optional[str],
    html_out: str,
    json_out: Optional[str],
) -> None:
    """HTML + JSON レポートを一括生成します (check のラッパーコマンド)。"""
    from .diagnosis.engine import Engine  # T06 — deferred
    from . import report as report_mod  # T07 — deferred

    from .config import load_config
    from .storage import Storage

    cfg = load_config(Path(config_path) if config_path else None)
    resolved_db = db_path or cfg.db_path

    if not Path(resolved_db).exists():
        click.echo(
            "エラー: DBファイルが見つかりません。先に procdiag record を実行してください。",
            err=True,
        )
        sys.exit(1)

    with Storage(resolved_db, read_only=True) as store:
        engine = Engine(cfg)
        diag = engine.run(store)
        charts = report_mod.build_charts(store)  # type: ignore[attr-defined]
        meta_hostname = store.meta().get("hostname")

    html_text = report_mod.render_html(
        diag,
        charts,
        hostname_label=cfg.report.hostname_label,
        meta_hostname=meta_hostname,
    )
    Path(html_out).write_text(html_text, encoding="utf-8")
    click.echo(f"HTML レポートを書き出しました: {html_out}")

    if json_out:
        json_text = report_mod.render_json(diag)
        Path(json_out).write_text(json_text, encoding="utf-8")
        click.echo(f"JSON レポートを書き出しました: {json_out}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--db", "db_path", default=None, type=click.Path(), help="対象の DB ファイル。"
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="出力フォーマット。",
)
def status(db_path: Optional[str], fmt: str) -> None:
    """記録済み DB の概況 (期間、サンプル数、自己負荷) を表示します。"""
    from .config import load_config
    from .storage import Storage
    from .core.util import humanize_bytes, humanize_duration

    cfg = load_config()
    resolved_db = db_path or cfg.db_path

    if not Path(resolved_db).exists():
        click.echo(
            "エラー: DBファイルが見つかりません。先に procdiag record を実行してください。",
            err=True,
        )
        sys.exit(1)

    with Storage(resolved_db, read_only=True) as store:
        counts = store.counts()
        meta_dict = store.meta()

        # Self-CPU stats from sys_samples
        cpu_rows = store.series("sys_samples", "self_cpu")
        self_cpu_avg: Optional[float] = None
        self_cpu_max: Optional[float] = None
        if cpu_rows:
            cpu_vals = [v for _, v in cpu_rows if v is not None]
            if cpu_vals:
                self_cpu_avg = sum(cpu_vals) / len(cpu_vals)
                self_cpu_max = max(cpu_vals)

        # Time span from sys_samples
        ts_rows = store.rows("sys_samples")
        span_str = "—"
        start_str = "—"
        end_str = "—"
        if ts_rows:
            ts_vals = [r["ts"] for r in ts_rows if r["ts"] is not None]
            if ts_vals:
                span_sec = max(ts_vals) - min(ts_vals)
                span_str = humanize_duration(span_sec)
                start_str = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(min(ts_vals))
                )
                end_str = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(max(ts_vals))
                )

        # Recent ops_events (last 5)
        event_rows = store.rows("ops_events")
        recent_events = list(event_rows)[-5:]

    if fmt == "json":
        payload = {
            "meta": meta_dict,
            "counts": counts,
            "span": span_str,
            "start": start_str,
            "end": end_str,
            "self_cpu_avg": self_cpu_avg,
            "self_cpu_max": self_cpu_max,
            "recent_events": [
                {"ts": r["ts"], "kind": r["kind"], "detail": r["detail"]}
                for r in recent_events
            ],
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    click.echo(f"DB: {resolved_db}  ({humanize_bytes(counts.get('db_bytes'))})")
    click.echo(f"期間: {start_str} 〜 {end_str}  ({span_str})")
    click.echo(
        f"サンプル数: sys={counts.get('sys_samples', 0)},  "
        f"proc={counts.get('proc_samples', 0)},  "
        f"cg={counts.get('cg_samples', 0)}"
    )
    click.echo(f"ロールアップ: sys_rollup_1m={counts.get('sys_rollup_1m', 0)}")
    if self_cpu_avg is not None:
        click.echo(f"自己負荷: avg={self_cpu_avg:.2f}%  max={self_cpu_max:.2f}%")
    else:
        click.echo("自己負荷: —")

    if recent_events:
        click.echo("\n直近イベント (最大5件):")
        for ev in recent_events:
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev["ts"]))
            click.echo(f"  [{ts_str}] {ev['kind']}  {ev['detail'] or ''}")


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("name", required=False)
def explain(name: Optional[str]) -> None:
    """ルールまたはサンプラーの検出内容と手動確認コマンドを表示します。

    引数なし: 登録済みのルールとサンプラーを一覧表示します。
    引数あり: ルール ID (大文字) またはサンプラー名 (小文字) の詳細を表示します。
    """
    from .core.rule import all_rules, get_rule
    from .core.sampler import all_samplers, get_sampler

    # Try to load diagnosis module (may not be implemented yet in P2).
    with suppress(ImportError):
        importlib.import_module("procdiag.diagnosis")

    # Try to load built-in samplers.
    for mod_name in (
        "procdiag.samplers.system",
        "procdiag.samplers.cgroup",
        "procdiag.samplers.process",
        "procdiag.samplers.disk",
    ):
        with suppress(ImportError):
            importlib.import_module(mod_name)

    if name is None:
        # List all rules and samplers.
        rules = all_rules()
        samplers = all_samplers()

        click.echo("━━━ 登録済みルール (procdiag explain <RULE-ID> で詳細) ━━━\n")
        if rules:
            for rid in sorted(rules):
                cls = rules[rid]
                what = getattr(cls, "what", "") or ""
                click.echo(f"  {rid.ljust(22)} {what[:60]}")
        else:
            click.echo("  (ルールモジュール未導入)")

        click.echo("\n━━━ 登録済みサンプラー (procdiag explain <name> で詳細) ━━━\n")
        if samplers:
            for sname in sorted(
                samplers, key=lambda n: (getattr(samplers[n], "tier", ""), n)
            ):
                cls = samplers[sname]
                tier = getattr(cls, "tier", "?")
                what = getattr(cls, "what", "") or ""
                click.echo(f"  {sname.ljust(14)} [{tier.ljust(3)}]  {what[:50]}")
        else:
            click.echo("  (サンプラーモジュール未導入)")
        return

    # Look up by rule_id (uppercase convention) or sampler name (lowercase).
    rule_cls = get_rule(name)
    sampler_cls = get_sampler(name)

    if rule_cls is None and sampler_cls is None:
        click.echo(f"不明な名前です: {name}", err=True)
        sys.exit(1)

    if rule_cls is not None:
        _print_rule_detail(name, rule_cls)
    else:
        _print_sampler_detail(name, sampler_cls)


def _print_rule_detail(rule_id: str, cls: type) -> None:
    """Print detail for a single rule class."""
    title = getattr(cls, "title", "")
    category = getattr(cls, "category", "")
    what = getattr(cls, "what", "") or ""
    why = getattr(cls, "why", "") or ""
    cmds = getattr(cls, "manual_commands", []) or []

    click.echo(f"# {rule_id} — {title}  (カテゴリ: {category})\n")
    click.echo(f"検出内容:  {what}")
    click.echo(f"理由:      {why}")
    if cmds:
        click.echo("\n手動確認コマンド:")
        for cmd in cmds:
            click.echo(f"  $ {cmd}")


def _print_sampler_detail(sampler_name: str, cls: type) -> None:
    """Print detail for a single sampler class."""
    title = getattr(cls, "title", "")
    tier = getattr(cls, "tier", "")
    interval_key = getattr(cls, "interval_key", "")
    what = getattr(cls, "what", "") or ""
    why = getattr(cls, "why", "") or ""
    cmds = getattr(cls, "manual_commands", []) or []

    click.echo(f"# {sampler_name} — {title}  (Tier: {tier}, 間隔: {interval_key})\n")
    click.echo(f"計測内容:  {what}")
    click.echo(f"理由:      {why}")
    if cmds:
        click.echo("\n手動確認コマンド:")
        for cmd in cmds:
            click.echo(f"  $ {cmd}")


# ---------------------------------------------------------------------------
# list-rules
# ---------------------------------------------------------------------------


@cli.command(name="list-rules")
def list_rules() -> None:
    """登録済みルールを rule_id / category / title の一覧で表示します。"""
    from .core.rule import all_rules

    with suppress(ImportError):
        importlib.import_module("procdiag.diagnosis")

    rules = all_rules()
    if not rules:
        click.echo("(ルールモジュール未導入)")
        return

    # Group by category.
    by_cat: dict[str, list[str]] = {}
    for rid, cls in rules.items():
        cat = getattr(cls, "category", "other") or "other"
        by_cat.setdefault(cat, []).append(rid)

    for cat in sorted(by_cat):
        click.echo(f"\n[{cat}]")
        for rid in sorted(by_cat[cat]):
            cls = rules[rid]
            title = getattr(cls, "title", "") or ""
            click.echo(f"  {rid.ljust(22)} {title}")


# ---------------------------------------------------------------------------
# list-samplers
# ---------------------------------------------------------------------------


@cli.command(name="list-samplers")
def list_samplers() -> None:
    """登録済みサンプラーを Tier ごとに一覧表示します。"""
    from .core.sampler import all_samplers

    for mod_name in (
        "procdiag.samplers.system",
        "procdiag.samplers.cgroup",
        "procdiag.samplers.process",
        "procdiag.samplers.disk",
    ):
        with suppress(ImportError):
            importlib.import_module(mod_name)

    samplers = all_samplers()
    if not samplers:
        click.echo("(サンプラーモジュール未導入)")
        return

    by_tier: dict[str, list[str]] = {}
    for sname, cls in samplers.items():
        tier = getattr(cls, "tier", "?") or "?"
        by_tier.setdefault(tier, []).append(sname)

    for tier in sorted(by_tier):
        click.echo(f"\n[{tier}]")
        for sname in sorted(by_tier[tier]):
            cls = samplers[sname]
            title = getattr(cls, "title", "") or ""
            interval_key = getattr(cls, "interval_key", "") or ""
            click.echo(f"  {sname.ljust(14)} {title.ljust(20)} ({interval_key})")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--db", "db_path", default=None, type=click.Path(), help="対象の DB ファイル。"
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["csv", "json"]),
    default="csv",
    help="出力フォーマット (既定: csv)。",
)
@click.option(
    "--rollup",
    "rollup",
    type=click.Choice(["1min", "raw"]),
    default="1min",
    help="1min=sys_rollup_1m  raw=sys_samples (既定: 1min)。",
)
@click.option(
    "--out",
    "out_path",
    default="-",
    type=click.Path(),
    help="出力先ファイル (既定: - = stdout)。",
)
def export(
    db_path: Optional[str],
    fmt: str,
    rollup: str,
    out_path: str,
) -> None:
    """記録データを CSV または JSON でエクスポートします。"""
    from .config import load_config
    from .export import export_rows
    from .storage import Storage

    cfg = load_config()
    resolved_db = db_path or cfg.db_path

    if not Path(resolved_db).exists():
        click.echo(
            "エラー: DBファイルが見つかりません。先に procdiag record を実行してください。",
            err=True,
        )
        sys.exit(1)

    table = "sys_samples" if rollup == "raw" else "sys_rollup_1m"

    with Storage(resolved_db, read_only=True) as store:
        if out_path == "-":
            count = export_rows(store, table=table, fmt=fmt, out=sys.stdout)
        else:
            with open(out_path, "w", encoding="utf-8", newline="") as fh:
                count = export_rows(store, table=table, fmt=fmt, out=fh)
            click.echo(f"{count} 行を書き出しました: {out_path}", err=True)


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--db", "db_path", default=None, type=click.Path(), help="対象の DB ファイル。"
)
@click.option(
    "--raw",
    "raw_retention",
    default=None,
    help="生サンプルの保持期間 (例: 48h)。省略時は config の値。",
)
@click.option(
    "--rollup",
    "rollup_retention",
    default=None,
    help="ロールアップの保持期間 (例: 90d)。省略時は config の値。",
)
def prune(
    db_path: Optional[str],
    raw_retention: Optional[str],
    rollup_retention: Optional[str],
) -> None:
    """期限切れのサンプルを削除します (保持ポリシーの手動実行)。"""
    from .config import load_config
    from .storage import Storage

    cfg = load_config()
    resolved_db = db_path or cfg.db_path
    raw = raw_retention or cfg.retention.raw
    rollup = rollup_retention or cfg.retention.rollup

    if not Path(resolved_db).exists():
        click.echo(
            "エラー: DBファイルが見つかりません。先に procdiag record を実行してください。",
            err=True,
        )
        sys.exit(1)

    with Storage(resolved_db) as store:
        result = store.retention(raw=raw, rollup=rollup)

    click.echo(
        f"削除完了: 生サンプル {result['raw_deleted']} 件, "
        f"ロールアップ {result['rollup_deleted']} 件"
    )


# ---------------------------------------------------------------------------
# service (subgroup)
# ---------------------------------------------------------------------------


@cli.group()
def service() -> None:
    """systemd ユニットファイルの生成と配置を行います。"""


@service.command("print")
@click.option(
    "--system", is_flag=True, help="system ユニットとして出力 (既定: user ユニット)。"
)
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(),
    help="ユニットに渡す --config パス。",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(),
    help="ユニットに渡す --db パス。",
)
@click.option("--duration", default=None, help="ユニットに渡す --duration 値。")
def service_print(
    system: bool,
    config_path: Optional[str],
    db_path: Optional[str],
    duration: Optional[str],
) -> None:
    """systemd ユニットファイルのテキストを標準出力に表示します。"""
    from .service import print_unit

    click.echo(
        print_unit(
            system=system,
            config_path=config_path,
            db_path=db_path,
            duration=duration,
        )
    )


@service.command("install")
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(),
    help="ユニットに渡す --config パス。",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(),
    help="ユニットに渡す --db パス。",
)
@click.option("--duration", default=None, help="ユニットに渡す --duration 値。")
def service_install(
    config_path: Optional[str],
    db_path: Optional[str],
    duration: Optional[str],
) -> None:
    """~/.config/systemd/user/ に user ユニットを配置します。

    systemctl は実行しません。有効化コマンドを画面に表示します。
    """
    from .service import install_unit, UNIT_NAME

    unit_path = install_unit(
        config_path=config_path,
        db_path=db_path,
        duration=duration,
    )
    click.echo(f"ユニットファイルを書き出しました: {unit_path}\n")
    click.echo("有効化するには以下のコマンドを実行してください:")
    click.echo("  systemctl --user daemon-reload")
    click.echo(f"  systemctl --user enable --now {UNIT_NAME}")
    click.echo("  # ログアウト後も継続する場合: sudo loginctl enable-linger $USER")
    click.echo("\nsystem ユニットとして配置する場合:")
    click.echo(
        f"  procdiag service print --system | "
        f"sudo tee /etc/systemd/system/{UNIT_NAME}"
    )


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(),
    help="設定ファイルのパス (既定: ./config.py)。",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(),
    help="記録先 DB ファイルのパス (config の db_path を上書き)。",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="出力フォーマット (既定: text)。",
)
def doctor(
    config_path: Optional[str],
    db_path: Optional[str],
    fmt: str,
) -> None:
    """記録開始前にホストの観測能力を診断します。

    終了コード: 0=すべて ok/info / 2=degraded あり
    """
    from .config import load_config
    from .doctor import run_doctor

    cfg = load_config(Path(config_path) if config_path else None)
    if db_path:
        cfg = cfg.model_copy(update={"db_path": db_path})

    checks = run_doctor(cfg, config_path=config_path)

    if fmt == "json":
        click.echo(
            json.dumps(
                [
                    {
                        "name": c.name,
                        "label": c.label,
                        "status": c.status,
                        "detail": c.detail,
                    }
                    for c in checks
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _STATUS_SYMBOL = {"ok": "✔", "degraded": "⚠", "info": "ℹ"}
        for c in checks:
            sym = _STATUS_SYMBOL.get(c.status, "?")
            click.echo(f"{sym} [{c.label}] {c.detail}")

        degraded_count = sum(1 for c in checks if c.status == "degraded")
        click.echo("")
        if degraded_count:
            click.echo(
                f"⚠ {degraded_count} 件の降格があります。"
                "記録は可能ですが該当データは欠損します"
            )
        else:
            click.echo("✔ すべての観測能力が利用可能です")

    degraded = any(c.status == "degraded" for c in checks)
    sys.exit(2 if degraded else 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point registered in pyproject.toml as ``procdiag``."""
    cli()


if __name__ == "__main__":
    main()
