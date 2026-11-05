"""Rule base class, registry, and registration decorator.

Maps netdiag's ``core/probe.py`` Probe + registry pattern to the rule domain.
A Rule answers the question "is this pattern present in the recorded data?"
by querying Storage and returning a list of Findings.  Rules are stateless
and independent; the engine in ``diagnosis/engine.py`` runs them all (or a
filtered subset) against a Storage instance.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

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
    # The engine, not the rule, decides whether the data can speak for a
    # window; a rule whose inputs are missing goes to Diagnosis.uncovered_rules
    # instead of silently reporting "nothing found".
    # requires_schema: tables / "table.column" that must exist in the DB
    #   (event tables included).
    # requires_rows: sampled tables without whose rows in the window the rule
    #   cannot fire.  Never event tables (empty is their normal state) and
    #   never tables read only to attribute a finding.  Tables listed here must
    #   exist as well.
    requires_schema: tuple[str, ...] = ()
    requires_rows: tuple[str, ...] = ()

    @classmethod
    def tunables(cls) -> Dict[str, Union[int, float]]:
        """Numeric thresholds an operator may override, with their defaults.

        Every public int/float class attribute (inherited ones included, e.g.
        ``min_observation_hours``) is tunable through
        ``config.rules.overrides[<rule_id>]``; ``procdiag explain`` lists them.
        """
        names = {
            name
            for klass in cls.__mro__
            if klass is not object
            for name, value in vars(klass).items()
            if not name.startswith("_")
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        }
        return {name: getattr(cls, name) for name in sorted(names)}

    def configure(self, config: "ProcdiagConfig") -> None:
        """Apply operator settings from ``config`` before ``evaluate`` runs.

        The engine calls this once per run on every instantiated rule.  The
        base implementation applies ``config.rules.overrides[self.rule_id]``
        onto the instance; a rule with a dedicated setting in ``config.rules``
        overrides this to copy that value first and then calls ``super()``, so
        an explicit override still wins.  Raises :class:`ValueError` for an
        unknown or ill-typed override (nothing is applied in that case).
        """
        overrides = config.rules.overrides.get(self.rule_id, {})
        resolved = resolve_overrides(type(self), overrides)
        for name, value in resolved.items():
            setattr(self, name, value)

    def evaluate(self, store: "Storage", window: Window) -> list[Finding]:
        """Run the rule against *store* over *window*.

        Must return a list of :class:`Finding` objects (empty list = healthy).
        Subclasses implement this; the default raises to surface missing
        overrides during development.
        """
        raise NotImplementedError(
            f"ルール {self.__class__.__name__} は evaluate() を実装していません"
        )


def resolve_overrides(
    cls: Type[Rule], overrides: Mapping[str, float]
) -> Dict[str, Union[int, float]]:
    """Validate *overrides* against ``cls.tunables()`` and coerce their types.

    An int threshold (counts, minutes) only accepts a whole number.  Raises
    :class:`ValueError` (Japanese message) on the first invalid entry.
    """
    tunables = cls.tunables()
    resolved: Dict[str, Union[int, float]] = {}
    for name, value in overrides.items():
        if name not in tunables:
            raise ValueError(
                f"{cls.rule_id}: '{name}' は調整可能なしきい値ではありません"
                f" (調整可能: {', '.join(tunables)})"
            )
        if isinstance(tunables[name], int):
            if not float(value).is_integer():
                raise ValueError(
                    f"{cls.rule_id}: '{name}' は整数で指定してください: {value}"
                )
            resolved[name] = int(value)
        else:
            resolved[name] = float(value)
    return resolved


_R = TypeVar("_R", bound=Rule)


def configure_rule(cls: Type[_R], config: "ProcdiagConfig") -> Tuple[_R, Optional[str]]:
    """Instantiate *cls* with *config* applied; ``(rule, error)``.

    An invalid override (the CLI rejects those at start-up, library callers
    may not) leaves the rule at its defaults and is returned as *error* so the
    caller can say so instead of aborting.
    """
    rule = cls()
    try:
        rule.configure(config)
    except ValueError as exc:
        return cls(), str(exc)
    return rule, None


def validate_rule_overrides(overrides: Mapping[str, Mapping[str, float]]) -> List[str]:
    """Return a message for every problem in ``config.rules.overrides``.

    Unknown rule IDs and non-tunable / ill-typed attributes are reported, so a
    typo in ``config.py`` fails at start-up instead of silently keeping the
    default threshold.  The rule registry must already be populated.
    """
    problems: List[str] = []
    for rule_id, values in overrides.items():
        cls = _REGISTRY.get(rule_id)
        if cls is None:
            problems.append(f"rules.overrides: 不明なルール ID です: {rule_id}")
            continue
        try:
            resolve_overrides(cls, values)
        except ValueError as exc:
            problems.append(f"rules.overrides: {exc}")
    return problems


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
