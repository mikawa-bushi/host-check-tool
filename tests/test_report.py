"""Tests for procdiag/report.py.

All Diagnosis / Finding / ChartData instances are synthesised here so this
test module has zero dependency on T06 (diagnosis engine).

Coverage targets:
- render_terminal: healthy / finding present / color=False / verbose reasoning
- render_json: round-trip via json.loads, required keys, no unicode escaping
- render_html: no external URLs, SVG present regardless of finding count,
               observation-quality numbers appear
- build_charts: real Storage in tmp_path with sys_samples + disk_samples rows;
                empty DB does not raise
- SVG downsampling: 10,000-point series produces output < 500 KB
"""

from __future__ import annotations

import json
import time

import pytest

from procdiag.core import (
    Confidence,
    Diagnosis,
    Finding,
    ObservationQuality,
    Severity,
)
from procdiag.report import (
    ChartData,
    build_charts,
    render_html,
    render_json,
    render_terminal,
)
from procdiag.storage import Storage

# ---------------------------------------------------------------------------
# Helpers — synthesise test fixtures
# ---------------------------------------------------------------------------


def _make_quality(*, span_hours: float = 12.0) -> ObservationQuality:
    return ObservationQuality(
        span_hours=span_hours,
        gap_seconds=30.0,
        missing_io_pct=0.1,
        self_cpu_avg=0.3,
        self_cpu_max=0.8,
        throttle_events=0,
        notes=["cgroup v2 なし — cgroup 計測をスキップ"],
    )


def _make_finding(
    rule_id: str = "MEM-LEAK-01",
    severity: Severity = Severity.WARNING,
    confidence: Confidence = Confidence.HIGH,
    related: list[str] | None = None,
    proposal: str = "",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        subject="mysqld",
        summary="RSS が 38 MB/h で単調増加 (R²=0.97, 18h 観測)",
        evidence={"slope_mb_per_h": 38.0, "r2": 0.97, "span_hours": 18.0},
        detected_at=(time.time() - 3600, time.time()),
        confidence=confidence,
        related=related or [],
        proposal=proposal,
    )


def _make_finding_critical() -> Finding:
    return Finding(
        rule_id="CPU-SATURATION-01",
        severity=Severity.CRITICAL,
        subject="全体",
        summary="PSI CPU some10 > 20% が 2 時間継続",
        evidence={"psi_avg": 35.2},
        detected_at=(time.time() - 7200, time.time()),
        confidence=Confidence.HIGH,
    )


def _healthy_diag() -> Diagnosis:
    return Diagnosis(
        findings=[],
        healthy=True,
        reasoning=["MEM-LEAK-01: slope=0, r2=0.01 → 閾値未達"],
        quality=_make_quality(),
    )


def _finding_diag(verbose_proposal: bool = False) -> Diagnosis:
    proposal_text = ""
    if verbose_proposal:
        proposal_text = (
            "[検出] mysqld の RSS が右肩上がりです\n"
            "[根拠] slope=+38MB/h, R²=0.97, 観測18h\n"
            "[暫定対処] mysqld を再起動して RSS をリセット\n"
            "[恒久対処] メモリリークのあるクエリキャッシュ設定を見直す\n"
            "[影響予測] 対処しなければ 24h 以内に OOM が発生する可能性"
        )
    return Diagnosis(
        findings=[
            _make_finding(proposal=proposal_text),
            _make_finding(
                rule_id="DISK-FULL-01",
                severity=Severity.WARNING,
                confidence=Confidence.MEDIUM,
                related=["DISK-GROWTH-01"],
            ),
        ],
        healthy=False,
        reasoning=[
            "MEM-LEAK-01: slope=+38MB/h, r2=0.97 → 検出",
            "DISK-FULL-01: 残り 9.2 日 → 検出",
        ],
        quality=_make_quality(),
    )


def _charts_empty() -> ChartData:
    return ChartData()


def _charts_with_data() -> ChartData:
    now = time.time()
    ts_list = [now - (60 - i) * 60 for i in range(60)]
    cpu = [(ts, 20.0 + i * 0.5) for i, ts in enumerate(ts_list)]
    mem = [(ts, 1_000_000_000 + i * 100_000) for i, ts in enumerate(ts_list)]
    psi = [(ts, 1.0, 0.5, 2.0) for ts in ts_list]
    mounts = {"/": [(ts, 45.0 + i * 0.1) for i, ts in enumerate(ts_list)]}
    spikes = [{"ts": now - 3600, "trigger_type": "cpu", "psi_snapshot": '{"cpu": 25}'}]
    return ChartData(
        cpu=cpu, cpu_max_core=cpu, mem_used=mem, psi=psi, mounts=mounts, spikes=spikes
    )


