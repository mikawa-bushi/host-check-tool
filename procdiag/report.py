"""Render a Diagnosis for humans (terminal), machines (JSON), or archive (HTML+SVG).

Conclusion-first layout mirrors netdiag/report.py: the headline is the root
cause and the next step; full evidence and proposal detail follow for readers
who want it.  The HTML output is completely self-contained — inline CSS, server-
side SVG charts, no CDN references — so it works in fully offline/air-gapped
factory environments.

Public API (called by cli.py / T08):
    render_terminal(diag, *, color=True, verbose=False) -> str
    render_json(diag) -> str
    render_html(diag, charts=None, *, hostname_label=None) -> str
    build_charts(store, *, since=None) -> ChartData

ChartData is a dataclass whose fields hold (ts, value) series for the main
system metrics, plus a ``spikes`` list for the spike-event section in HTML.
"""

from __future__ import annotations

import html as _html_lib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Tuple

import procdiag
from procdiag.core import Diagnosis, Finding, ObservationQuality, Severity
from procdiag.storage import Storage

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


def _confidence_ja(conf: str) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(conf, conf)


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

    ``spikes`` is a list of plain dicts (ts, trigger_type, psi_snapshot) used
    for the spike-event table in the HTML output.
    """

    cpu: List[Tuple[float, float]] = field(default_factory=list)
    cpu_max_core: List[Tuple[float, float]] = field(default_factory=list)
    mem_used: List[Tuple[float, float]] = field(default_factory=list)
    # PSI: list of (ts, cpu_some10, mem_some10, io_some10)
    psi: List[Tuple[float, float, float, float]] = field(default_factory=list)
    mounts: dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    spikes: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# build_charts — read Storage and produce ChartData
# ---------------------------------------------------------------------------


def build_charts(store: Storage, *, since: float | None = None) -> ChartData:
    """Build a ChartData from a Storage instance.

    Reads sys_rollup_1m first (preferred because it is already aggregated);
    falls back to sys_samples when the rollup table is empty (e.g. the DB was
    created just moments ago).  Reads disk_samples for per-mount series.
    Reads spike_events for the spikes list.

    Returns an empty ChartData (no exception) when the DB contains no data.
    """
    charts = ChartData()

    try:
        # --- system series ---
        cpu_series = store.series("sys_rollup_1m", "cpu_avg", since=since)
        if not cpu_series:
            # fall back to raw samples
            cpu_series = store.series("sys_samples", "cpu_total", since=since)

        charts.cpu = cpu_series

        # cpu_max_core from rollup if available, else raw
        core_series = store.series("sys_rollup_1m", "cpu_max_core_max", since=since)
        if not core_series:
            core_series = store.series("sys_samples", "cpu_max_core", since=since)
        charts.cpu_max_core = core_series

        # mem_used
        mem_series = store.series("sys_rollup_1m", "mem_used_avg", since=since)
        if not mem_series:
            mem_series = store.series("sys_samples", "mem_used", since=since)
        charts.mem_used = mem_series

        # PSI — need three columns together; query rows() and build tuples
        psi_rows = store.rows("sys_rollup_1m", since=since)
        if not psi_rows:
            psi_rows = store.rows("sys_samples", since=since)
        psi_list: List[Tuple[float, float, float, float]] = []
        for row in psi_rows:
            ts_key = "ts_min" if "ts_min" in row.keys() else "ts"
            ts = row[ts_key]
            if ts is None:
                continue
            c_val = row["psi_cpu_max"] if "psi_cpu_max" in row.keys() else row.get("psi_cpu_some10", None)  # type: ignore[attr-defined]
            m_val = row["psi_mem_max"] if "psi_mem_max" in row.keys() else row.get("psi_mem_some10", None)  # type: ignore[attr-defined]
            i_val = row["psi_io_max"] if "psi_io_max" in row.keys() else row.get("psi_io_some10", None)  # type: ignore[attr-defined]
            if c_val is None and m_val is None and i_val is None:
                continue
            psi_list.append(
                (
                    float(ts),
                    float(c_val) if c_val is not None else 0.0,
                    float(m_val) if m_val is not None else 0.0,
                    float(i_val) if i_val is not None else 0.0,
                )
            )
        charts.psi = psi_list

        # --- disk series ---
        disk_rows = store.rows("disk_samples", since=since)
        mounts: dict[str, List[Tuple[float, float]]] = {}
        for row in disk_rows:
            mp = row["mountpoint"]
            total = row["total"]
            used = row["used"]
            if mp is None or total is None or used is None or total == 0:
                continue
            pct = used / total * 100.0
            mounts.setdefault(mp, []).append((float(row["ts"]), pct))
        charts.mounts = mounts

        # --- spike events ---
        spike_rows = store.rows("spike_events")
        spikes: List[dict] = []
        for row in spike_rows:
            spikes.append(
                {
                    "ts": row["ts"],
                    "trigger_type": row["trigger_type"],
                    "psi_snapshot": row["psi_snapshot"],
                }
            )
        charts.spikes = spikes

    except Exception:
        # Defensive: return whatever was collected so far, never raise.
        pass

    return charts


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

    if diag.healthy:
        lines.append(
            f"{_c(_BOLD, color)}{_c(_GREEN, color)}"
            f"診断: 問題は見つかりませんでした。{_c(_RESET, color)}"
        )
    else:
        lines.append(f"{_c(_BOLD, color)}━━━ 診断 (Diagnosis) ━━━{_c(_RESET, color)}")
        for i, f in enumerate(diag.findings, 1):
            sev_col = _c(_ANSI.get(f.severity, ""), color)
            conf_ja = _confidence_ja(
                f.confidence.value if hasattr(f.confidence, "value") else f.confidence
            )
            lines.append(
                f"{sev_col}{_c(_BOLD, color)}{i}. 推定原因: {f.rule_id} — {f.summary} "
                f"[確信度: {conf_ja}]{_c(_RESET, color)}"
            )
            if f.proposal:
                for pline in f.proposal.splitlines():
                    lines.append(f"     {pline}")
            if f.related:
                lines.append(
                    f"     関連する検出 (同一原因とみられるもの): "
                    f"{', '.join(f.related)}"
                )

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
    if q.notes:
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
            "findings": [f.to_dict() for f in diag.findings],
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


def _polyline_svg(
    series: List[Tuple[float, float]],
    *,
    w: int = 720,
    h: int = 120,
    label: str = "",
    unit: str = "",
    y_max: float | None = None,
    color: str = "#4a90d9",
) -> str:
    """Generate a minimal SVG polyline for *series*.

    Downsamples to *w* points maximum so large series don't explode output
    size.  Draws start/end time labels and a y-axis max label.

    Returns a complete ``<svg>`` element string.
    """
    if not series:
        pad = 4
        return (
            f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{w}" height="{h}" fill="#f5f5f5" rx="4"/>'
            f'<text x="{w//2}" y="{h//2}" text-anchor="middle" '
            f'font-size="12" fill="#999">データなし: {_html_lib.escape(label)}</text>'
            f"</svg>"
        )

    # Downsample to at most w data points
    pts = _bucket_downsample(series, w)

    ts_min = pts[0][0]
    ts_max = pts[-1][0]
    v_min = min(v for _, v in pts)
    v_max_data = max(v for _, v in pts)
    if y_max is None:
        y_max = v_max_data if v_max_data > 0 else 1.0
    y_range = y_max if y_max > 0 else 1.0

    # Margin for axis labels
    margin_left = 36
    margin_right = 8
    margin_top = 16
    margin_bottom = 20
    chart_w = w - margin_left - margin_right
    chart_h = h - margin_top - margin_bottom

    def px(ts: float, val: float) -> Tuple[float, float]:
        x = margin_left + (ts - ts_min) / max(ts_max - ts_min, 1) * chart_w
        y = margin_top + (1.0 - min(val, y_range) / y_range) * chart_h
        return (round(x, 1), round(y, 1))

    polyline_pts = " ".join(f"{px(ts, v)[0]},{px(ts, v)[1]}" for ts, v in pts)

    # Time axis labels
    fmt = "%H:%M"
    t_start = datetime.fromtimestamp(ts_min).strftime(fmt)
    t_end = datetime.fromtimestamp(ts_max).strftime(fmt)
    y_label = f"{y_max:.0f}{unit}"

    return (
        f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{w}" height="{h}" fill="#fafafa" rx="4"/>'
        # y-axis max label
        f'<text x="{margin_left - 2}" y="{margin_top + 4}" '
        f'text-anchor="end" font-size="10" fill="#666">{_html_lib.escape(y_label)}</text>'
        # polyline
        f'<polyline points="{polyline_pts}" '
        f'fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/>'
        # x-axis labels
        f'<text x="{margin_left}" y="{h - 4}" font-size="10" fill="#666">'
        f"{_html_lib.escape(t_start)}</text>"
        f'<text x="{w - margin_right}" y="{h - 4}" '
        f'text-anchor="end" font-size="10" fill="#666">'
        f"{_html_lib.escape(t_end)}</text>"
        # chart label
        f'<text x="{margin_left + chart_w // 2}" y="{margin_top - 2}" '
        f'text-anchor="middle" font-size="11" font-weight="bold" fill="#333">'
        f"{_html_lib.escape(label)}</text>"
        f"</svg>"
    )


def _psi_svg(
    psi: List[Tuple[float, float, float, float]],
    *,
    w: int = 720,
    h: int = 120,
) -> str:
    """Generate a 3-series PSI SVG with a legend.

    *psi* is a list of (ts, cpu_some10, mem_some10, io_some10) tuples.
    """
    if not psi:
        return (
            f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{w}" height="{h}" fill="#f5f5f5" rx="4"/>'
            f'<text x="{w//2}" y="{h//2}" text-anchor="middle" '
            f'font-size="12" fill="#999">データなし: PSI</text>'
            f"</svg>"
        )

    labels = ["CPU PSI", "MEM PSI", "IO PSI"]
    colors = _SVG_PALETTE

    # Downsample each series independently
    # psi tuples are (ts, cpu, mem, io); after `t, *row` unpacking row=[cpu,mem,io]
    series_list = [[(t, row[idx]) for t, *row in psi] for idx in range(3)]
    series_list = [_bucket_downsample(s, w) for s in series_list]

    ts_min = psi[0][0]
    ts_max = psi[-1][0]
    # y-max across all three series
    all_vals = [v for s in series_list for _, v in s]
    y_max_data = max(all_vals) if all_vals else 1.0
    y_range = y_max_data if y_max_data > 0 else 1.0

    legend_h = 16
    margin_left = 36
    margin_right = 8
    margin_top = 16
    margin_bottom = 20
    chart_h = h - margin_top - margin_bottom - legend_h
    chart_w = w - margin_left - margin_right

    def px(ts: float, val: float) -> Tuple[float, float]:
        x = margin_left + (ts - ts_min) / max(ts_max - ts_min, 1) * chart_w
        y = margin_top + (1.0 - min(val, y_range) / y_range) * chart_h
        return (round(x, 1), round(y, 1))

    lines_svg = ""
    for series, color in zip(series_list, colors):
        if not series:
            continue
        pts = " ".join(f"{px(ts, v)[0]},{px(ts, v)[1]}" for ts, v in series)
        lines_svg += (
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="1.5" stroke-linejoin="round"/>'
        )

    # Legend
    legend_y = h - legend_h + 2
    legend_svg = ""
    for idx, (lbl, col) in enumerate(zip(labels, colors)):
        lx = margin_left + idx * 100
        legend_svg += (
            f'<rect x="{lx}" y="{legend_y}" width="12" height="8" fill="{col}"/>'
            f'<text x="{lx + 15}" y="{legend_y + 8}" font-size="10" fill="#333">'
            f"{_html_lib.escape(lbl)}</text>"
        )

    fmt = "%H:%M"
    t_start = datetime.fromtimestamp(ts_min).strftime(fmt)
    t_end = datetime.fromtimestamp(ts_max).strftime(fmt)
    y_label = f"{y_max_data:.0f}%"

    return (
        f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{w}" height="{h}" fill="#fafafa" rx="4"/>'
        f'<text x="{margin_left - 2}" y="{margin_top + 4}" '
        f'text-anchor="end" font-size="10" fill="#666">{_html_lib.escape(y_label)}</text>'
        + lines_svg
        + f'<text x="{margin_left}" y="{margin_top + chart_h + margin_bottom - 4}" '
        f'font-size="10" fill="#666">{_html_lib.escape(t_start)}</text>'
        f'<text x="{w - margin_right}" y="{margin_top + chart_h + margin_bottom - 4}" '
        f'text-anchor="end" font-size="10" fill="#666">{_html_lib.escape(t_end)}</text>'
        f'<text x="{margin_left + chart_w // 2}" y="{margin_top - 2}" '
        f'text-anchor="middle" font-size="11" font-weight="bold" fill="#333">PSI</text>'
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
"""


