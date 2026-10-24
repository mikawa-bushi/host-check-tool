"""Tests for procdiag.core — result types, sampler base, rule base, and util."""

from __future__ import annotations

import pytest

from procdiag.config import ProcdiagConfig
from procdiag.core.result import (
    Confidence,
    Diagnosis,
    Finding,
    ObservationQuality,
    Severity,
    Window,
)
from procdiag.core.rule import _REGISTRY, Rule, all_rules, get_rule, register
from procdiag.core.sampler import (
    _SAMPLER_REGISTRY,
    RunContext,
    SampleBatch,
    Sampler,
    all_samplers,
    get_sampler,
    register_sampler,
)
from procdiag.core.util import (
    humanize_bytes,
    humanize_duration,
    normalize_cmdline,
    parse_duration,
)

# ---------------------------------------------------------------------------
# Severity / Finding.to_dict
# ---------------------------------------------------------------------------


def test_severity_values():
    assert Severity.INFO.value == "info"
    assert Severity.WARNING.value == "warning"
    assert Severity.CRITICAL.value == "critical"


def test_severity_symbols():
    assert Severity.INFO.symbol == "ℹ"
    assert Severity.WARNING.symbol == "⚠"
    assert Severity.CRITICAL.symbol == "✘"


def test_severity_str_mixin():
    # str mix-in means Severity can be compared to a plain string
    assert Severity.INFO == "info"
    assert Severity.WARNING == "warning"


def test_confidence_values():
    assert Confidence.LOW.value == "low"
    assert Confidence.MEDIUM.value == "medium"
    assert Confidence.HIGH.value == "high"


def test_window_span_hours():
    w = Window(start_ts=0.0, end_ts=7200.0)
    assert w.span_hours == 2.0


def test_finding_to_dict_expands_enums():
    f = Finding(
        rule_id="MEM-LEAK-01",
        severity=Severity.WARNING,
        subject="mysqld",
        summary="メモリリーク疑い",
        evidence={"slope_mb_per_h": 38.5, "r2": 0.97},
        detected_at=(1000.0, 4000.0),
        confidence=Confidence.HIGH,
    )
    d = f.to_dict()
    assert d["severity"] == "warning"  # Enum expanded to value
    assert d["confidence"] == "high"
    assert d["rule_id"] == "MEM-LEAK-01"
    assert d["subject"] == "mysqld"
    assert d["evidence"]["r2"] == 0.97
    assert d["detected_at"] == [1000.0, 4000.0]
    assert d["related"] == []
    assert d["proposal"] == ""


def test_finding_to_dict_is_json_serialisable():
    import json

    f = Finding(
        rule_id="DISK-FULL-01",
        severity=Severity.CRITICAL,
        subject="/var",
        summary="ディスク満杯予測",
        evidence={"days_remaining": 9.2},
        detected_at=(0.0, 3600.0),
        confidence=Confidence.MEDIUM,
    )
    # Should not raise
    json.dumps(f.to_dict())


def test_observation_quality_to_dict():
    q = ObservationQuality(span_hours=24.5, gap_seconds=300.0, notes=["PSI非対応"])
    d = q.to_dict()
    assert d["span_hours"] == 24.5
    assert d["gap_seconds"] == 300.0
    assert d["notes"] == ["PSI非対応"]


def test_diagnosis_primary_returns_first_finding():
    f1 = Finding(
        rule_id="A",
        severity=Severity.CRITICAL,
        subject="proc",
        summary="critical",
        evidence={},
        detected_at=(0.0, 1.0),
        confidence=Confidence.HIGH,
    )
    f2 = Finding(
        rule_id="B",
        severity=Severity.WARNING,
        subject="proc",
        summary="warning",
        evidence={},
        detected_at=(0.0, 1.0),
        confidence=Confidence.LOW,
    )
    diag = Diagnosis(findings=[f1, f2], healthy=False)
    assert diag.primary is f1


def test_diagnosis_primary_none_when_healthy():
    diag = Diagnosis(findings=[], healthy=True)
    assert diag.primary is None


# ---------------------------------------------------------------------------
# Sampler.run defensive wall
# ---------------------------------------------------------------------------


class _BrokenSampler(Sampler):
    name = "_broken_test_sampler"
    tier = "T1"

    def collect(self, ctx):
        raise RuntimeError("simulated collect failure")


def _make_ctx():
    cfg = ProcdiagConfig()
    return RunContext(cfg)


def test_sampler_run_catches_exception_and_returns_empty():
    sampler = _BrokenSampler()
    ctx = _make_ctx()
    result = sampler.run(ctx)
    assert result == []
    assert any("_broken_test_sampler" in note for note in ctx.notes)
    assert any("collect失敗" in note for note in ctx.notes)


def test_sampler_run_returns_batches_on_success():
    class _GoodSampler(Sampler):
        name = "_good_test_sampler"
        tier = "T1"

        def collect(self, ctx):
            return [SampleBatch(table="sys_samples", rows=[(1.0,) + (None,) * 17])]

    sampler = _GoodSampler()
    ctx = _make_ctx()
    batches = sampler.run(ctx)
    assert len(batches) == 1
    assert batches[0].table == "sys_samples"


# ---------------------------------------------------------------------------
# Sampler registry
# ---------------------------------------------------------------------------


def test_register_sampler_decorator():
    @register_sampler
    class _TestSampler(Sampler):
        name = "_registry_test_sampler_01"

    assert "_registry_test_sampler_01" in all_samplers()
    assert get_sampler("_registry_test_sampler_01") is _TestSampler

    # Clean up so we don't pollute other tests
    _SAMPLER_REGISTRY.pop("_registry_test_sampler_01", None)


