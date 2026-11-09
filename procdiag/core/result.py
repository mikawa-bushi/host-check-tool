"""Core output types: Finding, Diagnosis, and their supporting enumerations.

A rule never decides *how* to fix a problem; it records *what* was detected.
Interpretation flows from storage → diagnosis → report without coupling the
layers together.  Keeping data classes here, separate from rule logic, mirrors
the netdiag pattern of ``core/result.py`` holding ``CheckResult`` and
``Status`` away from ``core/probe.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Severity(str, Enum):
    """Diagnosis severity level.

    ``str`` mix-in so the value serialises to plain JSON and compares to
    string literals without an extra ``.value`` call.
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def symbol(self) -> str:
        """Single character representing the severity (terminal output)."""
        return {
            Severity.INFO: "ℹ",
            Severity.WARNING: "⚠",
            Severity.CRITICAL: "✘",
        }[self]


class Confidence(str, Enum):
    """How strongly the evidence supports the finding."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Window:
    """The observation period a rule evaluated."""

    start_ts: float
    end_ts: float

    @property
    def span_hours(self) -> float:
        """Length of the window in hours."""
        return (self.end_ts - self.start_ts) / 3600.0


# The span from the first row to the last falls short of the time recorded:
# a period [X, X+6h) holds rows up to one step before X+6h, rollup rows carry
# the start of their minute, and the minute in progress at stop is never
# rolled up.  Together that stays under two minutes.
_SPAN_ALLOWANCE_HOURS = 2 / 60


def covers(span_hours: float, hours: float) -> bool:
    """Whether an observed span is long enough for a requirement of *hours*."""
    # Spans between minute-stamped rows land exactly on the allowance, where
    # float error in hours would decide the tie, so compare in microseconds.
    return round((span_hours + _SPAN_ALLOWANCE_HOURS - hours) * 3600, 6) >= 0


@dataclass
class Finding:
    """One diagnosed problem, as defined in design doc §6.1.

    ``evidence`` must be JSON-serialisable so the JSON report can embed it
    verbatim.  ``to_dict`` expands enums to their ``.value`` strings so callers
    do not need to handle the Enum type.
    """

    rule_id: str  # e.g. "MEM-LEAK-01"
    severity: Severity
    subject: str  # process name / unit name / mount-point
    summary: str  # one-line summary (Japanese)
    evidence: dict  # JSON-serialisable backing data
    detected_at: tuple[float, float]  # (start_ts, end_ts)
    confidence: Confidence
    related: list[str] = field(default_factory=list)  # folded Finding IDs
    proposal: str = ""  # rendered Jinja2 proposal (T06 fills this)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict representation with Enum values expanded."""
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "subject": self.subject,
            "summary": self.summary,
            "evidence": self.evidence,
            "detected_at": list(self.detected_at),
            "confidence": self.confidence.value,
            "related": list(self.related),
            "proposal": self.proposal,
        }


@dataclass
class ObservationQuality:
    """Quality metadata for the observation window (design doc §7.2).

    Included in every report so readers know how much to trust the findings.
    """

    span_hours: float = 0.0
    gap_seconds: float = 0.0  # total recorder gap (job-late) delay in seconds
    missing_io_pct: float = 0.0  # fraction of proc rows where io_counters was NULL
    self_cpu_avg: float = 0.0
    self_cpu_max: float = 0.0
    throttle_events: int = 0
    notes: list[str] = field(default_factory=list)  # demotion reasons etc.

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict representation."""
        return {
            "span_hours": self.span_hours,
            "gap_seconds": self.gap_seconds,
            "missing_io_pct": self.missing_io_pct,
            "self_cpu_avg": self.self_cpu_avg,
            "self_cpu_max": self.self_cpu_max,
            "throttle_events": self.throttle_events,
            "notes": list(self.notes),
        }


@dataclass
class SkippedRule:
    """A selected rule that could not run because the recording is too short."""

    rule_id: str
    title: str
    required_hours: float  # minimum observation the rule needs
    remaining_hours: float  # how much longer to record before it can run

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict representation."""
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "required_hours": self.required_hours,
            "remaining_hours": self.remaining_hours,
        }


@dataclass(frozen=True)
class Headline:
    """What a measured rule's ``Observation.value`` means, for people.

    Lower values are better for every measured rule.  ``threshold`` names the
    tunable that ``judge`` compares the value against, so a reader of the
    value (``compare``) takes the configured threshold from the rule itself.
    """

    label: str
    unit: str
    threshold: str
    digits: int = 1


@dataclass(frozen=True)
class Observation:
    """One subject's measured value over a window, before any threshold.

    ``identity`` tells two subjects with the same name apart across DBs
    (the process's cmdline hash); None for host-wide subjects.  ``evidence``
    is JSON-serialisable and carries what ``judge`` needs (r2, n, ...).
    """

    subject: str
    identity: Optional[str]
    value: float
    evidence: dict


@dataclass(frozen=True)
class Measurement:
    observation: Observation
    finding: Optional[Finding]


class CoverageGap(str, Enum):
    SCHEMA_MISSING = "schema_missing"
    NOT_RECORDED = "not_recorded"


@dataclass
class UncoveredRule:
    """A selected rule that was not run because its input data is missing.

    ``missing`` names the tables (or ``table.column``) behind ``reason``.
    Unlike :class:`SkippedRule`, recording longer does not help.
    """

    rule_id: str
    title: str
    reason: CoverageGap
    missing: tuple[str, ...]

    @property
    def detail(self) -> str:
        names = ", ".join(self.missing)
        if self.reason is CoverageGap.SCHEMA_MISSING:
            return f"DB の版が古く {names} がありません"
        return f"期間内に {names} の記録がありません"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "reason": self.reason.value,
            "missing": list(self.missing),
        }


@dataclass
class Diagnosis:
    """Aggregated result of running all rules against the stored data.

    Mirrors the structure of ``netdiag.diagnosis.Diagnosis`` so report and CLI
    code can be written once and reused across both tools.
    """

    findings: list[Finding]
    healthy: bool
    reasoning: list[str] = field(default_factory=list)  # verbose trace
    quality: ObservationQuality = field(default_factory=ObservationQuality)
    no_data: bool = False  # True when the DB held no samples at all (see engine)
    window: Optional[Window] = None  # None when no_data
    # Rule coverage: a "healthy" verdict only speaks for the rules that ran.
    evaluated_rules: list[str] = field(default_factory=list)
    skipped_rules: list[SkippedRule] = field(default_factory=list)
    errored_rules: list[str] = field(default_factory=list)  # evaluate() raised
    uncovered_rules: list[UncoveredRule] = field(default_factory=list)
    measurements: dict[str, list[Measurement]] = field(default_factory=dict)
    folded: list[Finding] = field(default_factory=list)

    @property
    def fully_evaluated(self) -> bool:
        """True when every selected rule ran to completion."""
        return (
            not self.skipped_rules
            and not self.errored_rules
            and not self.uncovered_rules
        )

    @property
    def primary(self) -> Finding | None:
        """The highest-priority finding, or None if the machine is healthy."""
        return self.findings[0] if self.findings else None