# ---------------------------------------------------------------------------
# render_terminal tests
# ---------------------------------------------------------------------------


class TestRenderTerminal:
    def test_healthy_message(self) -> None:
        out = render_terminal(_healthy_diag(), color=False)
        assert "問題は見つかりませんでした" in out

    def test_finding_headline(self) -> None:
        out = render_terminal(_finding_diag(), color=False)
        assert "MEM-LEAK-01" in out
        assert "mysqld" in out

    def test_diagnosis_section_present(self) -> None:
        out = render_terminal(_finding_diag(), color=False)
        assert "━━━ 診断 (Diagnosis) ━━━" in out

    def test_confidence_ja(self) -> None:
        out = render_terminal(_finding_diag(), color=False)
        assert "確信度: 高" in out

    def test_color_false_no_ansi(self) -> None:
        out = render_terminal(_finding_diag(), color=False)
        assert "\033[" not in out

    def test_color_true_has_ansi(self) -> None:
        out = render_terminal(_finding_diag(), color=True)
        assert "\033[" in out

    def test_verbose_reasoning(self) -> None:
        out = render_terminal(_finding_diag(), color=False, verbose=True)
        assert "推論過程:" in out
        assert "MEM-LEAK-01" in out

    def test_verbose_false_no_reasoning_section(self) -> None:
        out = render_terminal(_finding_diag(), color=False, verbose=False)
        assert "推論過程:" not in out

    def test_quality_block_present(self) -> None:
        out = render_terminal(_finding_diag(), color=False)
        assert "観測品質" in out
        assert "12.0" in out  # span_hours

    def test_related_shown(self) -> None:
        diag = _finding_diag()
        out = render_terminal(diag, color=False)
        assert "DISK-GROWTH-01" in out

    def test_critical_severity(self) -> None:
        diag = Diagnosis(
            findings=[_make_finding_critical()],
            healthy=False,
            quality=_make_quality(),
        )
        out = render_terminal(diag, color=False)
        assert "CPU-SATURATION-01" in out
        assert "✘" in out

    def test_proposal_lines_shown(self) -> None:
        diag = _finding_diag(verbose_proposal=True)
        out = render_terminal(diag, color=False)
        assert "[検出]" in out
        assert "[恒久対処]" in out


# ---------------------------------------------------------------------------
# render_json tests
# ---------------------------------------------------------------------------


class TestRenderJson:
    def test_round_trip(self) -> None:
        out = render_json(_finding_diag())
        parsed = json.loads(out)
        assert isinstance(parsed, dict)

    def test_required_keys(self) -> None:
        parsed = json.loads(render_json(_finding_diag()))
        for key in (
            "healthy",
            "findings",
            "quality",
            "reasoning",
            "generated_at",
            "procdiag_version",
        ):
            assert key in parsed, f"キー '{key}' が見つからない"

    def test_healthy_key_type(self) -> None:
        parsed = json.loads(render_json(_healthy_diag()))
        assert parsed["healthy"] is True

    def test_findings_list(self) -> None:
        parsed = json.loads(render_json(_finding_diag()))
        assert isinstance(parsed["findings"], list)
        assert len(parsed["findings"]) == 2

    def test_finding_keys(self) -> None:
        parsed = json.loads(render_json(_finding_diag()))
        f = parsed["findings"][0]
        for key in (
            "rule_id",
            "severity",
            "subject",
            "summary",
            "evidence",
            "detected_at",
            "confidence",
        ):
            assert key in f

    def test_no_unicode_escape(self) -> None:
        """日本語がサロゲートや \\uXXXX エスケープにならないこと"""
        out = render_json(_finding_diag())
        assert "\\u" not in out
        assert "mysqld" in out

    def test_quality_keys(self) -> None:
        parsed = json.loads(render_json(_finding_diag()))
        q = parsed["quality"]
        assert "span_hours" in q
        assert "gap_seconds" in q

    def test_indent_2(self) -> None:
        out = render_json(_finding_diag())
        # indent=2 means lines after first are indented
        assert "\n  " in out


# ---------------------------------------------------------------------------
# render_html tests
# ---------------------------------------------------------------------------