def test_register_sampler_requires_name():
    with pytest.raises(ValueError, match="name"):

        @register_sampler
        class _NoName(Sampler):
            name = ""


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------


def test_register_rule_decorator():
    @register
    class _TestRule(Rule):
        rule_id = "_registry_test_rule_01"
        category = "memory"

    assert "_registry_test_rule_01" in all_rules()
    assert get_rule("_registry_test_rule_01") is _TestRule

    # Clean up
    _REGISTRY.pop("_registry_test_rule_01", None)


def test_register_rule_requires_rule_id():
    with pytest.raises(ValueError, match="rule_id"):

        @register
        class _NoId(Rule):
            rule_id = ""


def test_rule_evaluate_raises_not_implemented():
    class _UnimplementedRule(Rule):
        rule_id = "_unimpl"

    with pytest.raises(NotImplementedError):
        _UnimplementedRule().evaluate(None, None)


# ---------------------------------------------------------------------------
# util.parse_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "s, expected",
    [
        ("60s", 60.0),
        ("1h", 3600.0),
        ("1.5h", 5400.0),
        ("24h", 86400.0),
        ("90d", 90 * 86400.0),
        ("2d", 2 * 86400.0),
        ("30m", 1800.0),
    ],
)
def test_parse_duration_valid(s, expected):
    assert parse_duration(s) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["abc", "1x", "", "1.2.3h", "-1h"])
def test_parse_duration_invalid(bad):
    with pytest.raises(ValueError, match="期間"):
        parse_duration(bad)


# ---------------------------------------------------------------------------
# util.humanize_bytes
# ---------------------------------------------------------------------------


def test_humanize_bytes_mb():
    # 1.5e6 / 1024**2 = 1.43 (binary) vs 1.5 (SI) — discriminates the base.
    assert humanize_bytes(1_500_000) == "1.4MB"


def test_humanize_bytes_zero():
    assert humanize_bytes(0) == "0B"


def test_humanize_bytes_none():
    assert humanize_bytes(None) == "—"


def test_humanize_bytes_gb():
    # Binary (1024) base to match df -h / free -h: 2.5e9 / 1024**3 ≈ 2.33.
    assert humanize_bytes(2_500_000_000) == "2.3GB"


def test_humanize_bytes_binary_base():
    assert humanize_bytes(1024**3) == "1.0GB"
    assert humanize_bytes(1024**2) == "1.0MB"
    assert humanize_bytes(1024) == "1.0KB"


# ---------------------------------------------------------------------------
# util.humanize_duration
# ---------------------------------------------------------------------------


def test_humanize_duration_days_hours():
    assert humanize_duration(93784) == "1日2時間"


def test_humanize_duration_hours_minutes():
    assert humanize_duration(3661) == "1時間1分"


def test_humanize_duration_seconds_only():
    assert humanize_duration(45) == "45秒"


def test_humanize_duration_zero():
    assert humanize_duration(0) == "0秒"


# ---------------------------------------------------------------------------
# util.normalize_cmdline
# ---------------------------------------------------------------------------


def test_normalize_cmdline_replaces_digits():
    h1, _sample = normalize_cmdline(["python", "app.py", "--pid", "12345"])
    h2, _sample2 = normalize_cmdline(["python", "app.py", "--pid", "99999"])
    # Different PIDs → same hash after digit replacement
    assert h1 == h2


def test_normalize_cmdline_stability():
    """Same cmdline must always produce the same hash."""
    h1, _ = normalize_cmdline(["mysqld", "--port", "3306"])
    h2, _ = normalize_cmdline(["mysqld", "--port", "3306"])
    assert h1 == h2


def test_normalize_cmdline_different_commands():
    h_mysql, _ = normalize_cmdline(["mysqld"])
    h_nginx, _ = normalize_cmdline(["nginx"])
    assert h_mysql != h_nginx


def test_normalize_cmdline_no_replace_digits():
    h1, _ = normalize_cmdline(["proc", "123"], replace_digits=False)
    h2, _ = normalize_cmdline(["proc", "456"], replace_digits=False)
    # Without replacement, different numbers → different hashes
    assert h1 != h2


def test_normalize_cmdline_empty():
    h, sample = normalize_cmdline([])
    assert isinstance(h, str)
    assert len(h) == 12


def test_normalize_cmdline_hash_length():
    h, _ = normalize_cmdline(["some", "command"])
    assert len(h) == 12


# ---------------------------------------------------------------------------
# diagnosis.memory._is_python_process (issue #6 §5-2)
# ---------------------------------------------------------------------------


def test_is_python_process_matches_only_python():
    from procdiag.diagnosis.memory import _is_python_process

    assert _is_python_process("python")
    assert _is_python_process("python3")
    assert _is_python_process("Python3.11")
    # The old "py" substring heuristic wrongly matched these:
    assert not _is_python_process("copy")
    assert not _is_python_process("scapy")
    assert not _is_python_process("happy")
    assert not _is_python_process("pypy")


# ---------------------------------------------------------------------------
# CounterDelta — cumulative counter -> clamped per-tick delta
# ---------------------------------------------------------------------------


def test_counter_delta_first_tick_none_then_delta():
    from procdiag.core.util import CounterDelta

    d = CounterDelta()
    assert d.update("a", 100) is None
    assert d.update("a", 150) == 50
    assert d.update("b", 7) is None  # keys are independent


def test_counter_delta_clamps_counter_reset_to_zero():
    from procdiag.core.util import CounterDelta

    d = CounterDelta()
    d.update(("unit", "cpu"), 10_000)
    assert d.update(("unit", "cpu"), 20) == 0  # unit restarted: counter reset
    assert d.update(("unit", "cpu"), 50) == 30  # resumes from the new baseline
