"""Public re-exports for the procdiag core layer.

Other packages import from here so they stay decoupled from the exact module
that holds each name — mirrors netdiag's ``core/__init__.py``.
"""

from __future__ import annotations

from .result import Confidence, Diagnosis, Finding, ObservationQuality, Severity, Window
from .rule import Rule, all_rules, get_rule, register, registry_names
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
    # rule
    "Rule",
    "register",
    "get_rule",
    "all_rules",
    "registry_names",
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
]