class TestRenderHtml:
    def test_no_external_urls(self) -> None:
        out = render_html(_finding_diag(), _charts_with_data())
        # SVG namespace URI (xmlns="http://www.w3.org/2000/svg") is not an external
        # resource reference, so we check for actual resource-loading patterns only.
        # Remove SVG xmlns attributes before checking for external URLs.
        stripped = out.replace('xmlns="http://www.w3.org/2000/svg"', "")
        assert "http://" not in stripped
        assert "https://" not in stripped

    def test_no_external_script_src(self) -> None:
        out = render_html(_finding_diag(), _charts_with_data())
        assert "<script src" not in out

    def test_no_external_link_href(self) -> None:
        out = render_html(_finding_diag(), _charts_with_data())
        # <link href= is forbidden (CDN CSS)
        assert "<link" not in out

    def test_svg_present_with_findings(self) -> None:
        out = render_html(_finding_diag(), _charts_with_data())
        assert "<svg" in out

    def test_svg_present_healthy(self) -> None:
        """SVG should be rendered even when there are no findings."""
        out = render_html(_healthy_diag(), _charts_with_data())
        assert "<svg" in out

    def test_svg_present_no_charts(self) -> None:
        """render_html with charts=None still produces SVG 'データなし' elements."""
        out = render_html(_finding_diag(), None)
        # No charts supplied; the chart section shows 'チャートデータなし'
        assert "チャートデータなし" in out

    def test_quality_numbers_appear(self) -> None:
        diag = Diagnosis(
            findings=[],
            healthy=True,
            quality=ObservationQuality(
                span_hours=24.5,
                gap_seconds=60.0,
                missing_io_pct=0.25,
                self_cpu_avg=0.4,
                self_cpu_max=1.2,
                throttle_events=3,
                notes=["テスト備考"],
            ),
        )
        out = render_html(diag, _charts_empty())
        assert "24.5" in out
        assert "60" in out
        assert "25.0" in out  # missing_io_pct * 100
        assert "0.40" in out  # self_cpu_avg
        assert "1.20" in out  # self_cpu_max
        assert "3" in out
        assert "テスト備考" in out

    def test_hostname_label_in_title(self) -> None:
        out = render_html(_healthy_diag(), hostname_label="L3ライン制御PC")
        assert "L3ライン制御PC" in out

    def test_summary_section_present(self) -> None:
        out = render_html(_finding_diag(), _charts_empty())
        assert "サマリ" in out

    def test_proposal_section_present(self) -> None:
        out = render_html(_finding_diag(verbose_proposal=True), _charts_empty())
        assert "提案詳細" in out
        assert "[検出]" in out

    def test_spike_section_present(self) -> None:
        out = render_html(_finding_diag(), _charts_with_data())
        assert "スパイクイベント" in out
        # The spike has trigger_type='cpu'
        assert "cpu" in out

    def test_no_related_section_when_empty(self) -> None:
        diag = Diagnosis(
            findings=[_make_finding()],
            healthy=False,
            quality=_make_quality(),
        )
        out = render_html(diag, _charts_empty())
        assert "関連する検出" not in out

    def test_related_shown_when_present(self) -> None:
        diag = _finding_diag()
        out = render_html(diag, _charts_empty())
        assert "DISK-GROWTH-01" in out


# ---------------------------------------------------------------------------
# build_charts tests
# ---------------------------------------------------------------------------


