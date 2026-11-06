"""Compare a period before a fix with one after it."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Union

from .core import (
    Diagnosis,
    Finding,
    Headline,
    MeasuredRule,
    Measurement,
    Severity,
    Window,
    configure_rule,
    get_rule,
)
from .core.result import CoverageGap
from .diagnosis import Engine
from .storage import RAW_TABLES, Storage, StorageVersionError

if TYPE_CHECKING:
    from .config import ProcdiagConfig

COMPARE_FORMAT = "procdiag-compare/1"
MATERIAL_CHANGE = 0.2

_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


class CompareError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    """A requested half-open period ``[since, until)``; None is an open end."""

    since: Optional[float]
    until: Optional[float]


@dataclass(frozen=True)
class SplitRequest:
    db: Path
    at: float


@dataclass(frozen=True)
class PeriodsRequest:
    before_db: Path
    after_db: Path
    before: Period
    after: Period


CompareRequest = Union[SplitRequest, PeriodsRequest]


def parse_instant(text: str) -> float:
    """Epoch seconds for an ISO 8601 time; no offset means this host's zone."""
    raw = text.strip()
    iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        raise CompareError(
            f"時刻の形式が不正です: '{text}' (例: 2026-10-28 09:00)"
        ) from None


def parse_period(text: str) -> Period:
    """``START..END`` (half-open); either end may be omitted."""
    if ".." not in text:
        raise CompareError(
            f"期間は START..END の形で指定してください (片側は省略可): '{text}'"
        )
    start, _, end = text.partition("..")
    period = Period(
        parse_instant(start) if start.strip() else None,
        parse_instant(end) if end.strip() else None,
    )
    if (
        period.since is not None
        and period.until is not None
        and period.since >= period.until
    ):
        raise CompareError(f"期間の開始が終了以降です: '{text}'")
    return period


def plan_request(
    *,
    dbs: tuple[str, ...],
    default_db: str,
    at: Optional[str],
    before: Optional[str],
    after: Optional[str],
) -> CompareRequest:
    if len(dbs) not in (0, 2):
        raise CompareError(
            "DB は 2 つ (修正前 修正後) 指定するか、省略して --db / 設定の DB を使ってください"
        )
    if dbs:
        if at is not None:
            raise CompareError(
                "--at は DB を 1 つだけ使うときの指定です"
                " (2 つの DB では --before / --after で期間を絞れます)"
            )
        whole = Period(None, None)
        return PeriodsRequest(
            Path(dbs[0]),
            Path(dbs[1]),
            parse_period(before) if before else whole,
            parse_period(after) if after else whole,
        )
    db = Path(default_db)
    if at is not None:
        if before or after:
            raise CompareError("--at と --before / --after は同時に指定できません")
        return SplitRequest(db, parse_instant(at))
    if not (before and after):
        raise CompareError(
            "1 つの DB では --at か、--before と --after の両方を指定してください"
        )
    return PeriodsRequest(db, db, parse_period(before), parse_period(after))


def check_order(before: Period, after: Period) -> None:
    end = before.until if before.until is not None else float("inf")
    start = after.since if after.since is not None else float("-inf")
    if end > start:
        raise CompareError(
            "修正前と修正後の期間が重なっています。"
            "1 つの DB では、修正前の期間は修正後の期間より前に終わる必要があります"
        )


class Why(str, Enum):
    """Machine-stable reason behind a verdict or an unknown side (JSON ``why``)."""

    FINDING_CLEARED = "finding_cleared"
    BELOW_THRESHOLD = "below_threshold"
    SEVERITY_LOWER = "severity_lower"
    VALUE_BETTER = "value_better"
    WITHIN_TOLERANCE = "within_tolerance"
    NO_COMPARABLE_VALUE = "no_comparable_value"
    SEVERITY_HIGHER = "severity_higher"
    VALUE_WORSE = "value_worse"
    FINDING_APPEARED = "finding_appeared"
    NO_DATA = "no_data"
    TOO_SHORT = "too_short"
    NOT_RECORDED = "not_recorded"
    SCHEMA_MISSING = "schema_missing"
    RULE_ERRORED = "rule_errored"
    SUBJECT_NOT_OBSERVED = "subject_not_observed"

    @property
    def label(self) -> str:
        return _WHY_JA[self]


