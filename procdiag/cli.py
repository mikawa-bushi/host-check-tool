"""``procdiag`` command-line interface.

Conclusion-first host-resource diagnosis: ``procdiag record`` collects
metrics, ``procdiag check`` (alias ``advise``) runs all rules and prints
the root cause and next steps.  The remaining commands support that core
workflow (status, export, explain, prune) or system integration (service).

Design mirrors netdiag's ``cli.py`` where possible:

- ``click.group`` + ``version_option``
- Thin command handlers: all logic lives in the library modules, imported
  inside each command so ``procdiag --help`` stays fast (no psutil / jinja2 /
  rule-registry import just to print usage).
- ``load_plugins()`` called once at group invocation (same as netdiag).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import click

from . import __version__
from .plugins import load_plugins

if TYPE_CHECKING:
    from .config import ProcdiagConfig
    from .core import Diagnosis
    from .report import ChartData

# ---------------------------------------------------------------------------
# Shared command helpers
# ---------------------------------------------------------------------------

# Exit code for usage / precondition errors (missing DB, unreadable config),
# kept distinct from the diagnosis-result codes (0/1/2/3) so automation can
# tell a broken observation pipeline (no DB = record never ran) from an
# unhealthy host.
_EXIT_USAGE = 4

_DB_MISSING_MSG = (
    "エラー: DB ファイルが見つかりません: {db}\n"
    "先に procdiag record を実行してください。"
)


def _db_option(help_text: str) -> Callable:
    """``--db PATH`` option (overrides ``config.db_path``)."""
    return click.option(
        "--db", "db_path", default=None, type=click.Path(), help=help_text
    )


def _config_option(
    help_text: str = "設定ファイルのパス (既定: ./config.py)。",
) -> Callable:
    """``--config PATH`` option."""
    return click.option(
        "--config", "config_path", default=None, type=click.Path(), help=help_text
    )


_quiet_option = click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="完了メッセージを抑止する (PROCDIAG_QUIET でも可)。",
)


def _load_config(
    config_path: Optional[str], db_path: Optional[str] = None
) -> ProcdiagConfig:
    """Load the config (``./config.py`` by default) with an optional DB override."""
    from .config import load_config

    cfg = load_config(Path(config_path) if config_path else None)
    if db_path:
        cfg = cfg.model_copy(update={"db_path": db_path})
    if cfg.rules.overrides:
        _validate_overrides(cfg)
    return cfg


def _validate_overrides(cfg: ProcdiagConfig) -> None:
    """Exit (code 4) when ``rules.overrides`` names an unknown rule/threshold.

    A typo there would otherwise silently keep the default threshold, so it is
    treated like any other config error: reported at start-up.
    """
    from .core import validate_rule_overrides
    from .plugins import load_extra_rules

    _load_registries()
    load_extra_rules(cfg.rules.extra_rules)
    problems = validate_rule_overrides(cfg.rules.overrides)
    if problems:
        for msg in problems:
            click.echo(f"エラー: {msg}", err=True)
        sys.exit(_EXIT_USAGE)


def _ensure_db_and_config(
    config_path: Optional[str], db_path: Optional[str]
) -> tuple[ProcdiagConfig, str]:
    """Load config, resolve the DB path, and exit (code 4) if the DB is absent.

    Centralises the load_config + existence-check shared by check/report/
    status/export/prune, and gives the DB-missing case its own exit code (4)
    instead of overloading the diagnosis code 1.
    """
    cfg = _load_config(config_path, db_path)
    if not Path(cfg.db_path).exists():
        click.echo(_DB_MISSING_MSG.format(db=cfg.db_path), err=True)
        sys.exit(_EXIT_USAGE)
    return cfg, cfg.db_path


def _is_quiet(flag: bool) -> bool:
    """Whether completion/progress messages should be suppressed.

    True when ``--quiet`` is passed or ``PROCDIAG_QUIET`` is set — so unattended
    systemd/cron runs don't spew per-invocation chatter into journald, and
    ``--format json`` stays machine-clean on stdout.
    """
    return flag or bool(os.environ.get("PROCDIAG_QUIET"))


def _can_show_unicode() -> bool:
    """Best-effort check that stdout can render the ✔/⚠/✘/ℹ status glyphs.

    A POSIX/ascii locale (common on minimal serial consoles / journald pipes)
    cannot, so doctor / snapshot fall back to ASCII labels there.  When the
    encoding is unknown we assume a modern UTF-8 terminal and return True.
    """
    enc = getattr(sys.stdout, "encoding", None)
    if not enc:
        return True
    try:
        "✔⚠✘ℹ".encode(enc)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def _use_ascii(flag: bool) -> bool:
    """ASCII status labels: ``--ascii``, ``NO_COLOR``, or a non-Unicode stdout."""
    return flag or bool(os.environ.get("NO_COLOR")) or not _can_show_unicode()


def _split_ids(value: Optional[str]) -> Optional[list[str]]:
    """``"A, B"`` -> ``["A", "B"]`` for the ``--only`` / ``--skip`` options."""
    return [s.strip() for s in value.split(",")] if value else None


def _load_registries() -> None:
    """Import the built-in rule and sampler modules to fill both registries."""
    from . import diagnosis, samplers  # noqa: F401  (imported for registration)


def _diagnose(
    cfg: ProcdiagConfig,
    db: str,
    *,
    only: Optional[list[str]] = None,
    skip: Optional[list[str]] = None,
    verbose: bool = False,
    with_charts: bool = False,
) -> tuple[Diagnosis, Optional[ChartData], Optional[str]]:
    """Run the rules on *db*; return ``(diagnosis, charts, meta_hostname)``.

    Charts and the recorded hostname are only needed for HTML output, so they
    are read (inside the open connection) only when *with_charts* is set and
    are ``None`` otherwise.
    """
    from .diagnosis import Engine
    from .report import build_charts
    from .storage import Storage

    charts = None
    meta_hostname = None
    with Storage(db, read_only=True) as store:
        diag = Engine(cfg, verbose=verbose).run(store, only=only, skip=skip)
        if with_charts:
            charts = build_charts(store, config=cfg)
            meta_hostname = store.meta().get("hostname")
    return diag, charts, meta_hostname


# ``check --output`` suffix -> format, used when ``--format`` is not given, so
# ``-o report.html`` writes HTML instead of terminal text in an .html file.
_FORMAT_BY_SUFFIX = {".html": "html", ".htm": "html", ".json": "json"}


def _exit_code(diag: Diagnosis, *, exit_zero_on_info: bool = False) -> int:
    """Map a diagnosis to the documented ``check`` exit code.

    0=healthy / 1=critical or warning / 2=info-only findings (0 with
    ``exit_zero_on_info``, for CI) / 3=no observation data at all.
    """
    if any(f.severity.value in ("warning", "critical") for f in diag.findings):
        return 1
    if diag.findings and not exit_zero_on_info:
        return 2
    if diag.no_data and not diag.findings:
        # Empty DB / no observation at all — distinct from a healthy host so
        # callers (CI, wrappers) can tell "nothing recorded" from "all clear".
        return 3
    return 0


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__version__, prog_name="procdiag")
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
        "  2. procdiag doctor --config config.py で観測能力を確認してください\n"
        "  3. procdiag record --config config.py で記録を開始してください"
    )


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


@cli.command()
@_config_option()
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="出力フォーマット (既定: text)。",
)
@click.option("--no-color", is_flag=True, help="ANSI カラーを無効化する。")
@click.option(
    "--ascii",
    "ascii_",
    is_flag=True,
    help="記号を ASCII ([OK]/[WARN]/[CRIT]/[INFO]) で出力する。",
)
def snapshot(
    config_path: Optional[str], fmt: str, no_color: bool, ascii_: bool
) -> None:
    """記録なしで、いまのホストの状態をその場で網羅チェックします (数秒)。

    CPU・メモリ・I/O の圧迫、ディスク容量・inode・読み取り専用化、ファイルハンドル、
    ゾンビ・上位プロセス、failed / 自動再起動したサービス、直近24時間のカーネル異常、
    温度、時刻同期、記録の稼働状況を確認します。

    終了コード: 0=警告なし / 1=warning・critical あり
    """
    import socket

    from . import snapshot as snapshot_mod
    from .core.util import should_use_color

    cfg = _load_config(config_path)
    items = snapshot_mod.run_snapshot(cfg)
    hostname = cfg.report.hostname_label or socket.gethostname()
    if fmt == "json":
        click.echo(snapshot_mod.render_json(items, hostname=hostname))
    else:
        click.echo(
            snapshot_mod.render_text(
                items,
                hostname=hostname,
                color=should_use_color(no_color),
                ascii_only=_use_ascii(ascii_),
            )
        )
    sys.exit(snapshot_mod.exit_code(items))


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--duration", default=None, help="記録時間 (例: 24h, 60s)。省略時は無期限。"
)
@_config_option()
@_db_option("記録先 DB ファイルのパス (config の db_path を上書き)。")
def record(
    duration: Optional[str], config_path: Optional[str], db_path: Optional[str]
) -> None:
    """CPU・メモリ・ディスクを記録します (フォアグラウンド)。Ctrl-C で安全に停止します。"""
    from .core.util import parse_duration
    from .recorder import Recorder

    cfg = _load_config(config_path, db_path)

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
@_db_option("診断対象の DB ファイル (既定: config の db_path)。")
@_config_option("設定ファイルのパス。")
@click.option("--only", default=None, help="カンマ区切りのルール ID のみ実行。")
@click.option("--skip", default=None, help="カンマ区切りのルール ID をスキップ。")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "html"]),
    default=None,
    help="出力フォーマット (既定: --output の拡張子が .html/.htm なら html、"
    ".json なら json、それ以外は text)。",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="レポートをファイルに書き出す (例: -o report.html)。",
)
@click.option("--verbose", is_flag=True, help="推論過程を表示する。")
@click.option("--no-color", is_flag=True, help="ANSI カラーを無効化する。")
@click.option(
    "--exit-zero-on-info",
    is_flag=True,
    help="info のみの Finding のとき終了コード 0 を返す (既定は 2)。CI 向け。",
)
@_quiet_option
def check(
    db_path: Optional[str],
    config_path: Optional[str],
    only: Optional[str],
    skip: Optional[str],
    fmt: Optional[str],
    output: Optional[str],
    verbose: bool,
    no_color: bool,
    exit_zero_on_info: bool,
    quiet: bool,
) -> None:
    """記録済みデータに診断ルールを実行し、改善提案を出力します。

    終了コード: 0=問題なし / 1=critical・warning あり / 2=info のみ /
    3=観測データなし（DB にサンプルが1件もない＝記録が回っていない）/
    4=前提条件エラー（DB ファイルが無い等）
    """
    from . import report as report_mod
    from .core.util import should_use_color

    fmt = fmt or _FORMAT_BY_SUFFIX.get(Path(output or "").suffix.lower(), "text")
    cfg, resolved_db = _ensure_db_and_config(config_path, db_path)
    diag, charts, meta_hostname = _diagnose(
        cfg,
        resolved_db,
        only=_split_ids(only),
        skip=_split_ids(skip),
        verbose=verbose,
        with_charts=fmt == "html",
    )

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
        color = should_use_color(no_color)
        text = report_mod.render_terminal(diag, color=color, verbose=verbose)

    if output:
        Path(output).write_text(text, encoding="utf-8")
        if not _is_quiet(quiet):
            click.echo(f"レポートを書き出しました: {output}")
    else:
        click.echo(text)

    sys.exit(_exit_code(diag, exit_zero_on_info=exit_zero_on_info))


# Register ``advise`` as a hidden alias for ``check`` (design doc §3).
cli.add_command(check, name="advise")
cli.commands["advise"].hidden = True  # type: ignore[attr-defined]


@cli.command()
@click.argument("dbs", nargs=-1, type=click.Path(), metavar="[BEFORE_DB AFTER_DB]")
@_db_option("1 つの DB の 2 期間を比べるときの DB (既定: config の db_path)。")
@_config_option("設定ファイルのパス。しきい値は両方の期間に同じものを使う。")
@click.option(
    "--at",
    default=None,
    help="対策した時刻。この前後で比べる (例: '2026-10-28 09:00')。",
)
@click.option(
    "--before",
    "before_spec",
    default=None,
    help="修正前の期間 START..END (END は含まない。片側は省略可)。",
)
@click.option(
    "--after",
    "after_spec",
    default=None,
    help="修正後の期間 START..END (END は含まない。片側は省略可)。",
)
@click.option("--only", default=None, help="カンマ区切りのルール ID のみ実行。")
@click.option("--skip", default=None, help="カンマ区切りのルール ID をスキップ。")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="出力フォーマット。",
)
@click.option(
    "--output", type=click.Path(), default=None, help="結果をファイルに書き出す。"
)
@click.option("--verbose", is_flag=True, help="推論過程と修正後の提案を表示する。")
@click.option("--no-color", is_flag=True, help="ANSI カラーを無効化する。")
@click.option(
    "--exit-zero-on-info",
    is_flag=True,
    help="修正後が info のみのとき終了コード 0 を返す (既定は 2)。",
)
@_quiet_option
def compare(
    dbs: tuple[str, ...],
    db_path: Optional[str],
    config_path: Optional[str],
    at: Optional[str],
    before_spec: Optional[str],
    after_spec: Optional[str],
    only: Optional[str],
    skip: Optional[str],
    fmt: str,
    output: Optional[str],
    verbose: bool,
    no_color: bool,
    exit_zero_on_info: bool,
    quiet: bool,
) -> None:
    """修正前と修正後の 2 期間 (または 2 つの DB) を同じルール・しきい値で比べます。

    時刻はオフセットなしならこのホストのローカルタイムです
    (例: '2026-10-28 09:00'、'2026-10-28T00:00Z')。

    終了コード: 0=修正後に critical・warning なし (修正前の検出はすべて判定できた) /
    1=修正後に critical・warning あり / 2=修正後に info のみ /
    3=判定できない検出がある・期間に記録がない / 4=前提条件エラー
    """
    from . import report as report_mod
    from .compare import CompareError, PeriodsRequest, plan_request, run_compare
    from .compare import exit_code as compare_exit_code
    from .core.util import should_use_color

    if dbs and db_path:
        click.echo("エラー: --db と DB の位置引数は同時に指定できません", err=True)
        sys.exit(_EXIT_USAGE)
    cfg = _load_config(config_path, db_path)
    try:
        request = plan_request(
            dbs=dbs, default_db=cfg.db_path, at=at, before=before_spec, after=after_spec
        )
        paths = (
            (request.before_db, request.after_db)
            if isinstance(request, PeriodsRequest)
            else (request.db,)
        )
        for path in paths:
            if not path.exists():
                click.echo(_DB_MISSING_MSG.format(db=path), err=True)
                sys.exit(_EXIT_USAGE)
        result = run_compare(
            cfg, request, only=_split_ids(only), skip=_split_ids(skip), verbose=verbose
        )
    except CompareError as exc:
        click.echo(f"エラー: {exc}", err=True)
        sys.exit(_EXIT_USAGE)

    if fmt == "json":
        text = report_mod.render_comparison_json(result)
    else:
        text = report_mod.render_comparison_terminal(
            result, color=should_use_color(no_color), verbose=verbose
        )

    if output:
        Path(output).write_text(text, encoding="utf-8")
        if not _is_quiet(quiet):
            click.echo(f"比較結果を書き出しました: {output}")
    else:
        click.echo(text)

    sys.exit(compare_exit_code(result, exit_zero_on_info=exit_zero_on_info))


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@cli.command()
@_db_option("診断対象の DB ファイル。")
@_config_option("設定ファイルのパス。")
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
@_quiet_option
def report(
    db_path: Optional[str],
    config_path: Optional[str],
    html_out: str,
    json_out: Optional[str],
    quiet: bool,
) -> None:
    """HTML + JSON レポートを一括生成します (check のラッパーコマンド)。"""
    from . import report as report_mod

    cfg, resolved_db = _ensure_db_and_config(config_path, db_path)
    diag, charts, meta_hostname = _diagnose(cfg, resolved_db, with_charts=True)

    html_text = report_mod.render_html(
        diag,
        charts,
        hostname_label=cfg.report.hostname_label,
        meta_hostname=meta_hostname,
    )
    Path(html_out).write_text(html_text, encoding="utf-8")
    quiet_mode = _is_quiet(quiet)
    if not quiet_mode:
        click.echo(f"HTML レポートを書き出しました: {html_out}")

    if json_out:
        Path(json_out).write_text(report_mod.render_json(diag), encoding="utf-8")
        if not quiet_mode:
            click.echo(f"JSON レポートを書き出しました: {json_out}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@_db_option("対象の DB ファイル。")
@_config_option()
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="出力フォーマット。",
)
def status(db_path: Optional[str], config_path: Optional[str], fmt: str) -> None:
    """記録済み DB の概況 (期間、サンプル数、自己負荷) を表示します。"""
    from .core.util import humanize_bytes
    from .status import TIME_FMT, collect_status
    from .storage import Storage

    _cfg, resolved_db = _ensure_db_and_config(config_path, db_path)
    with Storage(resolved_db, read_only=True) as store:
        info = collect_status(store)

    if fmt == "json":
        click.echo(json.dumps(info, ensure_ascii=False, indent=2))
        return

    counts = info["counts"]
    click.echo(f"DB: {resolved_db}  ({humanize_bytes(counts.get('db_bytes'))})")
    click.echo(f"期間: {info['start']} 〜 {info['end']}  ({info['span']})")
    click.echo(
        f"サンプル数: sys={counts.get('sys_samples', 0)},  "
        f"proc={counts.get('proc_samples', 0)},  "
        f"cg={counts.get('cg_samples', 0)}"
    )
    click.echo(f"ロールアップ: sys_rollup_1m={counts.get('sys_rollup_1m', 0)}")
    if info["self_cpu_avg"] is not None:
        click.echo(
            f"自己負荷: avg={info['self_cpu_avg']:.2f}%  max={info['self_cpu_max']:.2f}%"
        )
    else:
        click.echo("自己負荷: —")

    if info["recent_events"]:
        click.echo("\n直近イベント (最大5件):")
        for ev in info["recent_events"]:
            ts_str = time.strftime(TIME_FMT, time.localtime(ev["ts"]))
            click.echo(f"  [{ts_str}] {ev['kind']}  {ev['detail'] or ''}")

    if not info["has_data"]:
        click.echo(
            "\n⚠ まだサンプルがありません。procdiag record が稼働しているか、"
            "--db のパスが正しいかを確認してください。",
            err=True,
        )


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------


@cli.command()
@_config_option()
@_db_option("同梱する DB ファイル (既定: config の db_path)。")
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(),
    help="出力先 (既定: procdiag-bundle-<ホスト名>-<日時>.tar.gz)。",
)
@click.option(
    "--keep-cmdline",
    is_flag=True,
    help="DB にプロセスのコマンドラインを残す (既定は秘密情報保護のため削除)。",
)
@_quiet_option
def bundle(
    config_path: Optional[str],
    db_path: Optional[str],
    out_path: Optional[str],
    keep_cmdline: bool,
    quiet: bool,
) -> None:
    """記録 DB・レポート・現在の状態を 1 つの tar.gz にまとめます (USB 持ち出し用)。

    記録中でも実行できます。DB が無い場合も、その場の状態 (snapshot) と
    観測能力 (doctor) だけを収録したバンドルを作成します。
    """
    from .bundle import create_bundle
    from .core.util import humanize_bytes

    cfg = _load_config(config_path, db_path)
    result = create_bundle(
        cfg,
        config_path=config_path,
        out=Path(out_path) if out_path else None,
        keep_cmdline=keep_cmdline,
    )
    if not _is_quiet(quiet):
        size = humanize_bytes(result.path.stat().st_size)
        click.echo(f"バンドルを作成しました: {result.path} ({size})")
        click.echo(f"  収録: {', '.join(result.files)}")
        if not result.has_db:
            click.echo(
                f"  ※ 記録 DB ({cfg.db_path}) が無いため、傾向の診断は含まれません"
            )
        if result.cmdline_redacted:
            click.echo(
                "  ※ DB からプロセスのコマンドラインを削除しました (--keep-cmdline で保持)"
            )


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

    _load_registries()

    if name is None:
        rules = all_rules()
        click.echo("━━━ 登録済みルール (procdiag explain <RULE-ID> で詳細) ━━━")
        for category, rule_ids in _group_by(rules, "category").items():
            click.echo(f"\n[{category}]")
            for rid in rule_ids:
                click.echo(f"  {rid.ljust(22)} {rules[rid].title}")

        samplers = all_samplers()
        click.echo("\n━━━ 登録済みサンプラー (procdiag explain <name> で詳細) ━━━")
        for tier, names in _group_by(samplers, "tier").items():
            click.echo(f"\n[{tier}]")
            for sname in names:
                cls = samplers[sname]
                click.echo(
                    f"  {sname.ljust(14)} {cls.interval_key.ljust(20)} {cls.title}"
                )
        return

    # Look up by rule_id (uppercase convention) or sampler name (lowercase).
    rule_cls = get_rule(name)
    sampler_cls = get_sampler(name)

    if rule_cls is not None:
        category = getattr(rule_cls, "category", "")
        heading = f"# {name} — {getattr(rule_cls, 'title', '')}  (カテゴリ: {category})"
        _print_detail(heading, rule_cls, what_label="検出内容")
    elif sampler_cls is not None:
        tier = getattr(sampler_cls, "tier", "")
        interval_key = getattr(sampler_cls, "interval_key", "")
        heading = (
            f"# {name} — {getattr(sampler_cls, 'title', '')}"
            f"  (Tier: {tier}, 間隔: {interval_key})"
        )
        _print_detail(heading, sampler_cls, what_label="計測内容")
    else:
        click.echo(f"不明な名前です: {name}", err=True)
        sys.exit(1)


def _group_by(registry: dict[str, type], attr: str) -> dict[str, list[str]]:
    """Registry names grouped by a class attribute (``category`` / ``tier``), sorted."""
    groups: dict[str, list[str]] = {}
    for name in sorted(registry):
        groups.setdefault(getattr(registry[name], attr) or "?", []).append(name)
    return dict(sorted(groups.items()))


def _print_detail(heading: str, cls: type, *, what_label: str) -> None:
    """Print a rule's / sampler's what, why and manual check commands."""
    click.echo(f"{heading}\n")
    click.echo(f"{what_label}:  {getattr(cls, 'what', '') or ''}")
    click.echo(f"理由:      {getattr(cls, 'why', '') or ''}")
    cmds = getattr(cls, "manual_commands", []) or []
    if cmds:
        click.echo("\n手動確認コマンド:")
        for cmd in cmds:
            click.echo(f"  $ {cmd}")
    tunables = cls.tunables() if hasattr(cls, "tunables") else {}
    if tunables:
        click.echo(
            "\n調整可能なしきい値 (config.py の rules.overrides で上書き、括弧内は既定値):"
        )
        for name, default in tunables.items():
            click.echo(f"  {name} ({default})")


# ---------------------------------------------------------------------------
# list-rules / list-samplers
# ---------------------------------------------------------------------------


@cli.command(name="list-rules")
def list_rules() -> None:
    """登録済みルールを rule_id / category / title の一覧で表示します。"""
    from .core.rule import all_rules

    _load_registries()
    rules = all_rules()

    by_cat: dict[str, list[str]] = {}
    for rid, cls in rules.items():
        cat = getattr(cls, "category", "other") or "other"
        by_cat.setdefault(cat, []).append(rid)

    for cat in sorted(by_cat):
        click.echo(f"\n[{cat}]")
        for rid in sorted(by_cat[cat]):
            title = getattr(rules[rid], "title", "") or ""
            click.echo(f"  {rid.ljust(22)} {title}")


@cli.command(name="list-samplers")
def list_samplers() -> None:
    """登録済みサンプラーを Tier ごとに一覧表示します。"""
    from .core.sampler import all_samplers

    _load_registries()
    samplers = all_samplers()

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
@_db_option("対象の DB ファイル。")
@_config_option()
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
@_quiet_option
def export(
    db_path: Optional[str],
    config_path: Optional[str],
    fmt: str,
    rollup: str,
    out_path: str,
    quiet: bool,
) -> None:
    """記録データを CSV または JSON でエクスポートします。"""
    from .export import export_rows
    from .storage import Storage

    _cfg, resolved_db = _ensure_db_and_config(config_path, db_path)
    table = "sys_samples" if rollup == "raw" else "sys_rollup_1m"

    with Storage(resolved_db, read_only=True) as store:
        if out_path == "-":
            export_rows(store, table=table, fmt=fmt, out=sys.stdout)
        else:
            with open(out_path, "w", encoding="utf-8", newline="") as fh:
                count = export_rows(store, table=table, fmt=fmt, out=fh)
            if not _is_quiet(quiet):
                click.echo(f"{count} 行を書き出しました: {out_path}", err=True)


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


@cli.command()
@_db_option("対象の DB ファイル。")
@_config_option()
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
@_quiet_option
def prune(
    db_path: Optional[str],
    config_path: Optional[str],
    raw_retention: Optional[str],
    rollup_retention: Optional[str],
    quiet: bool,
) -> None:
    """期限切れのサンプルを削除します (保持ポリシーの手動実行)。"""
    from .storage import Storage

    cfg, resolved_db = _ensure_db_and_config(config_path, db_path)
    raw = raw_retention or cfg.retention.raw
    rollup = rollup_retention or cfg.retention.rollup

    with Storage(resolved_db) as store:
        result = store.retention(raw=raw, rollup=rollup)

    if not _is_quiet(quiet):
        click.echo(
            f"削除完了: 生サンプル {result['raw_deleted']} 件, "
            f"ロールアップ {result['rollup_deleted']} 件"
        )


# ---------------------------------------------------------------------------
# service (subgroup)
# ---------------------------------------------------------------------------

_unit_config_option = _config_option("ユニットに渡す --config パス。")
_unit_db_option = _db_option("ユニットに渡す --db パス。")
_unit_duration_option = click.option(
    "--duration", default=None, help="ユニットに渡す --duration 値。"
)


@cli.group()
def service() -> None:
    """systemd ユニットファイルの生成と配置を行います。"""


@service.command("print")
@click.option(
    "--system", is_flag=True, help="system ユニットとして出力 (既定: user ユニット)。"
)
@_unit_config_option
@_unit_db_option
@_unit_duration_option
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
@_unit_config_option
@_unit_db_option
@_unit_duration_option
def service_install(
    config_path: Optional[str],
    db_path: Optional[str],
    duration: Optional[str],
) -> None:
    """~/.config/systemd/user/ に user ユニットを配置します。

    systemctl は実行しません。有効化コマンドを画面に表示します。
    """
    from .service import UNIT_NAME, install_unit

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

_DOCTOR_SYMBOLS = {
    # ascii?: {status: symbol}
    False: {"ok": "✔", "degraded": "⚠", "info": "ℹ"},
    True: {"ok": "[OK]", "degraded": "[WARN]", "info": "[INFO]"},
}


@cli.command()
@_config_option()
@_db_option("記録先 DB ファイルのパス (config の db_path を上書き)。")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="出力フォーマット (既定: text)。",
)
@click.option(
    "--ascii",
    "ascii_",
    is_flag=True,
    help="記号を ASCII ([OK]/[WARN]/[INFO]) で出力する (Unicode 非対応端末向け)。",
)
def doctor(
    config_path: Optional[str],
    db_path: Optional[str],
    fmt: str,
    ascii_: bool,
) -> None:
    """記録開始前にホストの観測能力を診断します。

    終了コード: 0=すべて ok/info / 2=degraded あり
    """
    from .doctor import run_doctor

    cfg = _load_config(config_path, db_path)
    checks = run_doctor(cfg, config_path=config_path)
    degraded_count = sum(1 for c in checks if c.status == "degraded")

    if fmt == "json":
        click.echo(
            json.dumps([c.to_dict() for c in checks], ensure_ascii=False, indent=2)
        )
    else:
        symbols = _DOCTOR_SYMBOLS[_use_ascii(ascii_)]
        for c in checks:
            click.echo(f"{symbols.get(c.status, '?')} [{c.label}] {c.detail}")

        click.echo("")
        if degraded_count:
            click.echo(
                f"{symbols['degraded']} {degraded_count} 件の降格があります。"
                "記録は可能ですが該当データは欠損します"
            )
        else:
            click.echo(f"{symbols['ok']} すべての観測能力が利用可能です")

    sys.exit(2 if degraded_count else 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point registered in pyproject.toml as ``procdiag``."""
    cli()


if __name__ == "__main__":
    main()
