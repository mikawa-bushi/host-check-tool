"""``procdiag`` command-line interface.

Conclusion-first host-resource diagnosis: ``procdiag record`` collects
metrics, ``procdiag check`` runs all rules and prints the root cause and
next steps.  ``procdiag quick`` runs snapshot, a short record and check in
one go for a first-time user.  The remaining commands support that core
workflow (status, export, explain, prune) or system integration (service).

Design mirrors netdiag's ``cli.py`` where possible:

- ``click.group`` + ``version_option``
- Thin command handlers: all logic lives in the library modules, imported
  inside each command so ``procdiag --help`` stays fast (no psutil / jinja2 /
  rule-registry import just to print usage).
- ``load_plugins()`` called once at group invocation (same as netdiag).
"""

from __future__ import annotations

import errno
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
    from .storage import Storage

# ---------------------------------------------------------------------------
# Shared command helpers
# ---------------------------------------------------------------------------

# Exit code for usage / precondition errors (missing DB, unreadable config),
# kept distinct from the diagnosis-result codes (0/1/2/3) so automation can
# tell a broken observation pipeline (no DB = record never ran) from an
# unhealthy host.
_EXIT_NO_DATA = 3
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
    # readable=False: an unreadable file reaches _load_config and exits 4,
    # not click's usage error, whose code 2 means "info only" for check.
    return click.option(
        "--config",
        "config_path",
        default=None,
        type=click.Path(readable=False),
        help=help_text,
    )