_WHY_JA = {
    Why.FINDING_CLEARED: "検出が消え、値もしきい値の半分以下",
    Why.BELOW_THRESHOLD: "しきい値未満",
    Why.SEVERITY_LOWER: "重大度が下がった",
    Why.VALUE_BETTER: f"値が {MATERIAL_CHANGE:.0%} 以上改善",
    Why.WITHIN_TOLERANCE: f"値の変化が {MATERIAL_CHANGE:.0%} 未満",
    Why.NO_COMPARABLE_VALUE: "比べられる値がない",
    Why.SEVERITY_HIGHER: "重大度が上がった",
    Why.VALUE_WORSE: f"値が {MATERIAL_CHANGE:.0%} 以上悪化",
    Why.FINDING_APPEARED: "修正後に新たに検出",
    Why.NO_DATA: "期間内に記録がない",
    Why.TOO_SHORT: "記録時間が足りない",
    Why.NOT_RECORDED: "期間内に必要な記録がない",
    Why.SCHEMA_MISSING: "DB の版が古い",
    Why.RULE_ERRORED: "ルールの実行エラー",
    Why.SUBJECT_NOT_OBSERVED: "対象を測定できなかった",
}


@dataclass(frozen=True)
class Detected:
    severity: Severity
    count: int
    value: Optional[float]
    finding: Finding


@dataclass(frozen=True)
class Clear:
    value: Optional[float]
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Unknown:
    why: Why
    detail: str


SideState = Union[Detected, Clear, Unknown]


class Verdict(str, Enum):
    WORSE = "worse"
    NEW = "new"
    UNCHANGED = "unchanged"
    IMPROVED = "improved"
    NOT_EVALUABLE = "not_evaluable"
    RESOLVED = "resolved"

    @property
    def label(self) -> str:
        return _VERDICT_JA[self][0]

    @property
    def symbol(self) -> str:
        return _VERDICT_JA[self][1]


_VERDICT_JA = {
    Verdict.WORSE: ("悪化", "↗"),
    Verdict.NEW: ("新規", "＋"),
    Verdict.UNCHANGED: ("変化なし", "＝"),
    Verdict.IMPROVED: ("改善", "↘"),
    Verdict.NOT_EVALUABLE: ("判定不能", "？"),
    Verdict.RESOLVED: ("解消", "✔"),
}


def _material_change(before: float, after: float) -> Optional[Why]:
    rel = (after - before) / max(abs(before), 1e-9)
    if abs(rel) < MATERIAL_CHANGE:
        return None
    return Why.VALUE_BETTER if rel < 0 else Why.VALUE_WORSE


def classify(
    before: SideState, after: SideState, threshold: Optional[float]
) -> tuple[Verdict, Why]:
    if isinstance(after, Unknown):
        return Verdict.NOT_EVALUABLE, after.why
    if isinstance(before, Unknown):
        return Verdict.NOT_EVALUABLE, before.why
    if isinstance(before, Clear) and isinstance(after, Detected):
        return Verdict.NEW, Why.FINDING_APPEARED
    if isinstance(before, Detected) and isinstance(after, Clear):
        if after.value is None or threshold is None or after.value <= threshold / 2:
            return Verdict.RESOLVED, Why.FINDING_CLEARED
        change = (
            _material_change(before.value, after.value)
            if before.value is not None
            else None
        )
        if change is Why.VALUE_BETTER:
            return Verdict.IMPROVED, Why.BELOW_THRESHOLD
        if change is Why.VALUE_WORSE:
            return Verdict.WORSE, Why.VALUE_WORSE
        return Verdict.UNCHANGED, Why.WITHIN_TOLERANCE
    if isinstance(before, Detected) and isinstance(after, Detected):
        rank = _SEVERITY_RANK[after.severity] - _SEVERITY_RANK[before.severity]
        if rank > 0:
            return Verdict.WORSE, Why.SEVERITY_HIGHER
        if rank < 0:
            return Verdict.IMPROVED, Why.SEVERITY_LOWER
        if before.value is None or after.value is None:
            return Verdict.UNCHANGED, Why.NO_COMPARABLE_VALUE
        change = _material_change(before.value, after.value)
        if change is Why.VALUE_BETTER:
            return Verdict.IMPROVED, change
        if change is Why.VALUE_WORSE:
            return Verdict.WORSE, change
        return Verdict.UNCHANGED, Why.WITHIN_TOLERANCE
    raise ValueError("比較行には少なくとも一方の検出が必要です")


