"""Render a Diagnosis for humans (terminal), machines (JSON), or archive (HTML+SVG).

Conclusion-first layout mirrors netdiag/report.py: the headline is the root
cause and the next step; full evidence and proposal detail follow for readers
who want it.  The HTML output is completely self-contained — inline CSS, server-
side SVG charts, no CDN references — so it works in fully offline/air-gapped
factory environments.

Public API (called by cli.py):
    render_terminal(diag, *, color=True, verbose=False) -> str
    render_json(diag) -> str
    render_html(diag, charts=None, *, hostname_label=None, meta_hostname=None) -> str
    build_charts(store, *, since=None) -> ChartData
    render_comparison_terminal(cmp, *, color=True, verbose=False) -> str
    render_comparison_json(cmp) -> str

ChartData is a dataclass whose fields hold (ts, value) series for the main
system metrics, plus a ``spikes`` list for the spike-event section in HTML.
"""

from __future__ import annotations

import html as _html_lib
import json
import logging
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Tuple

import procdiag
from procdiag.compare import (
    COMPARE_FORMAT,
    Clear,
    Comparison,
    ComparisonRow,
    Detected,
    Side,
    SideName,
    SideState,
    Unknown,
    Verdict,
)
from procdiag.compare import exit_code as compare_exit_code
from procdiag.core import (
    Confidence,
    Diagnosis,
    Headline,
    ObservationQuality,
    Severity,
    SkippedRule,
    configure_rule,
)
from procdiag.diagnosis.cpu import CpuSaturationRule, CpuSinglecoreRule
from procdiag.spikes import culprits
from procdiag.storage import Storage

if TYPE_CHECKING:
    from procdiag.config import ProcdiagConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ANSI colour helpers — matches netdiag palette
# ---------------------------------------------------------------------------

_ANSI: dict[Severity, str] = {
    Severity.CRITICAL: "\033[31m",
    Severity.WARNING: "\033[33m",
    Severity.INFO: "\033[36m",
}
_RESET = "\033[0m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"


def _c(code: str, color: bool) -> str:
    """Return the ANSI code only when colour output is requested."""
    return code if color else ""


def _confidence_ja(conf: "Confidence | str") -> str:
    """Japanese label (高/中/低) for a Confidence enum or its string value."""
    value = getattr(conf, "value", conf)
    return {"high": "高", "medium": "中", "low": "低"}.get(value, value)


# ---------------------------------------------------------------------------
# ChartData — carries pre-built series for render_html / build_charts
# ---------------------------------------------------------------------------


@dataclass
class ChartData:
    """Holds time-series data for the HTML chart section.

    Each series is a list of ``(ts_epoch_float, value_float)`` pairs sorted
    by timestamp.  Missing data is represented by the series simply being
    empty — callers render a 'データなし' placeholder instead.

    ``psi`` holds three sub-series (cpu_some10, mem_some10, io_some10) in a
    single sequence of ``(ts, cpu, mem, io)`` tuples so the chart can draw
    them in one SVG element with shared x-axis.

    ``mounts`` is a dict mapping mountpoint -> series so each partition gets
    its own chart row.

    ``spikes`` is a list of plain dicts (ts, trigger_type, psi_snapshot,
    top_processes) used for the spike-event table in the HTML output.
    """

    cpu: List[Tuple[float, float]] = field(default_factory=list)
    cpu_max_core: List[Tuple[float, float]] = field(default_factory=list)
    mem_used: List[Tuple[float, float]] = field(default_factory=list)
    # PSI: list of (ts, cpu_some10, mem_some10, io_some10)
    psi: List[Tuple[float, float, float, float]] = field(default_factory=list)
    mounts: dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    spikes: List[dict] = field(default_factory=list)
    # Decision boundaries drawn as guide lines (the rules' effective values).
    core_pct_threshold: float = CpuSinglecoreRule.core_pct_threshold
    psi_cpu_threshold: float = CpuSaturationRule.psi_threshold


# ---------------------------------------------------------------------------
# spike-event formatting helpers
# ---------------------------------------------------------------------------


def _format_psi_snapshot(raw: object) -> str:
    """Turn a stored PSI-snapshot JSON into a compact human string.

    ``{"cpu_some10": 23.4, "mem_some10": 1.0, "io_some10": 9.2}`` ->
    ``"CPU 23% / MEM 1% / IO 9%"``.  Falls back to the raw text on any parse
    error so information is never silently lost.
    """
    if not raw:
        return ""
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return str(raw)
    if not isinstance(d, dict):
        return str(raw)

    def g(key: str) -> str:
        return _fmt_pct(d.get(key))

    return f"CPU {g('cpu_some10')} / MEM {g('mem_some10')} / IO {g('io_some10')}"


def _fmt_pct(v: object) -> str:
    """Format a numeric percentage, or '—' for missing/non-numeric values."""
    return f"{v:.0f}%" if isinstance(v, (int, float)) else "—"


# ---------------------------------------------------------------------------
# build_charts — read Storage and produce ChartData
# ---------------------------------------------------------------------------

# Column names per source table: (ts, cpu, max-core, mem, (psi cpu, mem, io)).
_SYS_CHART_COLUMNS = {
    "sys_rollup_1m": (
        "ts_min",
        "cpu_avg",
        "cpu_max_core_max",
        "mem_used_avg",
        ("psi_cpu_max", "psi_mem_max", "psi_io_max"),
    ),
    "sys_samples": (
        "ts",
        "cpu_total",
        "cpu_max_core",
        "mem_used",
        ("psi_cpu_some10", "psi_mem_some10", "psi_io_some10"),
    ),
}


