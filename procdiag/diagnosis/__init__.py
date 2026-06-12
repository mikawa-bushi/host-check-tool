"""Diagnosis package: rules, engine, and statistical helpers for procdiag.

Re-exports the public API of ``engine`` and ``trend`` so callers can write::

    from procdiag.diagnosis import Engine, TrendFit, fit_linear

Eagerly imports the rule modules so that every ``@register`` decorator fires
and all built-in rules are registered in ``procdiag.core.all_rules()`` as
soon as this package is imported.  Phase 4 rule additions in the same
modules (``memory``, ``cpu``, ``io_disk``, ``leak``) will therefore also be
picked up automatically — no further wiring is required in T09 or T10–T12.
"""

from __future__ import annotations

# -- eager rule registration -------------------------------------------------
# Import order does not matter (rules are independent), but all four must be
# listed so that leak.py (currently an empty stub) is also imported once
# Phase 4 adds rules to it.

from . import cpu  # noqa: F401 — registers CPU-PERIODIC-01, CPU-SINGLECORE-01
from . import crash  # noqa: F401 — registers OOM-KILL-01
from . import io_disk  # noqa: F401 — registers DISK-FULL-01
from . import leak  # noqa: F401 — stub; will register FD/THREAD/ZOMBIE rules
from . import memory  # noqa: F401 — registers MEM-LEAK-01

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