@dataclass(frozen=True)
class ComparisonRow:
    rule_id: str
    title: str
    subject: str
    identity: Optional[str]
    headline: Optional[Headline]
    threshold: Optional[float]
    before: SideState
    after: SideState

    def __post_init__(self) -> None:
        if not (isinstance(self.before, Detected) or isinstance(self.after, Detected)):
            raise ValueError("比較行には少なくとも一方の検出が必要です")

    @property
    def verdict(self) -> Verdict:
        return classify(self.before, self.after, self.threshold)[0]

    @property
    def why(self) -> Why:
        return classify(self.before, self.after, self.threshold)[1]

    @property
    def label(self) -> str:
        if self.why is Why.BELOW_THRESHOLD:
            return f"{self.verdict.label} ({self.why.label})"
        return self.verdict.label


class SideName(str, Enum):
    BEFORE = "before"
    AFTER = "after"

    @property
    def label(self) -> str:
        return "修正前" if self is SideName.BEFORE else "修正後"


@dataclass(frozen=True)
class Evaluated:
    window: Window
    raw_hours: float
    diagnosis: Diagnosis


@dataclass(frozen=True)
class Side:
    name: SideName
    db: Path
    hostname: Optional[str]
    schema_version: Optional[str]
    recording_config: Optional[str]
    requested: Period
    result: Optional[Evaluated]


class NoteKind(str, Enum):
    HOSTNAME_DIFFERS = "hostname_differs"
    SCHEMA_DIFFERS = "schema_differs"
    RECORDING_CONFIG_DIFFERS = "recording_config_differs"
    RAW_PARTIAL = "raw_partial"


@dataclass(frozen=True)
class Note:
    kind: NoteKind
    side: Optional[SideName] = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> str:
        d = self.detail
        if self.kind is NoteKind.HOSTNAME_DIFFERS:
            return (
                f"記録ホストが異なります (修正前: {d['before']} / 修正後: {d['after']})"
            )
        if self.kind is NoteKind.SCHEMA_DIFFERS:
            return (
                f"DB の形式が異なります (修正前: 版 {d['before']} / 修正後: 版 {d['after']})。"
                "一部のルールは片側でしか評価できません"
            )
        if self.kind is NoteKind.RECORDING_CONFIG_DIFFERS:
            return (
                "記録時の設定が異なります。"
                "サンプリング間隔などの違いが値に影響することがあります"
            )
        side = self.side.label if self.side else ""
        return (
            f"{side}の期間のうち生データがあるのは {d['raw_hours']:.1f} /"
            f" {d['span_hours']:.1f} 時間です。{', '.join(d['rules'])} は"
            "生データのある範囲だけで評価しました"
        )


@dataclass(frozen=True)
class Comparison:
    before: Side
    after: Side
    rows: tuple[ComparisonRow, ...]
    notes: tuple[Note, ...]

    def verdict_counts(self) -> dict[Verdict, int]:
        return {v: sum(1 for r in self.rows if r.verdict is v) for v in Verdict}


def _rule_gap(side: Side, rule_id: str) -> Optional[Unknown]:
    if side.result is None or side.result.diagnosis.no_data:
        return Unknown(Why.NO_DATA, "期間内に記録がありません")
    diag = side.result.diagnosis
    for r in diag.skipped_rules:
        if r.rule_id == rule_id:
            return Unknown(
                Why.TOO_SHORT,
                f"記録が {diag.quality.span_hours:.1f} 時間のため未評価"
                f" ({r.required_hours:.1f} 時間必要、あと {r.remaining_hours:.1f} 時間)",
            )
    for r in diag.uncovered_rules:
        if r.rule_id == rule_id:
            if r.reason is CoverageGap.SCHEMA_MISSING:
                return Unknown(Why.SCHEMA_MISSING, r.detail)
            hint = (
                " (生データは保持期間を過ぎると削除されます)"
                if set(r.missing) & set(RAW_TABLES)
                else ""
            )
            return Unknown(Why.NOT_RECORDED, r.detail + hint)
    if rule_id in diag.errored_rules:
        return Unknown(Why.RULE_ERRORED, "ルールの実行中にエラーが発生しました")
    return None


def _measured_state(gap: Optional[Unknown], m: Optional[Measurement]) -> SideState:
    if gap is not None:
        return gap
    if m is None:
        return Unknown(
            Why.SUBJECT_NOT_OBSERVED,
            "期間内にこの対象の測定値がありません (停止中・記録が短い等)",
        )
    if m.finding is not None:
        return Detected(m.finding.severity, 1, m.observation.value, m.finding)
    return Clear(m.observation.value, dict(m.observation.evidence))


