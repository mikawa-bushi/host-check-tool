"""Public re-exports for the procdiag core layer.

Other packages import from here so they stay decoupled from the exact module
that holds each name — mirrors netdiag's ``core/__init__.py``.
"""

from __future__ import annotations

from .result import (
    Capability,
    Confidence,
    CoverageGap,
    Diagnosis,
    Finding,
    Headline,
    Measurement,
    Observation,
    ObservationQuality,
    Severity,
    SkippedRule,
    UncoveredRule,
    Unobserved,
    Window,
    covers,
)
from .rule import (
    MeasuredRule,
    Rule,
    all_rules,
    configure_rule,
    get_rule,
    register,
    registry_names,
    rule_id_problems,
    validate_rule_overrides,
)
from .sampler import (
    RunContext,
    SampleBatch,
    Sampler,
    SystemSnapshot,
    all_samplers,
    get_sampler,
    register_sampler,
    sampler_registry_names,
)
from .util import (
    CounterDelta,
    GiB,
    KiB,
    MiB,
    humanize_bytes,
    humanize_duration,
    normalize_cmdline,
    parse_duration,
)

__all__ = [
    # result
    "Severity",
    "Confidence",
    "Window",
    "Finding",
    "ObservationQuality",
    "Diagnosis",
    "SkippedRule",
    "UncoveredRule",
    "CoverageGap",
    "Capability",
    "Unobserved",
    "Headline",
    "Observation",
    "Measurement",
    "covers",
    # rule
    "Rule",
    "MeasuredRule",
    "register",
    "get_rule",
    "all_rules",
    "registry_names",
    "configure_rule",
    "rule_id_problems",
    "validate_rule_overrides",
    # sampler
    "SampleBatch",
    "SystemSnapshot",
    "RunContext",
    "Sampler",
    "register_sampler",
    "get_sampler",
    "all_samplers",
    "sampler_registry_names",
    # util
    "parse_duration",
    "humanize_bytes",
    "humanize_duration",
    "normalize_cmdline",
    "CounterDelta",
    "KiB",
    "MiB",
    "GiB",
]