def build_charts(
    store: Storage,
    *,
    since: float | None = None,
    config: "ProcdiagConfig | None" = None,
) -> ChartData:
    """Build a ChartData from a Storage instance.

    Reads sys_rollup_1m first (preferred because it is already aggregated);
    falls back to sys_samples when the rollup table is empty (e.g. the DB was
    created just moments ago).  Reads disk_samples for per-mount series.
    Reads spike_events for the spikes list.

    Returns an empty ChartData (no exception) when the DB contains no data.
    With *config*, the threshold guide lines follow ``rules.overrides``.
    """
    charts = ChartData()
    if config is not None:
        core_rule, _ = configure_rule(CpuSinglecoreRule, config)
        psi_rule, _ = configure_rule(CpuSaturationRule, config)
        charts.core_pct_threshold = core_rule.core_pct_threshold
        charts.psi_cpu_threshold = psi_rule.psi_threshold

    try:
        _fill_system_series(charts, store, since)
        charts.mounts = _mount_series(store.rows("disk_samples", since=since))
        charts.spikes = [
            {
                "ts": row["ts"],
                "trigger_type": row["trigger_type"],
                "psi_snapshot": row["psi_snapshot"],
                "top_processes": culprits(row["top_processes"], store),
            }
            for row in store.rows("spike_events")
        ]
    except (sqlite3.Error, OSError) as exc:
        # Expected DB/IO read error — return whatever was collected so far
        # rather than aborting the report, but log it so the gap is visible.
        # Any other (unexpected) exception propagates so bugs are not hidden.
        logger.warning("build_charts: チャート構築を中断しました: %s", exc)

    return charts


def _fill_system_series(charts: ChartData, store: Storage, since: float | None) -> None:
    """Fill the cpu / max-core / mem / PSI series in a single pass.

    Reads whichever sys table has data exactly once (rollup preferred) instead
    of issuing one query per series; the column names differ between the
    rollup and raw tables, so they are looked up for the table actually read.
    """
    table = "sys_rollup_1m"
    sys_rows = store.rows(table, since=since)
    if not sys_rows:
        table = "sys_samples"
        sys_rows = store.rows(table, since=since)
    ts_key, cpu_key, core_key, mem_key, psi_keys = _SYS_CHART_COLUMNS[table]

    for row in sys_rows:
        ts = row[ts_key]
        if ts is None:
            continue
        tsf = float(ts)
        for series, key in (
            (charts.cpu, cpu_key),
            (charts.cpu_max_core, core_key),
            (charts.mem_used, mem_key),
        ):
            v = row[key]
            if v is not None:
                series.append((tsf, float(v)))
        psi = [row[k] for k in psi_keys]
        if any(v is not None for v in psi):
            c_val, m_val, i_val = (float(v) if v is not None else 0.0 for v in psi)
            charts.psi.append((tsf, c_val, m_val, i_val))


def _mount_series(disk_rows: List[sqlite3.Row]) -> dict[str, List[Tuple[float, float]]]:
    """Per-mountpoint used% series from ``disk_samples`` rows."""
    mounts: dict[str, List[Tuple[float, float]]] = {}
    for row in disk_rows:
        mp, total, used = row["mountpoint"], row["total"], row["used"]
        if mp is None or total is None or used is None or total == 0:
            continue
        mounts.setdefault(mp, []).append((float(row["ts"]), used / total * 100.0))
    return mounts


# ---------------------------------------------------------------------------
# rule-coverage helpers (shared by terminal / HTML)
# ---------------------------------------------------------------------------


def _coverage_summary(diag: Diagnosis) -> str | None:
    """One-line rule coverage, or None when nothing ran / no data at all.

    A healthy verdict only speaks for the rules that actually ran, so the
    report always says how many did — a short recording must not read as
    "all clear" (most regression rules need 6–24 h of data).
    """
    n_eval = len(diag.evaluated_rules)
    n_skip = len(diag.skipped_rules)
    n_err = len(diag.errored_rules)
    n_unc = len(diag.uncovered_rules)
    total = n_eval + n_skip + n_err + n_unc
    if total == 0 or diag.no_data:
        return None
    if diag.fully_evaluated:
        return f"評価範囲: {total} ルールすべてを評価"
    parts = [f"評価範囲: {total} ルール中 {n_eval} 件のみ評価"]
    if n_skip:
        longest = max(r.remaining_hours for r in diag.skipped_rules)
        parts.append(
            f"観測時間不足で未評価 {n_skip} 件"
            f" (あと最長 {longest:.1f} 時間の記録で全ルールを評価できます)"
        )
    if n_unc:
        parts.append(f"記録データが無く未評価 {n_unc} 件")
    if n_err:
        parts.append(f"実行エラー {n_err} 件: {', '.join(diag.errored_rules)}")
    return " / ".join(parts)


def _uncovered_list(diag: Diagnosis) -> str:
    return ", ".join(f"{r.rule_id} ({r.detail})" for r in diag.uncovered_rules)


def _skipped_by_wait(diag: Diagnosis) -> List[SkippedRule]:
    """Unevaluated rules, the one needing the longest further recording first."""
    return sorted(diag.skipped_rules, key=lambda r: -r.remaining_hours)


def _skipped_list(diag: Diagnosis) -> str:
    """``"MEM-PRESSURE-01 (あと 23.9h), MEM-LEAK-01 (あと 5.9h)"``."""
    return ", ".join(
        f"{r.rule_id} (あと {r.remaining_hours:.1f}h)" for r in _skipped_by_wait(diag)
    )


# ---------------------------------------------------------------------------
# render_terminal
# ---------------------------------------------------------------------------


