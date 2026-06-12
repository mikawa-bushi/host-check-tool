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
from typing import Any


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
    gap_seconds: float = 0.0
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
class Diagnosis:
    """Aggregated result of running all rules against the stored data.

    Mirrors the structure of ``netdiag.diagnosis.Diagnosis`` so report and CLI
    code can be written once and reused across both tools.
    """

    findings: list[Finding]
    healthy: bool
    reasoning: list[str] = field(default_factory=list)  # verbose trace
    quality: ObservationQuality = field(default_factory=ObservationQuality)

    @property
    def primary(self) -> Finding | None:
        """The highest-priority finding, or None if the machine is healthy."""
        return self.findings[0] if self.findings else None
