from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import procdiag.diagnosis  # noqa: F401
from procdiag.compare import (
    Clear,
    CompareError,
    Detected,
    Evaluated,
    Note,
    NoteKind,
    Period,
    PeriodsRequest,
    Side,
    SideName,
    SplitRequest,
    Unknown,
    Verdict,
    Why,
    build_comparison,
    check_order,
    classify,
    exit_code,
    parse_instant,
    plan_request,
)
from procdiag.config import ProcdiagConfig
from procdiag.core import (
    Confidence,
    CoverageGap,
    Diagnosis,
    Finding,
    Measurement,
    Observation,
    Severity,
    SkippedRule,
    UncoveredRule,
    Window,
)

_CFG = ProcdiagConfig()
_W = Window(0.0, 7 * 86400.0)


def _finding(rule_id, subject, severity=Severity.WARNING, **evidence):
    return Finding(
        rule_id=rule_id,
        severity=severity,
        subject=subject,
        summary=f"{subject}: test",
        evidence=evidence,
        detected_at=(0.0, 1.0),
        confidence=Confidence.MEDIUM,
    )


def _leak(subject, slope, *, identity=None, fires=None, r2=0.99, severity=None):
    obs = Observation(
        subject=subject,
        identity=identity or f"hash_{subject}",
        value=slope,
        evidence={"proc_id": 1, "r2": r2, "n": 600, "span_hours": 168.0},
    )
    fires = slope >= 1.0 and r2 >= 0.9 if fires is None else fires
    finding = (
        _finding(
            "MEM-LEAK-01",
            subject,
            severity or Severity.WARNING,
            slope_mb_per_h=slope,
        )
        if fires
        else None
    )
    return Measurement(obs, finding)


def _diag(*, measurements=None, findings=None, folded=None, **kw):
    measurements = measurements if measurements is not None else {}
    found = [m.finding for ms in measurements.values() for m in ms if m.finding]
    findings = found + (findings or [])
    return Diagnosis(
        findings=findings,
        healthy=not findings,
        evaluated_rules=kw.pop("evaluated", ["MEM-LEAK-01", *measurements]),
        measurements=measurements,
        folded=folded or [],
        **kw,
    )


def _side(name, diag, *, db="one.db", raw_hours=168.0, **meta):
    return Side(
        name=name,
        db=Path(db),
        hostname=meta.get("hostname", "host-a"),
        schema_version=meta.get("schema_version", "4"),
        recording_config=meta.get("recording_config", "{}"),
        requested=Period(None, None),
        result=None if diag is None else Evaluated(_W, raw_hours, diag),
    )


def _compare(before, after, **kw):
    return build_comparison(
        _side(SideName.BEFORE, before, **kw.get("b", {})),
        _side(SideName.AFTER, after, **kw.get("a", {})),
        _CFG,
    )


def _leak_cmp(before_ms, after_ms, **kw):
    return _compare(
        _diag(measurements={"MEM-LEAK-01": before_ms}),
        _diag(measurements={"MEM-LEAK-01": after_ms}),
        **kw,
    )