def render_terminal(
    diag: Diagnosis, *, color: bool = True, verbose: bool = False
) -> str:
    """Render *diag* as a plain-text / ANSI terminal string.

    Layout mirrors netdiag render_terminal:
    1. Finding headline list (symbol + rule_id + subject + summary)
    2. '診断: 問題は見つかりませんでした。' when healthy
    3. '━━━ 診断 (Diagnosis) ━━━' separator followed by numbered proposals
    4. Observation-quality summary block
    5. (verbose) '推論過程:' section
    """
    lines: List[str] = []

    # --- Finding headline list ---
    for f in diag.findings:
        col = _c(_ANSI.get(f.severity, ""), color)
        sym = f.severity.symbol
        rule = f"[{f.rule_id}]".ljust(18)
        subj = f.subject.ljust(16)
        lines.append(f"{col}{sym}{_c(_RESET, color)} {rule} {subj} {f.summary}")

    lines.append("")

    if diag.no_data and not diag.findings:
        warn = _c(_ANSI.get(Severity.WARNING, ""), color)
        lines.append(
            f"{_c(_BOLD, color)}{warn}"
            f"⚠ 観測データがありません。procdiag record でデータを記録してから "
            f"再度 check してください。{_c(_RESET, color)}"
        )
    elif diag.healthy:
        scope = "" if diag.fully_evaluated else "評価できた範囲では"
        lines.append(
            f"{_c(_BOLD, color)}{_c(_GREEN, color)}"
            f"診断: {scope}問題は見つかりませんでした。{_c(_RESET, color)}"
        )
    else:
        lines.append(f"{_c(_BOLD, color)}━━━ 診断 (Diagnosis) ━━━{_c(_RESET, color)}")
        for i, f in enumerate(diag.findings, 1):
            sev_col = _c(_ANSI.get(f.severity, ""), color)
            lines.append(
                f"{sev_col}{_c(_BOLD, color)}{i}. 推定原因: {f.rule_id} — {f.summary} "
                f"[確信度: {_confidence_ja(f.confidence)}]{_c(_RESET, color)}"
            )
            if f.proposal:
                for pline in f.proposal.splitlines():
                    lines.append(f"     {pline}")
            if f.related:
                lines.append(
                    f"     関連する検出 (同一原因とみられるもの): "
                    f"{', '.join(f.related)}"
                )

    # --- Rule coverage ---
    coverage = _coverage_summary(diag)
    if coverage is not None:
        if diag.fully_evaluated:
            lines.append(coverage)
        else:
            warn = _c(_ANSI.get(Severity.WARNING, ""), color)
            lines.append(f"{warn}⚠ {coverage}{_c(_RESET, color)}")
            if diag.skipped_rules:
                lines.append(f"  未評価: {_skipped_list(diag)}")
            if diag.uncovered_rules:
                lines.append(f"  記録なしで未評価: {_uncovered_list(diag)}")

    # --- Observation quality block ---
    q = diag.quality
    lines.append("")
    lines.append("--- 観測品質 ---")
    lines.append(f"  観測期間      : {q.span_hours:.1f} 時間")
    lines.append(f"  欠測 gap 合計 : {q.gap_seconds:.0f} 秒")
    lines.append(
        f"  自己負荷      : 平均 {q.self_cpu_avg:.2f}%  最大 {q.self_cpu_max:.2f}%"
    )
    lines.append(f"  I/O 欠損率    : {q.missing_io_pct * 100:.1f}%")
    lines.append(f"  減速イベント  : {q.throttle_events} 回")
    for note in q.notes:
        lines.append(f"  備考: {note}")

    # --- Verbose reasoning ---
    if verbose and diag.reasoning:
        lines.append("")
        lines.append(f"{_c(_BOLD, color)}推論過程:{_c(_RESET, color)}")
        for step in diag.reasoning:
            lines.append(f"  {step}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# render_json
# ---------------------------------------------------------------------------


def render_json(diag: Diagnosis) -> str:
    """Render *diag* as a stable, machine-readable JSON string.

    Key names are fixed (never rename them) — external tools such as LLM
    pipelines and ticket systems depend on this shape.
    """
    return json.dumps(
        {
            "healthy": diag.healthy,
            "no_data": diag.no_data,
            "findings": [f.to_dict() for f in diag.findings],
            "evaluated_rules": list(diag.evaluated_rules),
            "skipped_rules": [r.to_dict() for r in diag.skipped_rules],
            "errored_rules": list(diag.errored_rules),
            "uncovered_rules": [r.to_dict() for r in diag.uncovered_rules],
            "quality": diag.quality.to_dict(),
            "reasoning": list(diag.reasoning),
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "procdiag_version": procdiag.__version__,
        },
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# SVG chart helpers
# ---------------------------------------------------------------------------

_SVG_PALETTE = ["#4a90d9", "#e06c2b", "#2ba56c"]
# Per-series dash patterns so PSI lines stay distinguishable without colour
# (red/green colour-blindness, monochrome print): CPU solid / MEM dashed /
# IO dotted.  Empty string = solid line.
_PSI_DASHES = ["", "6 3", "2 3"]

# Plot-area margins (px) leaving room for the axis and title labels.
_MARGIN_LEFT = 36
_MARGIN_RIGHT = 8
_MARGIN_TOP = 16
_MARGIN_BOTTOM = 20
_PSI_LEGEND_H = 16


def _bucket_downsample(
    series: List[Tuple[float, float]], n_buckets: int
) -> List[Tuple[float, float]]:
    """Reduce *series* to at most *n_buckets* points using equal-width buckets.

    Each bucket keeps the average value (not just max) so the line stays
    smooth.  Empty buckets are omitted so the result may have fewer than
    *n_buckets* points.  LTTB is not required by the spec; equal-width buckets
    are sufficient and keep the implementation in stdlib-only territory.
    """
    if len(series) <= n_buckets:
        return series
    ts_start = series[0][0]
    ts_end = series[-1][0]
    span = ts_end - ts_start
    if span <= 0:
        return series[:n_buckets]
    bucket_width = span / n_buckets
    buckets: dict[int, List[float]] = {}
    ts_buckets: dict[int, List[float]] = {}
    for ts, val in series:
        idx = min(int((ts - ts_start) / bucket_width), n_buckets - 1)
        buckets.setdefault(idx, []).append(val)
        ts_buckets.setdefault(idx, []).append(ts)
    result: List[Tuple[float, float]] = []
    for idx in sorted(buckets):
        vals = buckets[idx]
        ts_vals = ts_buckets[idx]
        result.append((sum(ts_vals) / len(ts_vals), sum(vals) / len(vals)))
    return result


@dataclass
class _Plot:
    """Maps ``(ts, value)`` data onto the plot area of one SVG chart."""

    w: int
    chart_h: int
    ts_min: float
    ts_max: float
    y_range: float

    @property
    def chart_w(self) -> int:
        return self.w - _MARGIN_LEFT - _MARGIN_RIGHT

    def y(self, val: float) -> float:
        return round(
            _MARGIN_TOP + (1.0 - min(val, self.y_range) / self.y_range) * self.chart_h,
            1,
        )

    def points(self, series: List[Tuple[float, float]]) -> str:
        """``x,y`` pairs for a ``<polyline points=…>`` attribute."""
        span = max(self.ts_max - self.ts_min, 1)
        return " ".join(
            f"{round(_MARGIN_LEFT + (ts - self.ts_min) / span * self.chart_w, 1)},"
            f"{self.y(v)}"
            for ts, v in series
        )


def _empty_svg(w: int, h: int, label: str) -> str:
    """Placeholder chart shown when a series has no data."""
    return (
        f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{w}" height="{h}" fill="#f5f5f5" rx="4"/>'
        f'<text x="{w//2}" y="{h//2}" text-anchor="middle" '
        f'font-size="12" fill="#999">データなし: {_html_lib.escape(label)}</text>'
        f"</svg>"
    )


def _y_max_label_svg(text: str) -> str:
    """The y-axis maximum label in the top-left corner."""
    return (
        f'<text x="{_MARGIN_LEFT - 2}" y="{_MARGIN_TOP + 4}" '
        f'text-anchor="end" font-size="10" fill="#666">{_html_lib.escape(text)}</text>'
    )


def _threshold_svg(plot: _Plot, threshold: float | None, label: str) -> str:
    """Dashed guide line marking a rule's decision boundary (empty if None)."""
    if threshold is None:
        return ""
    ty = plot.y(threshold)
    return (
        f'<line x1="{_MARGIN_LEFT}" y1="{ty}" x2="{plot.w - _MARGIN_RIGHT}" y2="{ty}" '
        f'stroke="#c0392b" stroke-width="1" stroke-dasharray="4 3" opacity="0.75"/>'
        f'<text x="{plot.w - _MARGIN_RIGHT}" y="{ty - 2}" text-anchor="end" '
        f'font-size="9" fill="#c0392b">{_html_lib.escape(label)}</text>'
    )


def _time_axis_svg(plot: _Plot, y: int) -> str:
    """Start / end time labels (host-local HH:MM) along the x axis."""
    fmt = "%H:%M"
    t_start = datetime.fromtimestamp(plot.ts_min).strftime(fmt)
    t_end = datetime.fromtimestamp(plot.ts_max).strftime(fmt)
    return (
        f'<text x="{_MARGIN_LEFT}" y="{y}" font-size="10" fill="#666">'
        f"{_html_lib.escape(t_start)}</text>"
        f'<text x="{plot.w - _MARGIN_RIGHT}" y="{y}" '
        f'text-anchor="end" font-size="10" fill="#666">'
        f"{_html_lib.escape(t_end)}</text>"
    )


def _title_svg(plot: _Plot, label: str) -> str:
    """Bold chart title centred above the plot area."""
    return (
        f'<text x="{_MARGIN_LEFT + plot.chart_w // 2}" y="{_MARGIN_TOP - 2}" '
        f'text-anchor="middle" font-size="11" font-weight="bold" fill="#333">'
        f"{_html_lib.escape(label)}</text>"
    )


def _svg_open(w: int, h: int) -> str:
    """Opening ``<svg>`` tag plus the chart background."""
    return (
        f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{w}" height="{h}" fill="#fafafa" rx="4"/>'
    )


def _polyline_svg(
    series: List[Tuple[float, float]],
    *,
    w: int = 720,
    h: int = 120,
    label: str = "",
    unit: str = "",
    y_max: float | None = None,
    color: str = "#4a90d9",
    threshold: float | None = None,
    threshold_label: str = "",
) -> str:
    """Generate a minimal SVG polyline for *series*.

    Downsamples to *w* points maximum so large series don't explode output
    size.  Draws start/end time labels and a y-axis max label.

    Returns a complete ``<svg>`` element string.
    """
    if not series:
        return _empty_svg(w, h, label)

    pts = _bucket_downsample(series, w)

    if y_max is None:
        v_max_data = max(v for _, v in pts)
        y_max = v_max_data if v_max_data > 0 else 1.0
    # Keep a threshold guide line on-scale when it sits above the data.
    if threshold is not None and threshold > y_max:
        y_max = threshold
    plot = _Plot(
        w=w,
        chart_h=h - _MARGIN_TOP - _MARGIN_BOTTOM,
        ts_min=pts[0][0],
        ts_max=pts[-1][0],
        y_range=y_max if y_max > 0 else 1.0,
    )

    return (
        _svg_open(w, h)
        + _y_max_label_svg(f"{y_max:.0f}{unit}")
        + _threshold_svg(plot, threshold, threshold_label)
        + f'<polyline points="{plot.points(pts)}" '
        f'fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/>'
        + _time_axis_svg(plot, h - 4)
        + _title_svg(plot, label)
        + "</svg>"
    )


def _psi_svg(
    psi: List[Tuple[float, float, float, float]],
    *,
    w: int = 720,
    h: int = 120,
    threshold: float | None = None,
    threshold_label: str = "",
) -> str:
    """Generate a 3-series PSI SVG with a legend.

    *psi* is a list of (ts, cpu_some10, mem_some10, io_some10) tuples.
    """
    if not psi:
        return _empty_svg(w, h, "PSI")

    labels = ["CPU PSI", "MEM PSI", "IO PSI"]

    # Downsample each series independently
    # psi tuples are (ts, cpu, mem, io); after `t, *row` unpacking row=[cpu,mem,io]
    series_list = [
        _bucket_downsample([(t, row[idx]) for t, *row in psi], w) for idx in range(3)
    ]

    # y-max across all three series
    all_vals = [v for s in series_list for _, v in s]
    y_max_data = max(all_vals) if all_vals else 1.0
    # Expand the scale so a threshold guide above the data stays visible.
    if threshold is not None and threshold > y_max_data:
        y_max_data = threshold
    chart_h = h - _MARGIN_TOP - _MARGIN_BOTTOM - _PSI_LEGEND_H
    plot = _Plot(
        w=w,
        chart_h=chart_h,
        ts_min=psi[0][0],
        ts_max=psi[-1][0],
        y_range=y_max_data if y_max_data > 0 else 1.0,
    )

    lines_svg = ""
    for series, color, dash in zip(series_list, _SVG_PALETTE, _PSI_DASHES):
        if not series:
            continue
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        lines_svg += (
            f'<polyline points="{plot.points(series)}" fill="none" stroke="{color}" '
            f'stroke-width="1.5" stroke-linejoin="round"{dash_attr}/>'
        )

    legend_y = h - _PSI_LEGEND_H + 2
    legend_svg = ""
    for idx, (lbl, col, dash) in enumerate(zip(labels, _SVG_PALETTE, _PSI_DASHES)):
        lx = _MARGIN_LEFT + idx * 100
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        legend_svg += (
            f'<line x1="{lx}" y1="{legend_y + 5}" x2="{lx + 14}" y2="{legend_y + 5}" '
            f'stroke="{col}" stroke-width="2"{dash_attr}/>'
            f'<text x="{lx + 18}" y="{legend_y + 8}" font-size="10" fill="#333">'
            f"{_html_lib.escape(lbl)}</text>"
        )

    return (
        _svg_open(w, h)
        + _y_max_label_svg(f"{y_max_data:.0f}%")
        + _threshold_svg(plot, threshold, threshold_label)
        + lines_svg
        + _time_axis_svg(plot, _MARGIN_TOP + chart_h + _MARGIN_BOTTOM - 4)
        + _title_svg(plot, "PSI")
        + legend_svg
        + "</svg>"
    )


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------

_CSS = """
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { font-size: 1.4rem; margin-bottom: .25rem; }
h2 { font-size: 1.1rem; margin-top: 2rem; border-bottom: 2px solid #ddd; padding-bottom: .25rem; }
h3 { font-size: 1rem; margin: 1rem 0 .25rem 0; }
table { border-collapse: collapse; width: 100%; margin: .75rem 0; }
td, th { padding: .35rem .6rem; border-bottom: 1px solid #ddd; font-size: .88rem; }
th { background: #f0f0f0; text-align: left; }
.badge-ok   { background: #d4edda; color: #155724; padding: .2rem .5rem; border-radius: 4px; font-weight: bold; }
.badge-warn { background: #fff3cd; color: #856404; padding: .2rem .5rem; border-radius: 4px; font-weight: bold; }
.badge-crit { background: #f8d7da; color: #721c24; padding: .2rem .5rem; border-radius: 4px; font-weight: bold; }
.finding { border-left: 4px solid #c33; padding: .5rem 1rem; margin: 1rem 0; background: #fafafa; }
.finding.warning { border-color: #d90; }
.finding.info    { border-color: #17a2b8; }
.proposal p { margin: .3rem 0 .3rem 1rem; font-size: .9rem; }
.quality-block { background: #f8f9fa; padding: 1rem; border-radius: 4px; font-size: .9rem; }
.quality-block dt { font-weight: bold; float: left; width: 12em; }
.quality-block dd { margin-left: 13em; }
.chart-row { margin: .75rem 0; }
.chart-label { font-size: .85rem; color: #555; margin-bottom: .1rem; }
.spike-table td { font-size: .82rem; }
@media print {
  /* Colour-independent fallback so a monochrome printout still distinguishes
     severities (badges become bordered, finding rules thicken their border). */
  .badge-ok, .badge-warn, .badge-crit {
    background: #fff !important; color: #000 !important; border: 1.5px solid #000;
  }
  .finding { border-left-width: 6px; }
}
"""


def _esc(s: object) -> str:
    return _html_lib.escape(str(s))


def render_html(
    diag: Diagnosis,
    charts: "ChartData | None" = None,
    *,
    hostname_label: str | None = None,
    meta_hostname: str | None = None,
) -> str:
    """Render a self-contained HTML report.

    Five sections:
    1. サマリ — health badge + finding count + finding list table
    2. 提案詳細 — per-finding 5-part proposal
    3. 時系列チャート — inline SVG charts from ChartData
    4. スパイクイベント一覧 — from ChartData.spikes
    5. 観測品質 — ObservationQuality details

    No external resources are referenced (no <script src>, no <link href>,
    no http:// / https:// URLs) — requirement from design doc §1.2.
    """
    # Determine title: explicit label > recorded meta hostname > default.
    title = hostname_label or meta_hostname or "procdiag レポート"

    # Times shown in the HTML (chart axes, spike table) come from
    # datetime.fromtimestamp() and are therefore in the host's *local* zone.
    # Keep the footer consistent (local + %Z) instead of mixing in UTC.
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>{_esc(title)}</title>
<style>
{_CSS}
</style></head><body>
<h1>{_esc(title)}</h1>
<p style="color:#888;font-size:.85rem;">生成日時: {_esc(generated_at)} / procdiag {_esc(procdiag.__version__)} ／ 時刻はすべてホストのローカルタイム</p>
{_summary_section(diag)}
{_proposal_section(diag)}
{_charts_section(charts)}
{_spike_section(charts.spikes if charts else [])}
{_quality_section(diag.quality)}
</body></html>"""


def _summary_section(diag: Diagnosis) -> str:
    """① サマリ: health badge, verdict and the finding table."""
    if diag.no_data and not diag.findings:
        badge = '<span class="badge-warn">観測データなし</span>'
        verdict = "観測データがありません。procdiag record でデータを記録してください"
    elif diag.healthy and diag.fully_evaluated:
        badge = '<span class="badge-ok">正常</span>'
        verdict = "問題は見つかりませんでした"
    elif diag.healthy:
        badge = '<span class="badge-warn">問題なし (一部未評価)</span>'
        verdict = "評価できた範囲では問題は見つかりませんでした"
    else:
        n_crit = sum(1 for f in diag.findings if f.severity == Severity.CRITICAL)
        n_warn = sum(1 for f in diag.findings if f.severity == Severity.WARNING)
        if n_crit:
            badge = f'<span class="badge-crit">異常 — 重大 {n_crit}件</span>'
        else:
            badge = f'<span class="badge-warn">注意 — 警告 {n_warn}件</span>'
        verdict = f"{len(diag.findings)} 件の問題を検出"

    finding_rows = "".join(
        f"<tr>"
        f"<td>{_esc(f.severity.symbol)}</td>"
        f"<td>{_esc(f.rule_id)}</td>"
        f"<td>{_esc(f.subject)}</td>"
        f"<td>{_esc(f.summary)}</td>"
        f"<td>{_esc(_confidence_ja(f.confidence))}</td>"
        f"</tr>"
        for f in diag.findings
    )

    return f"""
<h2>① サマリ</h2>
<p>{badge} &nbsp; {_esc(verdict)}</p>
{_coverage_html(diag)}<table>
  <thead><tr><th></th><th>ルールID</th><th>対象</th><th>サマリ</th><th>確信度</th></tr></thead>
  <tbody>{finding_rows}</tbody>
</table>
"""


def _coverage_html(diag: Diagnosis) -> str:
    """Coverage line plus, when incomplete, the table of unevaluated rules."""
    coverage = _coverage_summary(diag)
    if coverage is None:
        return ""
    html = f"<p>{_esc(coverage)}</p>\n"
    if diag.skipped_rules:
        rows = "".join(
            f"<tr><td>{_esc(r.rule_id)}</td><td>{_esc(r.title)}</td>"
            f"<td>{r.required_hours:.1f} 時間</td><td>あと {r.remaining_hours:.1f} 時間</td></tr>"
            for r in _skipped_by_wait(diag)
        )
        html += (
            "<table>\n  <thead><tr><th>未評価ルール</th><th>内容</th>"
            "<th>必要な観測時間</th><th>残り</th></tr></thead>\n"
            f"  <tbody>{rows}</tbody>\n</table>\n"
        )
    if diag.uncovered_rules:
        rows = "".join(
            f"<tr><td>{_esc(r.rule_id)}</td><td>{_esc(r.title)}</td>"
            f"<td>{_esc(r.detail)}</td></tr>"
            for r in diag.uncovered_rules
        )
        html += (
            "<table>\n  <thead><tr><th>記録なしで未評価</th><th>内容</th>"
            "<th>理由</th></tr></thead>\n"
            f"  <tbody>{rows}</tbody>\n</table>\n"
        )
    return html


def _proposal_section(diag: Diagnosis) -> str:
    """② 提案詳細: one card per finding with its rendered proposal."""
    if not diag.findings:
        return "<h2>② 提案詳細</h2><p>検出された問題はありません。</p>"

    detail_html = ""
    for f in diag.findings:
        if f.proposal:
            proposal_html = "".join(
                f"<p>{_esc(line)}</p>" for line in f.proposal.splitlines()
            )
        else:
            proposal_html = "<p>（提案テンプレートなし）</p>"

        related_html = ""
        if f.related:
            related_html = (
                f"<p><em>関連する検出 (同一原因とみられるもの): "
                f"{_esc(', '.join(f.related))}</em></p>"
            )

        detail_html += f"""
<div class="finding {f.severity.value}">
  <h3>{_esc(f.severity.symbol)} {_esc(f.rule_id)} — {_esc(f.summary)} <small>[確信度: {_esc(_confidence_ja(f.confidence))}]</small></h3>
  <div class="proposal">{proposal_html}</div>
  {related_html}
</div>
"""
    return f"<h2>② 提案詳細</h2>\n{detail_html}"


def _charts_section(charts: "ChartData | None") -> str:
    """③ 時系列チャート: CPU, max core, memory, PSI and one chart per mount.

    The guide lines are the decision boundaries of the rules that read these
    series, taken from the rule classes so chart and rule cannot drift apart.
    """
    html = "<h2>③ 時系列チャート</h2>"
    if charts is None:
        return html + "<p>チャートデータなし。</p>"

    core_pct = charts.core_pct_threshold
    psi_pct = charts.psi_cpu_threshold
    mem_y_max = max(v for _, v in charts.mem_used) * 1.1 if charts.mem_used else None
    svgs = [
        _polyline_svg(
            charts.cpu, label="CPU使用率", unit="%", y_max=100.0, color="#4a90d9"
        ),
        _polyline_svg(
            charts.cpu_max_core,
            label="CPU最大コア",
            unit="%",
            y_max=100.0,
            color="#e06c2b",
            threshold=core_pct,
            threshold_label=f"単一コア飽和 {core_pct:.0f}%",
        ),
        _polyline_svg(
            charts.mem_used,
            label="メモリ使用量 (bytes)",
            unit="B",
            y_max=mem_y_max,
            color="#2ba56c",
        ),
        _psi_svg(
            charts.psi,
            threshold=psi_pct,
            threshold_label=f"CPU飽和 {psi_pct:.0f}%",
        ),
    ]
    for mp, series in sorted(charts.mounts.items()):
        svgs.append(
            _polyline_svg(
                series,
                label=f"ディスク {mp}",  # escaped once, by the SVG title helper
                unit="%",
                y_max=100.0,
                color="#8e44ad",
            )
        )
    return html + "".join(f'<div class="chart-row">{svg}</div>' for svg in svgs)


def _spike_section(spikes: List[dict]) -> str:
    """④ スパイクイベント一覧: time, trigger, PSI snapshot and culprits."""
    rows_html = ""
    for ev in spikes:
        ts_str = datetime.fromtimestamp(ev.get("ts", 0)).strftime("%Y-%m-%d %H:%M:%S")
        psi_str = _format_psi_snapshot(ev.get("psi_snapshot", ""))
        procs = ev.get("top_processes") or []
        if procs:
            procs_str = " / ".join(
                f"{_esc(p.get('name', '?'))}(pid {_esc(p.get('pid', '?'))}) "
                f"CPU {_fmt_pct(p.get('cpu_pct'))}"
                for p in procs
            )
        else:
            procs_str = "—"
        rows_html += (
            f"<tr class='spike-table'>"
            f"<td>{_esc(ts_str)}</td>"
            f"<td>{_esc(ev.get('trigger_type', ''))}</td>"
            f"<td><small>{_esc(psi_str)}</small></td>"
            f"<td><small>{procs_str}</small></td>"
            f"</tr>"
        )

    return f"""
<h2>④ スパイクイベント一覧</h2>
<table>
  <thead><tr><th>時刻</th><th>種別</th><th>PSI (CPU/MEM/IO)</th><th>上位プロセス (CPU順)</th></tr></thead>
  <tbody>{rows_html if rows_html else '<tr><td colspan="4">イベントなし</td></tr>'}</tbody>
</table>
"""


def _quality_section(q: ObservationQuality) -> str:
    """⑤ 観測品質: how much the findings above can be trusted."""
    return f"""
<h2>⑤ 観測品質</h2>
<div class="quality-block">
  <dl>
    <dt>観測期間</dt><dd>{q.span_hours:.1f} 時間</dd>
    <dt>欠測 gap 合計</dt><dd>{q.gap_seconds:.0f} 秒</dd>
    <dt>自己負荷 (平均)</dt><dd>{q.self_cpu_avg:.2f}%</dd>
    <dt>自己負荷 (最大)</dt><dd>{q.self_cpu_max:.2f}%</dd>
    <dt>I/O 欠損率</dt><dd>{q.missing_io_pct * 100:.1f}%</dd>
    <dt>減速イベント</dt><dd>{q.throttle_events} 回</dd>
  </dl>
  {"".join(f"<p>備考: {_esc(n)}</p>" for n in q.notes)}
</div>
"""


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def _iso(ts: float | None) -> str | None:
    return None if ts is None else _local(ts).isoformat(timespec="seconds")


def _value_text(state: SideState, headline: Headline) -> str:
    if isinstance(state, Unknown) or state.value is None:
        return "?"
    return f"{state.value:.{headline.digits}f}"


def _state_text(state: SideState) -> str:
    if isinstance(state, Detected):
        count = f", {state.count}件" if state.count > 1 else ""
        return f"検出 ({state.severity.value}{count})"
    if isinstance(state, Clear):
        return "検出なし"
    return "判定不能"


def _row_change(row: ComparisonRow) -> str:
    if row.headline is None:
        return f"{_state_text(row.before)} → {_state_text(row.after)}"
    h = row.headline
    text = (
        f"{h.label} {_value_text(row.before, h)} → {_value_text(row.after, h)} {h.unit}"
    )
    if isinstance(row.after, Detected):
        text += f"  (修正後も {row.after.severity.value})"
    elif (
        isinstance(row.after, Clear)
        and row.verdict is not Verdict.RESOLVED
        and "r2" in row.after.evidence
    ):
        text += f"  (修正後 R²={row.after.evidence['r2']:.2f})"
    return text


def _period_text(side: Side, *, show_db: bool) -> str:
    db = f"  [{side.db}]" if show_db else ""
    res = side.result
    if res is None:
        return f"{side.name.label}: 期間内に記録がありません{db}"
    start = _local(res.window.start_ts).strftime("%Y-%m-%d %H:%M")
    end = _local(res.window.end_ts).strftime("%Y-%m-%d %H:%M")
    return (
        f"{side.name.label}: {start} 〜 {end}  {res.window.span_hours:.1f} 時間"
        f" (うち生データ {res.raw_hours:.1f} 時間){db}"
    )


def _unevaluated_items(side: Side) -> List[Tuple[str, str]]:
    if side.result is None:
        return []
    diag = side.result.diagnosis
    return (
        [(r.rule_id, f"あと {r.remaining_hours:.1f}h") for r in diag.skipped_rules]
        + [(r.rule_id, r.detail) for r in diag.uncovered_rules]
        + [(rid, "実行エラー") for rid in diag.errored_rules]
    )


def _compare_conclusion(cmp: Comparison) -> str:
    code = compare_exit_code(cmp)
    if code == 1:
        return "修正後もまだ問題があります。"
    if cmp.before.result is None or cmp.after.result is None:
        return "片方の期間に記録がないため比較できません。"
    if code == 3:
        return "判定できない検出があります。記録を続けてから比べ直してください。"
    if not cmp.rows:
        return "どちらの期間にも問題は検出されませんでした。"
    if all(r.verdict is Verdict.RESOLVED for r in cmp.rows):
        return "修正前に検出された問題はすべて解消しました。"
    if code == 2:
        return "修正後は info の検出のみです。"
    return "修正後に warning / critical の検出はありません。"


def _cols(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


_VERDICT_ANSI = {
    Verdict.WORSE: _ANSI[Severity.CRITICAL],
    Verdict.NEW: _ANSI[Severity.CRITICAL],
    Verdict.UNCHANGED: _ANSI[Severity.WARNING],
    Verdict.NOT_EVALUABLE: _ANSI[Severity.WARNING],
    Verdict.IMPROVED: _GREEN,
    Verdict.RESOLVED: _GREEN,
}


def render_comparison_terminal(
    cmp: Comparison, *, color: bool = True, verbose: bool = False
) -> str:
    show_db = cmp.before.db != cmp.after.db
    zone = datetime.now().astimezone().strftime("%Z %z")
    reset = _c(_RESET, color)
    warn = _c(_ANSI[Severity.WARNING], color)
    lines = [
        _period_text(cmp.before, show_db=show_db),
        _period_text(cmp.after, show_db=show_db),
        f"時刻はこのホストのローカルタイム ({zone})",
    ]
    lines += [f"{warn}注意: {n.message}{reset}" for n in cmp.notes]
    lines.append("")

    width = max((_cols(r.label) for r in cmp.rows), default=0)
    for row in cmp.rows:
        col = _c(_VERDICT_ANSI[row.verdict], color)
        label = row.label + " " * (width - _cols(row.label))
        rule = f"[{row.rule_id}]".ljust(18)
        lines.append(
            f"{col}{row.verdict.symbol} {label}{reset}"
            f"  {rule} {row.subject.ljust(16)} {_row_change(row)}"
        )
        for name, state in ((SideName.BEFORE, row.before), (SideName.AFTER, row.after)):
            if isinstance(state, Unknown):
                lines.append(f"      {name.label}: {state.detail}")
        if verbose and isinstance(row.after, Detected) and row.after.finding.proposal:
            lines += [f"      {p}" for p in row.after.finding.proposal.splitlines()]
    if cmp.rows:
        lines.append("")

    for side in (cmp.before, cmp.after):
        by_reason: dict[str, list[str]] = {}
        for rid, why in _unevaluated_items(side):
            by_reason.setdefault(why, []).append(rid)
        if by_reason:
            lines.append(f"{side.name.label}の未評価:")
            lines += [f"  {', '.join(r)}: {why}" for why, r in by_reason.items()]

    counts = [f"{v.label} {n} 件" for v, n in cmp.verdict_counts().items() if n]
    summary = " / ".join(counts) + "。" if counts else ""
    lines.append(f"{_c(_BOLD, color)}結果: {summary}{_compare_conclusion(cmp)}{reset}")

    if verbose:
        for side in (cmp.before, cmp.after):
            if side.result and side.result.diagnosis.reasoning:
                lines.append("")
                lines.append(f"{_c(_BOLD, color)}推論過程 ({side.name.label}):{reset}")
                lines += [f"  {s}" for s in side.result.diagnosis.reasoning]
    return "\n".join(lines)


def _state_dict(state: SideState) -> dict:
    if isinstance(state, Detected):
        return {
            "state": "detected",
            "severity": state.severity.value,
            "count": state.count,
            "value": state.value,
            "confidence": state.finding.confidence.value,
            "summary": state.finding.summary,
        }
    if isinstance(state, Clear):
        return {"state": "clear", "value": state.value, "evidence": state.evidence}
    return {"state": "unknown", "why": state.why.value, "detail": state.detail}


def _side_dict(side: Side) -> dict:
    res = side.result
    diag = res.diagnosis if res else Diagnosis(findings=[], healthy=True)
    return {
        "db": str(side.db),
        "hostname": side.hostname,
        "schema_version": side.schema_version,
        "requested": {
            "since": _iso(side.requested.since),
            "until": _iso(side.requested.until),
        },
        "window": (
            {"start": _iso(res.window.start_ts), "end": _iso(res.window.end_ts)}
            if res
            else None
        ),
        "span_hours": res.window.span_hours if res else 0.0,
        "raw_hours": res.raw_hours if res else 0.0,
        "no_data": res is None,
        "evaluated_rules": list(diag.evaluated_rules),
        "skipped_rules": [r.to_dict() for r in diag.skipped_rules],
        "uncovered_rules": [r.to_dict() for r in diag.uncovered_rules],
        "errored_rules": list(diag.errored_rules),
        "quality": diag.quality.to_dict() if res else None,
    }


def _row_dict(row: ComparisonRow) -> dict:
    headline = None
    if row.headline is not None:
        headline = {
            "label": row.headline.label,
            "unit": row.headline.unit,
            "threshold": row.threshold,
        }
    return {
        "rule_id": row.rule_id,
        "title": row.title,
        "subject": row.subject,
        "identity": row.identity,
        "verdict": row.verdict.value,
        "why": row.why.value,
        "label": row.label,
        "headline": headline,
        "before": _state_dict(row.before),
        "after": _state_dict(row.after),
    }


def render_comparison_json(cmp: Comparison) -> str:
    """Stable JSON (``"format": "procdiag-compare/1"``); key names are frozen."""
    notes = [
        {
            "kind": n.kind.value,
            "side": n.side.value if n.side else None,
            "detail": dict(n.detail),
            "message": n.message,
        }
        for n in cmp.notes
    ]
    return json.dumps(
        {
            "format": COMPARE_FORMAT,
            "verdict_counts": {v.value: n for v, n in cmp.verdict_counts().items()},
            "rows": [_row_dict(r) for r in cmp.rows],
            "before": _side_dict(cmp.before),
            "after": _side_dict(cmp.after),
            "notes": notes,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "procdiag_version": procdiag.__version__,
        },
        indent=2,
        ensure_ascii=False,
    )
