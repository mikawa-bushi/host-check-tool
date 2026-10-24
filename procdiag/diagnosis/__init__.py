"""Diagnosis package: rules, engine, and statistical helpers for procdiag.

Re-exports the public API of ``engine`` and ``trend`` so callers can write::

    from procdiag.diagnosis import Engine, TrendFit, fit_linear

Eagerly imports the rule modules so that every ``@register`` decorator fires
and all built-in rules are registered in ``procdiag.core.all_rules()`` as
soon as this package is imported.  A new rule added to one of these modules is
picked up automatically; a new rule *module* must be added to the import list
below.
"""

from __future__ import annotations

# -- eager rule registration -------------------------------------------------
# Import order does not matter (rules are independent).
from . import (  # noqa: F401  (imported for @register side-effects)
    cpu,  # CPU-PERIODIC-01, CPU-SINGLECORE-01, CPU-SATURATION-01
    crash,  # OOM-KILL-01
    io_disk,  # DISK-FULL-01, IO-BOTTLENECK-01, DISK-GROWTH-01, DISK-JOURNAL-01
    leak,  # FD-LEAK-01, THREAD-LEAK-01, ZOMBIE-01
    memory,  # MEM-LEAK-01, MEM-PRESSURE-01, SWAP-THRASH-01
    thermal,  # THERMAL-01
)

# -- public re-exports -------------------------------------------------------
from .engine import Engine, fold
from .trend import (
    Period,
    TrendFit,
    detect_periodicity,
    detect_recurrence,
    extrapolate_full,
    fit_linear,
)

__all__ = [
    # engine
    "Engine",
    "fold",
    # trend
    "TrendFit",
    "Period",
    "fit_linear",
    "extrapolate_full",
    "detect_periodicity",
    "detect_recurrence",
]