class TestMeasuredRows:
    def test_leak_fixed_shows_both_values_and_resolves(self):
        cmp = _leak_cmp([_leak("myapp", 38.0)], [_leak("myapp", 0.2, r2=0.04)])
        (row,) = cmp.rows
        assert row.verdict is Verdict.RESOLVED and row.why is Why.FINDING_CLEARED
        assert isinstance(row.before, Detected) and row.before.value == 38.0
        assert row.before.value == row.before.finding.evidence["slope_mb_per_h"]
        assert isinstance(row.after, Clear) and row.after.value == 0.2
        assert row.after.evidence["r2"] == 0.04
        assert row.headline.unit == "MB/h" and row.threshold == 1.0
        assert exit_code(cmp) == 0

    def test_clear_above_half_the_threshold_is_only_improved(self):
        (row,) = _leak_cmp(
            [_leak("myapp", 38.0)], [_leak("myapp", 0.9, fires=False)]
        ).rows
        assert row.verdict is Verdict.IMPROVED and row.why is Why.BELOW_THRESHOLD
        assert row.label == "改善 (しきい値未満)"

    def test_declined_but_still_steep_is_not_an_improvement(self):
        (row,) = _leak_cmp([_leak("myapp", 38.0)], [_leak("myapp", 60.0, r2=0.3)]).rows
        assert row.verdict is Verdict.WORSE and row.why is Why.VALUE_WORSE

    def test_threshold_comes_from_the_configured_rule(self):
        cfg = ProcdiagConfig.model_validate(
            {"rules": {"overrides": {"MEM-LEAK-01": {"slope_warning_mb_h": 5.0}}}}
        )
        cmp = build_comparison(
            _side(
                SideName.BEFORE, _diag(measurements={"MEM-LEAK-01": [_leak("a", 9)]})
            ),
            _side(
                SideName.AFTER,
                _diag(measurements={"MEM-LEAK-01": [_leak("a", 2.4, fires=False)]}),
            ),
            cfg,
        )
        assert cmp.rows[0].threshold == 5.0
        assert cmp.rows[0].verdict is Verdict.RESOLVED

    def test_still_leaking_slower_is_improved_and_exits_1(self):
        cmp = _leak_cmp([_leak("myapp", 38.0)], [_leak("myapp", 20.0)])
        assert cmp.rows[0].verdict is Verdict.IMPROVED
        assert cmp.rows[0].why is Why.VALUE_BETTER
        assert exit_code(cmp) == 1

    def test_small_change_while_firing_is_unchanged(self):
        cmp = _leak_cmp([_leak("myapp", 38.0)], [_leak("myapp", 35.0)])
        assert cmp.rows[0].verdict is Verdict.UNCHANGED

    def test_severity_decides_before_the_value(self):
        cmp = _leak_cmp(
            [_leak("myapp", 60.0, severity=Severity.CRITICAL)],
            [_leak("myapp", 55.0, severity=Severity.WARNING)],
        )
        assert cmp.rows[0].why is Why.SEVERITY_LOWER

    def test_subject_gone_after_is_not_evaluable(self):
        cmp = _leak_cmp([_leak("myapp", 38.0)], [_leak("other", 0.1)])
        (row,) = cmp.rows
        assert row.verdict is Verdict.NOT_EVALUABLE
        assert row.why is Why.SUBJECT_NOT_OBSERVED
        assert exit_code(cmp) == 3

    def test_too_short_after_is_not_evaluable_never_resolved(self):
        after = _diag(
            evaluated=[],
            skipped_rules=[SkippedRule("MEM-LEAK-01", "メモリリーク疑い", 6.0, 3.0)],
        )
        cmp = _compare(_diag(measurements={"MEM-LEAK-01": [_leak("myapp", 38)]}), after)
        (row,) = cmp.rows
        assert isinstance(row.after, Unknown) and row.after.why is Why.TOO_SHORT
        assert "あと 3.0 時間" in row.after.detail
        assert exit_code(cmp) == 3

    def test_info_before_that_cannot_be_judged_does_not_force_3(self):
        zombie = Measurement(
            Observation("zombie_processes", None, 0.5, {"r2": 0.9}),
            _finding("ZOMBIE-01", "zombie_processes", Severity.INFO),
        )
        after = _diag(
            evaluated=[],
            uncovered_rules=[
                UncoveredRule(
                    "ZOMBIE-01", "z", CoverageGap.NOT_RECORDED, ("proc_samples",)
                )
            ],
        )
        cmp = _compare(_diag(measurements={"ZOMBIE-01": [zombie]}), after)
        assert cmp.rows[0].verdict is Verdict.NOT_EVALUABLE
        assert cmp.rows[0].after.why is Why.NOT_RECORDED
        assert "保持期間" in cmp.rows[0].after.detail
        assert exit_code(cmp) == 0


class TestIdentityMatching:
    def test_same_name_different_cmdlines_stay_apart(self):
        cmp = _leak_cmp(
            [
                _leak("python3", 38.0, identity="h1"),
                _leak("python3", 0.0, identity="h2"),
            ],
            [
                _leak("python3", 0.1, identity="h1"),
                _leak("python3", 30.0, identity="h2"),
            ],
        )
        by_identity = {r.identity: r for r in cmp.rows}
        assert by_identity["h1"].verdict is Verdict.RESOLVED
        assert by_identity["h1"].before.value == 38.0
        assert by_identity["h2"].verdict is Verdict.NEW

    def test_a_changed_cmdline_matches_by_name_when_unambiguous(self):
        cmp = _leak_cmp(
            [_leak("myapp", 38.0, identity="v1")], [_leak("myapp", 0.1, identity="v2")]
        )
        assert cmp.rows[0].verdict is Verdict.RESOLVED

    def test_ambiguous_names_are_not_guessed(self):
        cmp = _leak_cmp(
            [_leak("myapp", 38.0, identity="v1")],
            [_leak("myapp", 0.1, identity="v2"), _leak("myapp", 0.2, identity="v3")],
        )
        (row,) = cmp.rows
        assert row.after.why is Why.SUBJECT_NOT_OBSERVED