def _pair(
    before: list[Measurement], after: list[Measurement]
) -> list[tuple[Optional[Measurement], Optional[Measurement]]]:
    def key(m: Measurement) -> tuple[str, Optional[str]]:
        return m.observation.subject, m.observation.identity

    b_by = {key(m): m for m in before}
    a_by = {key(m): m for m in after}
    pairs: list[tuple[Optional[Measurement], Optional[Measurement]]] = [
        (b_by[k], a_by[k]) for k in b_by if k in a_by
    ]
    b_left = [m for k, m in b_by.items() if k not in a_by]
    a_left = [m for k, m in a_by.items() if k not in b_by]
    for name in dict.fromkeys(m.observation.subject for m in b_left + a_left):
        bs = [m for m in b_left if m.observation.subject == name]
        as_ = [m for m in a_left if m.observation.subject == name]
        if len(bs) == 1 and len(as_) == 1:
            pairs.append((bs[0], as_[0]))
        else:
            pairs.extend((m, None) for m in bs)
            pairs.extend((None, m) for m in as_)
    return pairs


def _measured_rows(
    rule_id: str,
    cls: type[MeasuredRule],
    before: Side,
    after: Side,
    config: "ProcdiagConfig",
) -> list[ComparisonRow]:
    rule, _ = configure_rule(cls, config)
    threshold = float(getattr(rule, cls.headline.threshold))
    gaps = (_rule_gap(before, rule_id), _rule_gap(after, rule_id))
    sides = [
        s.result.diagnosis.measurements.get(rule_id, []) if s.result else []
        for s in (before, after)
    ]
    rows = []
    for b, a in _pair(*sides):
        if not ((b and b.finding) or (a and a.finding)):
            continue
        shown = b or a
        rows.append(
            ComparisonRow(
                rule_id=rule_id,
                title=cls.title,
                subject=shown.observation.subject,
                identity=shown.observation.identity,
                headline=cls.headline,
                threshold=threshold,
                before=_measured_state(gaps[0], b),
                after=_measured_state(gaps[1], a),
            )
        )
    return rows


def _side_findings(side: Side) -> list[Finding]:
    if side.result is None:
        return []
    diag = side.result.diagnosis
    return diag.findings + diag.folded


def _plain_state(side: Side, rule_id: str, subject: str) -> SideState:
    gap = _rule_gap(side, rule_id)
    if gap is not None:
        return gap
    hits = [
        f for f in _side_findings(side) if f.rule_id == rule_id and f.subject == subject
    ]
    if not hits:
        return Clear(None)
    worst = max(hits, key=lambda f: _SEVERITY_RANK[f.severity])
    return Detected(worst.severity, len(hits), None, worst)


def _note_list(before: Side, after: Side) -> list[Note]:
    notes = [
        Note(kind, detail={"before": b, "after": a})
        for kind, b, a in (
            (NoteKind.HOSTNAME_DIFFERS, before.hostname, after.hostname),
            (NoteKind.SCHEMA_DIFFERS, before.schema_version, after.schema_version),
        )
        if b is not None and a is not None and b != a
    ]
    configs = (before.recording_config, after.recording_config)
    if None not in configs and configs[0] != configs[1]:
        notes.append(Note(NoteKind.RECORDING_CONFIG_DIFFERS))
    for side in (before, after):
        res = side.result
        if res is None or not 0 < res.raw_hours < res.window.span_hours - 1 / 60:
            continue
        raw_rules = [
            rid
            for rid in res.diagnosis.evaluated_rules
            if (cls := get_rule(rid)) and set(cls.requires_rows) & set(RAW_TABLES)
        ]
        if raw_rules:
            notes.append(
                Note(
                    NoteKind.RAW_PARTIAL,
                    side.name,
                    {
                        "raw_hours": res.raw_hours,
                        "span_hours": res.window.span_hours,
                        "rules": raw_rules,
                    },
                )
            )
    return notes


def build_comparison(before: Side, after: Side, config: "ProcdiagConfig") -> Comparison:
    rows: list[ComparisonRow] = []
    measured = {
        rid
        for side in (before, after)
        if side.result
        for rid in side.result.diagnosis.measurements
    }
    for rule_id in sorted(measured):
        cls = get_rule(rule_id)
        rows.extend(_measured_rows(rule_id, cls, before, after, config))
    plain_keys = dict.fromkeys(
        (f.rule_id, f.subject)
        for side in (before, after)
        for f in _side_findings(side)
        if f.rule_id not in measured
    )
    for rule_id, subject in plain_keys:
        cls = get_rule(rule_id)
        rows.append(
            ComparisonRow(
                rule_id=rule_id,
                title=cls.title if cls else rule_id,
                subject=subject,
                identity=None,
                headline=None,
                threshold=None,
                before=_plain_state(before, rule_id, subject),
                after=_plain_state(after, rule_id, subject),
            )
        )
    order = list(Verdict)
    rows.sort(key=lambda r: (order.index(r.verdict), r.rule_id, r.subject))
    return Comparison(before, after, tuple(rows), tuple(_note_list(before, after)))


