"""Render a Diagnosis for humans (terminal), machines (JSON), or archive (HTML+SVG).

Conclusion-first layout mirrors netdiag/report.py: the headline is the root
cause and the next step; full evidence and proposal detail follow for readers
who want it.  The HTML output is completely self-contained — inline CSS, server-
side SVG charts, no CDN references — so it works in fully offline/air-gapped
factory environments.

Public API (called by cli.py / T08):
    render_terminal(diag, *, color=True, verbose=False) -> str
    render_json(diag) -> str
    render_html(diag, charts=None, *, hostname_label=None, meta_hostname=None) -> str
    build_charts(store, *, since=None) -> ChartData

ChartData is a dataclass whose fields hold (ts, value) series for the main
system metrics, plus a ``spikes`` list for the spike-event section in HTML.
"""

from __future__ import annotations

import html as _html_lib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Tuple

import procdiag
from procdiag.core import Diagnosis, Finding, ObservationQuality, Severity
from procdiag.storage import Storage

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
# spike-event enrichment helpers
# ---------------------------------------------------------------------------


def _parse_top_processes(raw: object, store: Storage, *, limit: int = 3) -> List[dict]:
    """Parse a spike row's ``top_processes`` JSON into named culprit entries.

    ``top_processes`` is stored as ``{"by_cpu": [...], "by_rss": [...]}`` where
    each entry has ``proc_id``/``pid``/``cpu_pct``/``rss`` but **no name** (the
    recorder keeps the row lean).  Resolve ``proc_id`` -> name here so the report
    can name the culprit instead of dumping ids.  Defensive: any parse or lookup
    failure yields an empty list rather than aborting the report.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    by_cpu = data.get("by_cpu", []) if isinstance(data, dict) else []
    out: List[dict] = []
    for entry in by_cpu[:limit]:
        if not isinstance(entry, dict):
            continue
        proc_id = entry.get("proc_id")
        try:
            name = store.proc_name(proc_id) if proc_id is not None else "?"
        except sqlite3.Error:
            name = f"proc_{proc_id}"
        out.append(
            {
                "name": name,
                "pid": entry.get("pid"),
                "cpu_pct": entry.get("cpu_pct"),
                "rss": entry.get("rss"),
            }
        )
    return out


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
        v = d.get(key)
        return f"{v:.0f}%" if isinstance(v, (int, float)) else "—"

    return f"CPU {g('cpu_some10')} / MEM {g('mem_some10')} / IO {g('io_some10')}"


def _fmt_pct(v: object) -> str:
    """Format a numeric percentage, or '—' for missing/non-numeric values."""
    return f"{v:.0f}%" if isinstance(v, (int, float)) else "—"


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
        # Read whichever sys table has data exactly once (rollup preferred),
        # then extract cpu / cpu_max_core / mem / PSI in a single pass instead
        # of issuing six series() queries + a separate rows() for PSI.  The
        # column names differ between the rollup and raw tables, so pick them
        # up front based on which table we actually read.
        sys_rows = store.rows("sys_rollup_1m", since=since)
        if sys_rows:
            ts_key = "ts_min"
            cpu_key, core_key, mem_key = "cpu_avg", "cpu_max_core_max", "mem_used_avg"
            psi_keys = ("psi_cpu_max", "psi_mem_max", "psi_io_max")
        else:
            sys_rows = store.rows("sys_samples", since=since)
            ts_key = "ts"
            cpu_key, core_key, mem_key = "cpu_total", "cpu_max_core", "mem_used"
            psi_keys = ("psi_cpu_some10", "psi_mem_some10", "psi_io_some10")

        cpu_series: List[Tuple[float, float]] = []
        core_series: List[Tuple[float, float]] = []
        mem_series: List[Tuple[float, float]] = []
        psi_list: List[Tuple[float, float, float, float]] = []
        for row in sys_rows:
            ts = row[ts_key]
            if ts is None:
                continue
            tsf = float(ts)
            cpu_v = row[cpu_key]
            if cpu_v is not None:
                cpu_series.append((tsf, float(cpu_v)))
            core_v = row[core_key]
            if core_v is not None:
                core_series.append((tsf, float(core_v)))
            mem_v = row[mem_key]
            if mem_v is not None:
                mem_series.append((tsf, float(mem_v)))
            c_val, m_val, i_val = (row[k] for k in psi_keys)
            if c_val is not None or m_val is not None or i_val is not None:
                psi_list.append(
                    (
                        tsf,
                        float(c_val) if c_val is not None else 0.0,
                        float(m_val) if m_val is not None else 0.0,
                        float(i_val) if i_val is not None else 0.0,
                    )
                )
        charts.cpu = cpu_series
        charts.cpu_max_core = core_series
        charts.mem_used = mem_series
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
            keys = row.keys()
            top_raw = row["top_processes"] if "top_processes" in keys else None
            spikes.append(
                {
                    "ts": row["ts"],
                    "trigger_type": row["trigger_type"],
                    "psi_snapshot": row["psi_snapshot"],
                    "top_processes": _parse_top_processes(top_raw, store),
                }
            )
        charts.spikes = spikes

    except (sqlite3.Error, OSError) as exc:
        # Expected DB/IO read error — return whatever was collected so far
        # rather than aborting the report, but log it so the gap is visible.
        # Any other (unexpected) exception propagates so bugs are not hidden.
        logger.warning("build_charts: チャート構築を中断しました: %s", exc)

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

    if diag.no_data and not diag.findings:
        warn = _c(_ANSI.get(Severity.WARNING, ""), color)
        lines.append(
            f"{_c(_BOLD, color)}{warn}"
            f"⚠ 観測データがありません。procdiag record でデータを記録してから "
            f"再度 check してください。{_c(_RESET, color)}"
        )
    elif diag.healthy:
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
            "no_data": diag.no_data,
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
# Per-series dash patterns so PSI lines stay distinguishable without colour
# (red/green colour-blindness, monochrome print): CPU solid / MEM dashed /
# IO dotted.  Empty string = solid line.
_PSI_DASHES = ["", "6 3", "2 3"]


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
    threshold: float | None = None,
    threshold_label: str = "",
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
    # Keep a threshold guide line on-scale when it sits above the data.
    if threshold is not None and threshold > y_max:
        y_max = threshold
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

    threshold_svg = ""
    if threshold is not None:
        ty = round(margin_top + (1.0 - min(threshold, y_range) / y_range) * chart_h, 1)
        threshold_svg = (
            f'<line x1="{margin_left}" y1="{ty}" x2="{w - margin_right}" y2="{ty}" '
            f'stroke="#c0392b" stroke-width="1" stroke-dasharray="4 3" opacity="0.75"/>'
            f'<text x="{w - margin_right}" y="{ty - 2}" text-anchor="end" '
            f'font-size="9" fill="#c0392b">{_html_lib.escape(threshold_label)}</text>'
        )

    return (
        f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{w}" height="{h}" fill="#fafafa" rx="4"/>'
        # y-axis max label
        f'<text x="{margin_left - 2}" y="{margin_top + 4}" '
        f'text-anchor="end" font-size="10" fill="#666">{_html_lib.escape(y_label)}</text>'
        # threshold guide line (rule decision boundary)
        f"{threshold_svg}"
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
    threshold: float | None = None,
    threshold_label: str = "",
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
    # Expand the scale so a threshold guide above the data stays visible.
    if threshold is not None and threshold > y_max_data:
        y_max_data = threshold
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
    for series, color, dash in zip(series_list, colors, _PSI_DASHES):
        if not series:
            continue
        pts = " ".join(f"{px(ts, v)[0]},{px(ts, v)[1]}" for ts, v in series)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        lines_svg += (
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="1.5" stroke-linejoin="round"{dash_attr}/>'
        )

    # Legend
    legend_y = h - legend_h + 2
    legend_svg = ""
    for idx, (lbl, col, dash) in enumerate(zip(labels, colors, _PSI_DASHES)):
        lx = margin_left + idx * 100
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        legend_svg += (
            f'<line x1="{lx}" y1="{legend_y + 5}" x2="{lx + 14}" y2="{legend_y + 5}" '
            f'stroke="{col}" stroke-width="2"{dash_attr}/>'
            f'<text x="{lx + 18}" y="{legend_y + 8}" font-size="10" fill="#333">'
            f"{_html_lib.escape(lbl)}</text>"
        )

    fmt = "%H:%M"
    t_start = datetime.fromtimestamp(ts_min).strftime(fmt)
    t_end = datetime.fromtimestamp(ts_max).strftime(fmt)
    y_label = f"{y_max_data:.0f}%"

    threshold_svg = ""
    if threshold is not None:
        ty = round(margin_top + (1.0 - min(threshold, y_range) / y_range) * chart_h, 1)
        threshold_svg = (
            f'<line x1="{margin_left}" y1="{ty}" x2="{w - margin_right}" y2="{ty}" '
            f'stroke="#c0392b" stroke-width="1" stroke-dasharray="4 3" opacity="0.75"/>'
            f'<text x="{w - margin_right}" y="{ty - 2}" text-anchor="end" '
            f'font-size="9" fill="#c0392b">{_html_lib.escape(threshold_label)}</text>'
        )

    return (
        f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{w}" height="{h}" fill="#fafafa" rx="4"/>'
        f'<text x="{margin_left - 2}" y="{margin_top + 4}" '
        f'text-anchor="end" font-size="10" fill="#666">{_html_lib.escape(y_label)}</text>'
        + threshold_svg
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
@media print {
  /* Colour-independent fallback so a monochrome printout still distinguishes
     severities (badges become bordered, finding rules thicken their border). */
  .badge-ok, .badge-warn, .badge-crit {
    background: #fff !important; color: #000 !important; border: 1.5px solid #000;
  }
  .finding { border-left-width: 6px; }
}
"""