class TestBuildCharts:
    def test_empty_db_no_exception(self, tmp_path) -> None:
        db = tmp_path / "test.db"
        with Storage(db) as store:
            charts = build_charts(store)
        assert isinstance(charts, ChartData)
        assert charts.cpu == []
        assert charts.mem_used == []
        assert charts.mounts == {}
        assert charts.spikes == []

    def test_sys_samples_series(self, tmp_path) -> None:
        db = tmp_path / "test.db"
        base_ts = time.time() - 300
        with Storage(db) as store:
            rows = []
            for i in range(10):
                ts = base_ts + i * 30
                rows.append(
                    (
                        ts,
                        20.0 + i,
                        30.0 + i,  # cpu_total, cpu_max_core
                        1_000_000 + i * 1000,
                        500_000,  # mem_used, mem_available
                        0,
                        0,
                        0,  # swap_used, swap_in, swap_out
                        0.5,  # load1
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,  # psi_*
                        0,
                        0,  # disk_read_bytes, disk_write_bytes
                        0.1,
                        10_000_000,  # self_cpu, self_rss
                    )
                )
            store.buffer("sys_samples", rows)
            store.flush()

            charts = build_charts(store)

        assert len(charts.cpu) == 10
        assert charts.cpu[0][1] == pytest.approx(20.0)
        assert len(charts.cpu_max_core) == 10
        assert len(charts.mem_used) == 10

    def test_disk_samples_series(self, tmp_path) -> None:
        db = tmp_path / "test.db"
        base_ts = time.time() - 300
        with Storage(db) as store:
            rows = []
            for i in range(5):
                ts = base_ts + i * 60
                rows.append((ts, "/", 100_000_000_000, 50_000_000_000 + i * 1_000_000))
            store.buffer("disk_samples", rows)
            store.flush()

            charts = build_charts(store)

        assert "/" in charts.mounts
        assert len(charts.mounts["/"]) == 5
        # usage should be ~50%
        _, pct = charts.mounts["/"][0]
        assert 49.0 <= pct <= 51.0

    def test_since_filter(self, tmp_path) -> None:
        db = tmp_path / "test.db"
        now = time.time()
        with Storage(db) as store:
            rows = []
            for i in range(20):
                ts = now - (20 - i) * 60
                rows.append(
                    (
                        ts,
                        10.0,
                        15.0,
                        500_000,
                        500_000,
                        0,
                        0,
                        0,
                        0.1,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0,
                        0,
                        0.0,
                        0,
                    )
                )
            store.buffer("sys_samples", rows)
            store.flush()

            # Only last 5 minutes
            since = now - 5 * 60
            charts = build_charts(store, since=since)

        assert all(ts >= since for ts, _ in charts.cpu)

    def test_spike_events_loaded(self, tmp_path) -> None:
        db = tmp_path / "test.db"
        now = time.time()
        with Storage(db) as store:
            store.conn.execute(
                "INSERT INTO spike_events (ts, trigger_type, psi_snapshot, top_processes, top_cgroups) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, "cpu", '{"cpu_some10": 35}', "[]", "[]"),
            )
            store.conn.commit()
            charts = build_charts(store)

        assert len(charts.spikes) == 1
        assert charts.spikes[0]["trigger_type"] == "cpu"


# ---------------------------------------------------------------------------
# SVG downsampling tests
# ---------------------------------------------------------------------------


class TestSvgDownsample:
    def test_large_series_output_size(self) -> None:
        """10,000 points → output must be < 500 KB."""
        from procdiag.report import _polyline_svg

        now = time.time()
        series = [(now + i, float(i % 100)) for i in range(10_000)]
        svg = _polyline_svg(series, label="テスト", unit="%")
        assert len(svg.encode("utf-8")) < 500_000

    def test_empty_series_svg(self) -> None:
        from procdiag.report import _polyline_svg

        svg = _polyline_svg([], label="空データ", unit="%")
        assert "データなし" in svg
        assert "<svg" in svg

    def test_single_point_no_crash(self) -> None:
        from procdiag.report import _polyline_svg

        svg = _polyline_svg([(time.time(), 42.0)], label="1点", unit="%")
        assert "<svg" in svg

    def test_psi_svg_three_series(self) -> None:
        from procdiag.report import _psi_svg

        now = time.time()
        psi = [(now + i, float(i), float(i) * 0.5, float(i) * 0.1) for i in range(60)]
        svg = _psi_svg(psi)
        # All three palette colours must appear
        for col in ["#4a90d9", "#e06c2b", "#2ba56c"]:
            assert col in svg

    def test_psi_svg_empty_no_crash(self) -> None:
        from procdiag.report import _psi_svg

        svg = _psi_svg([])
        assert "データなし" in svg


# ---------------------------------------------------------------------------
# build_charts robustness (issue #5 §4-1)
# ---------------------------------------------------------------------------


class TestBuildChartsRobustness:
    def test_resilient_to_db_error_returns_empty(self, tmp_path) -> None:
        db = tmp_path / "r.db"
        with Storage(db):
            pass  # create schema
        ro = Storage(db, read_only=True)
        ro.close()  # subsequent reads raise sqlite3.ProgrammingError
        # An expected DB error is caught → partial (empty) charts, never raises.
        charts = build_charts(ro)
        assert charts.cpu == []
        assert charts.mounts == {}

    def test_propagates_unexpected_error(self, tmp_path, monkeypatch) -> None:
        db = tmp_path / "r.db"
        with Storage(db):
            pass
        with Storage(db, read_only=True) as ro:

            def _boom(*_a, **_k):
                raise ValueError("unexpected boom")

            monkeypatch.setattr(ro, "series", _boom)
            # A non-DB error must propagate, not be silently swallowed.
            with pytest.raises(ValueError):
                build_charts(ro)