def exit_code(cmp: Comparison, *, exit_zero_on_info: bool = False) -> int:
    """The documented code (priority 1 > 3 > 2 > 0).

    1: a warning/critical finding remains or appeared after the fix.
    3: a side has no data, or a warning/critical before finding cannot be
       judged after (record longer, or within raw retention).
    2: only info findings remain after (0 with ``exit_zero_on_info``).
    """
    after_hits = [r.after for r in cmp.rows if isinstance(r.after, Detected)]
    if any(h.severity is not Severity.INFO for h in after_hits):
        return 1
    if cmp.before.result is None or cmp.after.result is None:
        return 3
    if any(
        r.verdict is Verdict.NOT_EVALUABLE
        and isinstance(r.before, Detected)
        and r.before.severity is not Severity.INFO
        for r in cmp.rows
    ):
        return 3
    if after_hits and not exit_zero_on_info:
        return 2
    return 0


def _resolve(store: Storage, period: Period) -> Optional[tuple[Window, float]]:
    until = None if period.until is None else period.until - 1e-3
    bounds = [
        b
        for t in ("sys_samples", "sys_rollup_1m")
        if (b := store.time_bounds(t, period.since, until)) is not None
    ]
    if not bounds:
        return None
    window = Window(min(b[0] for b in bounds), max(b[1] for b in bounds))
    raw = store.time_bounds("sys_samples", window.start_ts, window.end_ts)
    return window, (raw[1] - raw[0]) / 3600.0 if raw else 0.0


def _evaluate_side(
    name: SideName,
    store: Storage,
    period: Period,
    engine: Engine,
    only: Optional[list[str]],
    skip: Optional[list[str]],
) -> Side:
    meta = store.meta()
    resolved = _resolve(store, period)
    result = None
    if resolved is not None:
        window, raw_hours = resolved
        diag = engine.run(store, only=only, skip=skip, window=window)
        result = Evaluated(window, raw_hours, diag)
    return Side(
        name=name,
        db=store.path,
        hostname=meta.get("hostname"),
        schema_version=meta.get("schema_version"),
        recording_config=meta.get("config"),
        requested=period,
        result=result,
    )


def _open(stack: ExitStack, db: Path) -> Storage:
    try:
        return stack.enter_context(Storage(db, read_only=True))
    except StorageVersionError as exc:
        raise CompareError(f"{db}: {exc}") from exc


def run_compare(
    config: "ProcdiagConfig",
    request: CompareRequest,
    *,
    only: Optional[list[str]] = None,
    skip: Optional[list[str]] = None,
    verbose: bool = False,
) -> Comparison:
    """Diagnose both periods with the same rules and thresholds, then compare.

    Raises :class:`CompareError` for overlapping periods in one DB and for a
    DB written by a newer procdiag.  Read-only throughout.
    """
    engine = Engine(config, verbose=verbose)
    if isinstance(request, SplitRequest):
        before_db = after_db = request.db
    else:
        before_db, after_db = request.before_db, request.after_db
    same_db = before_db.resolve() == after_db.resolve()
    if same_db and isinstance(request, PeriodsRequest):
        check_order(request.before, request.after)
    with ExitStack() as stack:
        after_store = _open(stack, after_db)
        before_store = after_store if same_db else _open(stack, before_db)
        if isinstance(request, SplitRequest):
            after = _evaluate_side(
                SideName.AFTER,
                after_store,
                Period(request.at, None),
                engine,
                only,
                skip,
            )
            span = after.result.window.span_hours if after.result else 0.0
            hours = max(span, engine.required_hours(only=only, skip=skip))
            before_period = Period(request.at - hours * 3600.0, request.at)
        else:
            after = _evaluate_side(
                SideName.AFTER, after_store, request.after, engine, only, skip
            )
            before_period = request.before
        before = _evaluate_side(
            SideName.BEFORE, before_store, before_period, engine, only, skip
        )
    return build_comparison(before, after, config)