_SEV_SYMBOL = {
    Severity.CRITICAL: "✘",
    Severity.WARNING: "⚠",
    Severity.INFO: "ℹ",
}


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

    def esc(s: object) -> str:
        return _html_lib.escape(str(s))

    # Determine title: explicit label > recorded meta hostname > default.
    title = hostname_label or meta_hostname or "procdiag レポート"

    # --- ① サマリ ---
    if diag.no_data and not diag.findings:
        badge = '<span class="badge-warn">観測データなし</span>'
        verdict = "観測データがありません。procdiag record でデータを記録してください"
    elif diag.healthy:
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
        sym = _SEV_SYMBOL.get(f.severity, "")
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
  <h3>{esc(_SEV_SYMBOL.get(f.severity, ""))} {esc(f.rule_id)} — {esc(f.summary)} <small>[確信度: {esc(conf_ja)}]</small></h3>
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
            threshold=95.0,  # CPU-SINGLECORE-01 (_CPU_SINGLECORE_CORE_PCT)
            threshold_label="単一コア飽和 95%",
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
        psi_svg = _psi_svg(
            charts.psi,
            threshold=20.0,  # CPU-SATURATION-01 (_CPU_SATURATION_PSI_THRESHOLD)
            threshold_label="CPU飽和 20%",
        )
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
        psi_str = _format_psi_snapshot(ev.get("psi_snapshot", ""))
        procs = ev.get("top_processes") or []
        if procs:
            procs_str = " / ".join(
                f"{esc(p.get('name', '?'))}(pid {esc(p.get('pid', '?'))}) "
                f"CPU {_fmt_pct(p.get('cpu_pct'))}"
                for p in procs
            )
        else:
            procs_str = "—"
        spike_rows_html += (
            f"<tr class='spike-table'>"
            f"<td>{esc(ts_str)}</td>"
            f"<td>{esc(ev.get('trigger_type', ''))}</td>"
            f"<td><small>{esc(psi_str)}</small></td>"
            f"<td><small>{procs_str}</small></td>"
            f"</tr>"
        )

    spike_section = f"""
<h2>④ スパイクイベント一覧</h2>
<table>
  <thead><tr><th>時刻</th><th>種別</th><th>PSI (CPU/MEM/IO)</th><th>上位プロセス (CPU順)</th></tr></thead>
  <tbody>{spike_rows_html if spike_rows_html else '<tr><td colspan="4">イベントなし</td></tr>'}</tbody>
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

    # Times shown in the HTML (chart axes, spike table) come from
    # datetime.fromtimestamp() and are therefore in the host's *local* zone.
    # Keep the footer consistent (local + %Z) instead of mixing in UTC.
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>{esc(title)}</title>
<style>
{_CSS}
</style></head><body>
<h1>{esc(title)}</h1>
<p style="color:#888;font-size:.85rem;">生成日時: {esc(generated_at)} / procdiag {esc(procdiag.__version__)} ／ 時刻はすべてホストのローカルタイム</p>
{summary_section}
{proposal_section}
{charts_html}
{spike_section}
{quality_section}
</body></html>"""