_since_option = click.option(
    "--since",
    default=None,
    help="診断する期間の始まり (例: '2026-10-28 09:00')。"
    "省略すると生データの残っている範囲を診断する。",
)
_until_option = click.option(
    "--until",
    default=None,
    help="診断する期間の終わり。この時刻ちょうどの記録は含まない。",
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
    """Load the config (``./config.py`` by default) with an optional DB override.

    Exits with code 4 when the config file is missing or broken.
    """
    from .config import load_config

    try:
        cfg = load_config(Path(config_path) if config_path else None)
    except ValueError as exc:
        click.echo(f"エラー: {exc}", err=True)
        sys.exit(_EXIT_USAGE)
    if db_path:
        cfg = cfg.model_copy(update={"db_path": db_path})
    if cfg.rules.overrides or cfg.rules.disabled:
        _validate_rules(cfg)
    return cfg


def _validate_rules(
    cfg: ProcdiagConfig,
    only: Optional[list[str]] = None,
    skip: Optional[list[str]] = None,
) -> None:
    """Exit (code 4) when the rule settings or ``--only`` / ``--skip`` are wrong.

    That is an unknown rule or threshold in ``rules.overrides`` /
    ``rules.disabled`` / *only* / *skip*, or a selection that leaves no rule to
    run.  A typo there would otherwise silently keep a default or change which
    rules run, so it is treated like any other config error: reported at
    start-up.
    """
    from .core import validate_rule_overrides
    from .diagnosis import Engine

    problems: list[str] = []
    try:
        Engine(cfg).select(only=only, skip=skip)
    except ValueError as exc:
        problems = str(exc).splitlines()
    problems += validate_rule_overrides(cfg.rules.overrides)
    if problems:
        for msg in problems:
            click.echo(f"エラー: {msg}", err=True)
        sys.exit(_EXIT_USAGE)


def _ensure_db_and_config(
    config_path: Optional[str], db_path: Optional[str]
) -> tuple[ProcdiagConfig, str]:
    """Load config, resolve the DB path, and exit (code 4) if the DB is absent.

    Centralises the load_config + existence-check shared by check/status/
    export/prune, and gives the DB-missing case its own exit code (4)
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
    """``"A, B,"`` -> ``["A", "B"]`` for the ``--only`` / ``--skip`` options."""
    return [s.strip() for s in value.split(",") if s.strip()] if value else None


def _load_registries() -> None:
    """Import the built-in rule and sampler modules to fill both registries."""
    from . import diagnosis, samplers  # noqa: F401  (imported for registration)


def _parse_period(
    since: Optional[str], until: Optional[str]
) -> tuple[Optional[float], Optional[float]]:
    """``--since`` / ``--until`` as epoch seconds; exit (code 4) when unreadable."""
    from .compare import CompareError, parse_instant

    try:
        start = parse_instant(since) if since else None
        end = parse_instant(until) if until else None
    except CompareError as exc:
        click.echo(f"エラー: {exc}", err=True)
        sys.exit(_EXIT_USAGE)
    if start is not None and end is not None and start >= end:
        click.echo("エラー: --since は --until より前の時刻にしてください", err=True)
        sys.exit(_EXIT_USAGE)
    return start, end


def _local_minute(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _period_notes(store: "Storage", diag: Diagnosis, *, chosen: bool) -> list[str]:
    """Say what the diagnosed period leaves out, for the quality notes."""
    from .core.rule import get_rule
    from .storage import RAW_TABLES

    window = diag.window
    if window is None:
        return []
    notes = []
    recorded = store.system_bounds()
    if not chosen and recorded and recorded[0] < window.start_ts - 60:
        notes.append(
            f"記録は {_local_minute(recorded[0])} からありますが、診断したのは"
            f"生データの残る {_local_minute(window.start_ts)} 以降の"
            f" {window.span_hours:.1f} 時間です。それより前も診断するには --since を"
            "指定してください (期間が長いほど時間とメモリを使います)。"
        )
    raw = store.time_bounds("sys_samples", window.start_ts, window.end_ts)
    raw_hours = (raw[1] - raw[0]) / 3600.0 if raw else 0.0
    raw_rules = [
        rid
        for rid in diag.evaluated_rules
        if (cls := get_rule(rid)) and set(cls.requires_rows) & set(RAW_TABLES)
    ]
    if raw_rules and 0 < raw_hours < window.span_hours - 1 / 60:
        notes.append(
            f"期間 {window.span_hours:.1f} 時間のうち生データがあるのは"
            f" {raw_hours:.1f} 時間です。{', '.join(raw_rules)} は"
            "生データのある範囲だけで評価しました。"
        )
    return notes


def _diagnose(
    cfg: ProcdiagConfig,
    db: str,
    *,
    only: Optional[list[str]] = None,
    skip: Optional[list[str]] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    verbose: bool = False,
    with_charts: bool = False,
) -> tuple[Diagnosis, Optional[ChartData], Optional[str]]:
    """Run the rules on *db*; return ``(diagnosis, charts, meta_hostname)``.

    Without *since* / *until* the engine picks the period (the raw samples).
    With either, the rules run over the records in ``[since, until)`` and the
    command exits with code 3 when there are none.  Charts and the recorded
    hostname are only needed for HTML output, so they are read (inside the
    open connection) only when *with_charts* is set and are ``None`` otherwise.
    """
    from .core import Window
    from .diagnosis import Engine
    from .report import build_charts
    from .storage import Storage

    charts = None
    meta_hostname = None
    chosen = since is not None or until is not None
    with Storage(db, read_only=True) as store:
        window = None
        if chosen:
            bounds = store.system_bounds(since, before=until)
            if bounds is None:
                recorded = store.system_bounds()
                where = (
                    f"記録があるのは {_local_minute(recorded[0])} 〜"
                    f" {_local_minute(recorded[1])} です。"
                    if recorded
                    else "DB に記録がありません。"
                )
                click.echo(f"エラー: 指定した期間に記録がありません。{where}", err=True)
                sys.exit(_EXIT_NO_DATA)
            window = Window(*bounds)
        diag = Engine(cfg, verbose=verbose).run(
            store, only=only, skip=skip, window=window
        )
        diag.quality.notes.extend(_period_notes(store, diag, chosen=chosen))
        if with_charts:
            charts = build_charts(
                store,
                since=window.start_ts if window else None,
                until=window.end_ts if window else None,
                config=cfg,
            )
            meta_hostname = store.meta().get("hostname")
    return diag, charts, meta_hostname


# ``check --output`` suffix -> format, used when ``--format`` is not given, so
# ``-o report.html`` writes HTML instead of terminal text in an .html file.
_FORMAT_BY_SUFFIX = {".html": "html", ".htm": "html", ".json": "json"}


def _render_html(
    cfg: ProcdiagConfig,
    diag: Diagnosis,
    charts: Optional[ChartData],
    meta_hostname: Optional[str],
) -> str:
    """The self-contained HTML report, as ``check`` and ``quick`` write it."""
    from .report import render_html

    return render_html(
        diag,
        charts,
        hostname_label=cfg.report.hostname_label,
        meta_hostname=meta_hostname,
    )


def _host_label(cfg: ProcdiagConfig) -> str:
    """Host name shown by ``snapshot``: ``report.hostname_label`` or the real one."""
    import socket

    return cfg.report.hostname_label or socket.gethostname()


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
        return _EXIT_NO_DATA
    return 0


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


# Removed command name -> what to run instead.  Kept so an old script or
# muscle memory gets a pointer instead of click's bare "No such command".
_REMOVED_COMMANDS = {
    "report": "procdiag check --output report.html",
    "list-rules": "procdiag explain",
    "list-samplers": "procdiag explain",
    "advise": "procdiag check",
}


class _ProcdiagGroup(click.Group):
    def get_command(self, ctx: click.Context, cmd_name: str) -> Optional[click.Command]:
        replacement = _REMOVED_COMMANDS.get(cmd_name)
        if replacement is not None:
            # Exit 4, not click's usage code 2: an old CI job calling a removed
            # name must not read as check's "info only" (2) and pass.
            err = click.UsageError(
                f"procdiag {cmd_name} は削除されました。"
                f"代わりに {replacement} を使ってください。",
                ctx,
            )
            err.exit_code = _EXIT_USAGE
            raise err
        return super().get_command(ctx, cmd_name)


@click.group(cls=_ProcdiagGroup)
@click.version_option(version=__version__, prog_name="procdiag")
def cli() -> None:
    """procdiag — local-first Linux ホスト資源診断ツール。

    初めての方は procdiag quick を実行してください。いまの状態の確認・1 時間の記録・診断を 1 回で行います。
    """
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
    from . import snapshot as snapshot_mod
    from .core.util import should_use_color

    cfg = _load_config(config_path)
    items = snapshot_mod.run_snapshot(cfg)
    hostname = _host_label(cfg)
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
@_since_option
@_until_option
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
    since: Optional[str],
    until: Optional[str],
    fmt: Optional[str],
    output: Optional[str],
    verbose: bool,
    no_color: bool,
    exit_zero_on_info: bool,
    quiet: bool,
) -> None:
    """記録済みデータに診断ルールを実行し、改善提案を出力します。

    既定では生データの残っている期間を診断します。--since / --until で期間を
    選べます (時刻はオフセットなしならこのホストのローカルタイム)。

    終了コード: 0=問題なし / 1=critical・warning あり / 2=info のみ /
    3=観測データなし（DB にサンプルが1件もない＝記録が回っていない、
    または指定した期間に記録がない）/ 4=前提条件エラー（DB ファイルが無い等）
    """
    from . import report as report_mod
    from .core.util import should_use_color

    start, end = _parse_period(since, until)
    fmt = fmt or _FORMAT_BY_SUFFIX.get(Path(output or "").suffix.lower(), "text")
    cfg, resolved_db = _ensure_db_and_config(config_path, db_path)
    only_ids, skip_ids = _split_ids(only), _split_ids(skip)
    _validate_rules(cfg, only_ids, skip_ids)
    diag, charts, meta_hostname = _diagnose(
        cfg,
        resolved_db,
        only=only_ids,
        skip=skip_ids,
        since=start,
        until=end,
        verbose=verbose,
        with_charts=fmt == "html",
    )

    if fmt == "json":
        text = report_mod.render_json(diag)
    elif fmt == "html":
        text = _render_html(cfg, diag, charts, meta_hostname)
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


# ---------------------------------------------------------------------------
# quick
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--duration",
    default="1h",
    show_default=True,
    help="記録する時間 (例: 30m, 2h)。",
)
@click.option(
    "--out",
    "out_dir",
    default=None,
    # readable=False: an unusable path gets quick's own exit-4 message, not
    # click's usage error, whose code 2 means "info only" for check.
    type=click.Path(readable=False),
    help="記録 DB とレポートを置くディレクトリ (既定: ./procdiag-quick-<日時>)。"
    "空でないディレクトリは指定できません。",
)
@_config_option()
@click.option("--no-color", is_flag=True, help="ANSI カラーを無効化する。")
def quick(
    duration: str, out_dir: Optional[str], config_path: Optional[str], no_color: bool
) -> None:
    """いまの状態の確認・短い記録・診断を 1 回で行います (初めての方向け)。

    snapshot でいまの状態を確認し、--duration の間だけ記録してから check
    と同じルールで診断します。記録中に Ctrl-C
    を押すと記録を早めに終え、そこまでの記録を診断します。記録 DB と HTML
    レポートは --out のディレクトリに置きます。

    終了コード: 0=問題なし / 1=snapshot か診断に critical・warning あり /
    2=info のみ / 3=観測データなし / 4=前提条件エラー
    """
    from . import report as report_mod
    from . import snapshot as snapshot_mod
    from .core.util import humanize_duration, parse_duration, should_use_color
    from .recorder import Recorder

    try:
        seconds = parse_duration(duration)
    except ValueError as exc:
        click.echo(f"エラー: {exc}", err=True)
        sys.exit(_EXIT_USAGE)
    if seconds <= 0:
        click.echo(
            f"エラー: --duration には 0 より長い時間を指定してください: {duration}",
            err=True,
        )
        sys.exit(_EXIT_USAGE)
    cfg = _load_config(config_path)
    out = Path(out_dir or time.strftime("procdiag-quick-%Y%m%d-%H%M%S"))
    try:
        occupied = out.exists() and (not out.is_dir() or any(out.iterdir()))
        if not occupied:
            out.mkdir(parents=True, exist_ok=True)
            if not os.access(out, os.W_OK | os.X_OK):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
    except OSError as exc:
        click.echo(
            f"エラー: 出力先を使えません: {out} ({exc.strerror})\n"
            "書き込めるディレクトリで実行するか、--out で書き込める場所を指定してください。",
            err=True,
        )
        sys.exit(_EXIT_USAGE)
    if occupied:
        click.echo(
            f"エラー: 出力先が空のディレクトリではありません: {out}\n"
            "まだ無いパスか、空のディレクトリを --out に指定してください。",
            err=True,
        )
        sys.exit(_EXIT_USAGE)
    db = out / "procdiag.db"
    color = should_use_color(no_color)

    click.echo("[1/3] いまの状態を確認します (snapshot)\n")
    # The "recording" line would tell the user to run procdiag record, which
    # is what step 2 does next.
    items = [i for i in snapshot_mod.run_snapshot(cfg) if i.name != "recording"]
    click.echo(
        snapshot_mod.render_text(
            items,
            hostname=_host_label(cfg),
            color=color,
            ascii_only=_use_ascii(False),
            next_step="",
        )
    )
    problems = [
        i for i in items if i.status in (snapshot_mod.WARNING, snapshot_mod.CRITICAL)
    ]

    end = time.strftime("%H:%M:%S", time.localtime(time.time() + seconds))
    click.echo(f"\n[2/3] {humanize_duration(seconds)} 記録します ({end} に終わります)")
    click.echo("Ctrl-C で早めに終えても、それまでの記録を診断します。")
    Recorder(cfg.model_copy(update={"db_path": str(db)}), duration=seconds).run()

    diag, charts, meta_hostname = _diagnose(cfg, str(db), with_charts=True)
    # Write the report before printing anything more: when an SSH drop ended
    # the recording (SIGHUP), the terminal is gone and the next echo fails.
    report = out / "report.html"
    try:
        report.write_text(
            _render_html(cfg, diag, charts, meta_hostname), encoding="utf-8"
        )
    except OSError as exc:
        report_error = (
            f"エラー: HTML レポートを書けませんでした: {report} ({exc.strerror})"
        )
    else:
        report_error = ""
    click.echo("\n[3/3] 記録を診断しました (check)\n")
    click.echo(report_mod.render_terminal(diag, color=color).lstrip("\n"))
    if problems:
        click.echo(
            f"\n[1/3] いまの状態の確認で、注意が必要な項目が {len(problems)} 件ありました:"
        )
        marks = {snapshot_mod.CRITICAL: "重大", snapshot_mod.WARNING: "警告"}
        for item in problems:
            click.echo(f"  [{marks[item.status]}] {item.label}: {item.detail}")
            if item.hint:
                click.echo(f"      → {item.hint}")

    if report_error:
        click.echo(f"\n{report_error}", err=True)
    else:
        click.echo(f"\nHTML レポートを書き出しました: {report}")
    click.echo(f"記録 DB: {db}")
    click.echo(
        "メモリリークや慢性的なメモリ不足を見るには、"
        "procdiag record --duration 24h で記録してから procdiag check を実行してください。"
    )
    if report_error:
        sys.exit(_EXIT_USAGE)
    sys.exit(1 if problems else _exit_code(diag))


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
    only_ids, skip_ids = _split_ids(only), _split_ids(skip)
    _validate_rules(cfg, only_ids, skip_ids)
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
            cfg, request, only=only_ids, skip=skip_ids, verbose=verbose
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