class TestPlainRows:
    def test_finding_level_transitions(self):
        before = _diag(
            findings=[_finding("DISK-FULL-01", "/var"), _finding("OOM-KILL-01", "db")],
            evaluated=["DISK-FULL-01", "OOM-KILL-01", "CPU-SATURATION-01"],
        )
        after = _diag(
            findings=[_finding("CPU-SATURATION-01", "busy")],
            evaluated=["DISK-FULL-01", "CPU-SATURATION-01"],
            uncovered_rules=[
                UncoveredRule(
                    "OOM-KILL-01", "x", CoverageGap.NOT_RECORDED, ("proc_samples",)
                )
            ],
        )
        rows = {r.rule_id: r for r in _compare(before, after).rows}
        assert rows["DISK-FULL-01"].verdict is Verdict.RESOLVED
        assert rows["DISK-FULL-01"].after == Clear(None)
        assert rows["CPU-SATURATION-01"].verdict is Verdict.NEW
        assert rows["OOM-KILL-01"].verdict is Verdict.NOT_EVALUABLE

    def test_finding_after_a_side_that_could_not_see_it_is_not_new(self):
        before = _diag(
            evaluated=[],
            uncovered_rules=[
                UncoveredRule(
                    "CPU-SATURATION-01", "x", CoverageGap.NOT_RECORDED, ("sys_samples",)
                )
            ],
        )
        after = _diag(findings=[_finding("CPU-SATURATION-01", "busy")])
        (row,) = _compare(before, after).rows
        assert row.verdict is Verdict.NOT_EVALUABLE and row.why is Why.NOT_RECORDED

    def test_folded_child_on_one_side_is_not_new(self):
        growth = _finding("DISK-GROWTH-01", "/var", Severity.INFO)
        before = _diag(
            findings=[_finding("DISK-FULL-01", "/var")],
            folded=[growth],
            evaluated=["DISK-FULL-01", "DISK-GROWTH-01"],
        )
        after = _diag(
            findings=[_finding("DISK-GROWTH-01", "/var", Severity.INFO)],
            evaluated=["DISK-FULL-01", "DISK-GROWTH-01"],
        )
        rows = {r.rule_id: r for r in _compare(before, after).rows}
        assert rows["DISK-GROWTH-01"].verdict is Verdict.UNCHANGED
        assert rows["DISK-FULL-01"].verdict is Verdict.RESOLVED

    def test_rows_are_sorted_problems_first(self):
        before = _diag(
            findings=[_finding("DISK-FULL-01", "/var")], evaluated=["DISK-FULL-01"]
        )
        after = _diag(
            findings=[_finding("DISK-FULL-01", "/srv")], evaluated=["DISK-FULL-01"]
        )
        assert [r.verdict for r in _compare(before, after).rows] == [
            Verdict.NEW,
            Verdict.RESOLVED,
        ]


class TestClassifyTable:
    det = Detected(Severity.WARNING, 1, 10.0, _finding("X", "s"))
    unk = Unknown(Why.TOO_SHORT, "")

    @pytest.mark.parametrize(
        "before, after, expected",
        [
            (det, Clear(None), (Verdict.RESOLVED, Why.FINDING_CLEARED)),
            (det, unk, (Verdict.NOT_EVALUABLE, Why.TOO_SHORT)),
            (Clear(None), det, (Verdict.NEW, Why.FINDING_APPEARED)),
            (unk, det, (Verdict.NOT_EVALUABLE, Why.TOO_SHORT)),
            (det, det, (Verdict.UNCHANGED, Why.WITHIN_TOLERANCE)),
        ],
    )
    def test_states(self, before, after, expected):
        assert classify(before, after, 1.0) == expected

    def test_a_row_needs_a_detected_side(self):
        with pytest.raises(ValueError):
            classify(Clear(1.0), Clear(1.0), 1.0)


