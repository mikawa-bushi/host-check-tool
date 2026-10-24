"""Rule base class, registry, and registration decorator.

Maps netdiag's ``core/probe.py`` Probe + registry pattern to the rule domain.
A Rule answers the question "is this pattern present in the recorded data?"
by querying Storage and returning a list of Findings.  Rules are stateless
and independent; the engine in ``diagnosis/engine.py`` runs them all (or a
filtered subset) against a Storage instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Type

from .result import Finding, Severity, Window

if TYPE_CHECKING:
    # Avoid circular imports at runtime; both are only referenced in type hints.
    from ..config import ProcdiagConfig
    from ..storage import Storage


class Rule:
    """Base class for all diagnostic rules.

    Subclasses set the class-level attributes and implement ``evaluate``.
    The attributes mirror ``Probe`` in netdiag: they are surfaced verbatim
    by ``procdiag explain`` so operators learn the detection rationale and
    can reproduce the check manually.
    """

    rule_id: str = ""
    category: str = ""  # "memory" | "cpu" | "io_disk" | "leak"
    title: str = ""
    default_severity: Severity = Severity.WARNING
    min_observation_hours: float = 6.0  # prevent false positives on short runs
    what: str = ""  # what pattern is detected (Japanese)
    why: str = ""  # why this detection method (Japanese)
    manual_commands: list[str] = []

    def configure(self, config: "ProcdiagConfig") -> None:
        """Apply operator settings from ``config`` before ``evaluate`` runs.

        The engine calls this once per run on every instantiated rule.  The
        default does nothing; a rule whose threshold is exposed in
        ``config.rules`` overrides it to copy that value onto the instance, so
        the setting written in ``config.py`` actually takes effect.
        """

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run the rule against *store* over *window*.

        Must return a list of :class:`Finding` objects (empty list = healthy).
        Subclasses implement this; the default raises to surface missing
        overrides during development.
        """
        raise NotImplementedError(
            f"ルール {self.__class__.__name__} は evaluate() を実装していません"
        )


# --- registry -----------------------------------------------------------------

_REGISTRY: Dict[str, Type[Rule]] = {}


def register(cls: Type[Rule]) -> Type[Rule]:
    """Class decorator: add a rule to the global registry by its ``rule_id``."""
    if not cls.rule_id:
        raise ValueError(f"ルールクラス {cls.__name__} に rule_id が設定されていません")
    _REGISTRY[cls.rule_id] = cls
    return cls


def get_rule(rule_id: str) -> Optional[Type[Rule]]:
    """Look up a rule class by ID, or return None."""
    return _REGISTRY.get(rule_id)


def all_rules() -> Dict[str, Type[Rule]]:
    """Return a shallow copy of the full registry."""
    return dict(_REGISTRY)


def registry_names() -> List[str]:
    """Return sorted list of registered rule IDs."""
    return sorted(_REGISTRY)