def render_html(
    diag: Diagnosis,
    charts: "ChartData | None" = None,
    *,
    hostname_label: str | None = None,
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

    def esc(s: object) -> str:
        return _html_lib.escape(str(s))

    # Determine title
    meta_hostname: str | None = None
    title = hostname_label or meta_hostname or "procdiag レポート"

    # --- ① サマリ ---
    if diag.healthy:
        badge = '<span class="badge-ok">正常</span>'
        verdict = "問題は見つかりませんでした"
    else:
        n_crit = sum(1 for f in diag.findings if f.severity == Severity.CRITICAL)
        n_warn = sum(1 for f in diag.findings if f.severity == Severity.WARNING)
        if n_crit:
            badge = f'<span class="badge-crit">異常 — 重大 {n_crit}件</span>'
        else:
            badge = f'<span class="badge-warn">注意 — 警告 {n_warn}件</span>'
        verdict = f"{len(diag.findings)} 件の問題を検出"

    finding_rows = ""
    for f in diag.findings:
        sev_map = {
            Severity.CRITICAL: "✘",
            Severity.WARNING: "⚠",
            Severity.INFO: "ℹ",
        }
        sym = sev_map.get(f.severity, "")
        conf_ja = _confidence_ja(
            f.confidence.value if hasattr(f.confidence, "value") else f.confidence
        )
        finding_rows += (
            f"<tr>"
            f"<td>{esc(sym)}</td>"
            f"<td>{esc(f.rule_id)}</td>"
            f"<td>{esc(f.subject)}</td>"
            f"<td>{esc(f.summary)}</td>"
            f"<td>{esc(conf_ja)}</td>"
            f"</tr>"
        )

    summary_section = f"""
<h2>① サマリ</h2>
<p>{badge} &nbsp; {esc(verdict)}</p>
<table>
  <thead><tr><th></th><th>ルールID</th><th>対象</th><th>サマリ</th><th>確信度</th></tr></thead>
  <tbody>{finding_rows}</tbody>
</table>
"""

    # --- ② 提案詳細 ---
    detail_html = ""
    for f in diag.findings:
        sev_cls = f.severity.value  # "critical" / "warning" / "info"
        conf_ja = _confidence_ja(
            f.confidence.value if hasattr(f.confidence, "value") else f.confidence
        )
        proposal_html = ""
        if f.proposal:
            for line in f.proposal.splitlines():
                proposal_html += f"<p>{esc(line)}</p>"
        else:
            proposal_html = "<p>（提案テンプレートなし）</p>"

        related_html = ""
        if f.related:
            related_html = (
                f"<p><em>関連する検出 (同一原因とみられるもの): "
                f"{esc(', '.join(f.related))}</em></p>"
            )

        detail_html += f"""
<div class="finding {sev_cls}">
  <h3>{esc(f.rule_id)} — {esc(f.summary)} <small>[確信度: {esc(conf_ja)}]</small></h3>
  <div class="proposal">{proposal_html}</div>
  {related_html}
</div>
"""

    proposal_section = f"<h2>② 提案詳細</h2>\n{detail_html}"
    if not diag.findings:
        proposal_section = "<h2>② 提案詳細</h2><p>検出された問題はありません。</p>"

    # --- ③ 時系列チャート ---
    charts_html = "<h2>③ 時系列チャート</h2>"
    if charts is None:
        charts_html += "<p>チャートデータなし。</p>"
    else:
        # CPU
        cpu_svg = _polyline_svg(
            charts.cpu, label="CPU使用率", unit="%", y_max=100.0, color="#4a90d9"
        )
        charts_html += f'<div class="chart-row">{cpu_svg}</div>'

        # CPU max core
        core_svg = _polyline_svg(
            charts.cpu_max_core,
            label="CPU最大コア",
            unit="%",
            y_max=100.0,
            color="#e06c2b",
        )
        charts_html += f'<div class="chart-row">{core_svg}</div>'

        # Memory
        mem_y_max: float | None = None
        if charts.mem_used:
            mem_y_max = max(v for _, v in charts.mem_used) * 1.1
        mem_svg = _polyline_svg(
            charts.mem_used,
            label="メモリ使用量 (bytes)",
            unit="B",
            y_max=mem_y_max,
            color="#2ba56c",
        )
        charts_html += f'<div class="chart-row">{mem_svg}</div>'

        # PSI
        psi_svg = _psi_svg(charts.psi)
        charts_html += f'<div class="chart-row">{psi_svg}</div>'

        # Disk mount points
        if charts.mounts:
            for mp, series in sorted(charts.mounts.items()):
                disk_svg = _polyline_svg(
                    series,
                    label=f"ディスク {esc(mp)}",
                    unit="%",
                    y_max=100.0,
                    color="#8e44ad",
                )
                charts_html += f'<div class="chart-row">{disk_svg}</div>'

    # --- ④ スパイクイベント ---
    spike_rows_html = ""
    spike_list = charts.spikes if charts else []
    for ev in spike_list:
        ts_str = datetime.fromtimestamp(ev.get("ts", 0)).strftime("%Y-%m-%d %H:%M:%S")
        spike_rows_html += (
            f"<tr class='spike-table'>"
            f"<td>{esc(ts_str)}</td>"
            f"<td>{esc(ev.get('trigger_type', ''))}</td>"
            f"<td><small>{esc(ev.get('psi_snapshot', ''))}</small></td>"
            f"</tr>"
        )

    spike_section = f"""
<h2>④ スパイクイベント一覧</h2>
<table>
  <thead><tr><th>時刻</th><th>種別</th><th>PSIスナップショット</th></tr></thead>
  <tbody>{spike_rows_html if spike_rows_html else '<tr><td colspan="3">イベントなし</td></tr>'}</tbody>
</table>
"""

    # --- ⑤ 観測品質 ---
    q = diag.quality
    quality_section = f"""
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
  {"".join(f"<p>備考: {esc(n)}</p>" for n in q.notes)}
</div>
"""

    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>{esc(title)}</title>
<style>
{_CSS}
</style></head><body>
<h1>{esc(title)}</h1>
<p style="color:#888;font-size:.85rem;">生成日時: {esc(generated_at)} / procdiag {esc(procdiag.__version__)}</p>
{summary_section}
{proposal_section}
{charts_html}
{spike_section}
{quality_section}
</body></html>"""