class TestNotesAndExitCodes:
    def test_two_dbs_from_different_hosts_and_schemas_get_notes(self):
        cmp = _compare(
            _diag(),
            _diag(),
            b={"db": "a.db", "hostname": "pc01", "schema_version": "3"},
            a={"db": "b.db", "hostname": "pc02", "recording_config": '{"x": 1}'},
        )
        kinds = {n.kind for n in cmp.notes}
        assert kinds == {
            NoteKind.HOSTNAME_DIFFERS,
            NoteKind.SCHEMA_DIFFERS,
            NoteKind.RECORDING_CONFIG_DIFFERS,
        }
        host = next(n for n in cmp.notes if n.kind is NoteKind.HOSTNAME_DIFFERS)
        assert host.message == "記録ホストが異なります (修正前: pc01 / 修正後: pc02)"

    def test_partial_raw_coverage_names_the_raw_rules(self):
        before = _diag(evaluated=["MEM-LEAK-01", "CPU-SATURATION-01", "OOM-KILL-01"])
        cmp = _compare(before, _diag(), b={"raw_hours": 48.0})
        (note,) = cmp.notes
        assert note.kind is NoteKind.RAW_PARTIAL and note.side is SideName.BEFORE
        assert note.detail["rules"] == ["CPU-SATURATION-01", "OOM-KILL-01"]
        assert "48.0 / 168.0 時間" in note.message

    def test_a_side_without_data_exits_3(self):
        cmp = _compare(_diag(), None)
        assert exit_code(cmp) == 3

    def test_info_only_after_exits_2_or_0(self):
        info = _finding("ZOMBIE-01", "zombie_processes", Severity.INFO)
        cmp = _compare(_diag(), _diag(findings=[info]))
        assert exit_code(cmp) == 2
        assert exit_code(cmp, exit_zero_on_info=True) == 0

    def test_warning_after_wins_over_missing_data(self):
        before = _diag(
            findings=[_finding("DISK-FULL-01", "/")], evaluated=["DISK-FULL-01"]
        )
        cmp = _compare(before, _diag(findings=[_finding("DISK-FULL-01", "/srv")]))
        assert exit_code(cmp) == 1


class TestLabels:
    def test_every_enum_value_has_a_japanese_label(self):
        for enum in (Verdict, Why, SideName):
            for member in enum:
                assert member.label and member.label.isascii() is False
        for kind in NoteKind:
            detail = {"before": "a", "after": "b", "raw_hours": 1.0, "span_hours": 2.0}
            note = Note(kind, SideName.BEFORE, {**detail, "rules": ["X"]})
            assert note.message and not note.message.isascii()


class TestPlanRequest:
    def test_split_at_on_the_default_db(self):
        req = plan_request(
            dbs=(), default_db="d.db", at="2026-10-28T09:00Z", before=None, after=None
        )
        expected = datetime(2026, 10, 28, 9, tzinfo=timezone.utc).timestamp()
        assert req == SplitRequest(Path("d.db"), expected)

    def test_two_dbs_default_to_whole_recordings(self):
        req = plan_request(
            dbs=("a.db", "b.db"), default_db="d.db", at=None, before=None, after=None
        )
        assert req == PeriodsRequest(
            Path("a.db"), Path("b.db"), Period(None, None), Period(None, None)
        )

    @pytest.mark.parametrize(
        "kw",
        [
            {"dbs": ("a.db",)},
            {"dbs": ("a.db", "b.db"), "at": "2026-10-28"},
            {"at": "2026-10-28", "before": "2026-10-01..2026-10-02"},
            {},
            {"before": "2026-10-01..2026-10-02"},
            {"before": "2026-10-02..2026-10-01", "after": "2026-10-03.."},
            {"before": "2026-10-01", "after": "2026-10-03.."},
            {"at": "yesterday"},
        ],
    )
    def test_bad_combinations_are_usage_errors(self, kw):
        args = {
            "dbs": (),
            "default_db": "d.db",
            "at": None,
            "before": None,
            "after": None,
        }
        with pytest.raises(CompareError):
            plan_request(**{**args, **kw})

    def test_times_honour_offsets_and_default_to_local(self):
        jst = timezone(timedelta(hours=9))
        assert (
            parse_instant("2026-10-28 09:00+09:00")
            == datetime(2026, 10, 28, 9, tzinfo=jst).timestamp()
        )
        assert (
            parse_instant("2026-10-28 09:00") == datetime(2026, 10, 28, 9).timestamp()
        )
        assert parse_instant("2026-10-28") == datetime(2026, 10, 28).timestamp()

    def test_periods_in_one_db_must_not_overlap(self):
        check_order(Period(None, 10.0), Period(10.0, None))
        for before, after in [
            (Period(0.0, 11.0), Period(10.0, 20.0)),
            (Period(0.0, None), Period(10.0, 20.0)),
            (Period(20.0, 30.0), Period(0.0, 10.0)),
        ]:
            with pytest.raises(CompareError, match="重なって"):
                check_order(before, after)
