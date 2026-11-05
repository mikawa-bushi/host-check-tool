"""Diagnosis engine: run all registered rules against a Storage instance.

Maps ``netdiag/diagnosis/engine.py`` to the procdiag domain.  The key
differences from the netdiag engine:

- Rules are **independent** — no topological sort is needed.  Each rule
  queries the DB directly; there are no upstream dependencies.
- The window is determined from the DB's own data (``sys_samples`` min/max
  timestamps, falling back to rollup tables if raw samples were pruned).
- A per-rule minimum observation guard skips rules whose required window is
  longer than the recorded data (with a reasoning note); instantaneous rules
  (``min_observation_hours = 0.0``) still run on short recordings.
- Findings are **folded**: DISK-GROWTH-01 findings that share a mount-point
  with a DISK-FULL-01 finding are moved to ``Finding.related`` and removed
  from the top-level list.
- Each rule's ``evaluate()`` is wrapped in try/except; a crashing rule is
  logged to ``reasoning`` but does not abort the diagnosis.
- Proposals are rendered from Jinja2 templates stored next to this package.
"""

from __future__ import annotations

import re
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from jinja2 import Environment, FileSystemLoader

from ..config import ProcdiagConfig
from ..core import (
    Confidence,
    CoverageGap,
    Diagnosis,
    Finding,
    ObservationQuality,
    Rule,
    Severity,
    SkippedRule,
    UncoveredRule,
    Window,
    all_rules,
    configure_rule,
)
from ..plugins import load_extra_rules
from ..storage import OPTIONAL_SAMPLER_TABLES

if TYPE_CHECKING:
    from ..storage import Storage

# ---------------------------------------------------------------------------
# Folding map: rule_id that should be absorbed → parent rule_id
# ---------------------------------------------------------------------------

_FOLD_INTO: dict[str, str] = {
    "DISK-GROWTH-01": "DISK-FULL-01",
}

# ---------------------------------------------------------------------------
# Severity sort order (lower index = higher priority)
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}
_CONFIDENCE_ORDER = {
    Confidence.HIGH: 0,
    Confidence.MEDIUM: 1,
    Confidence.LOW: 2,
}

# ---------------------------------------------------------------------------
# Jinja2 template environment
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _make_jinja_env() -> Environment:
    """Return a Jinja2 environment backed by the templates/ directory."""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
    )


# Leading "<delay>s" token in a gap event's detail (see recorder._on_gap).
_GAP_DELAY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)s")


def _parse_gap_delay_seconds(detail: Optional[str]) -> float:
    """Extract the leading ``<delay>s`` seconds value from a gap event detail.

    ``recorder._on_gap`` formats the detail as ``"<delay>s ジョブ<name>"``.
    Returns ``0.0`` when *detail* is missing or does not start with that token,
    so an unparseable row contributes nothing to the gap total rather than
    raising.
    """
    if not detail:
        return 0.0
    m = _GAP_DELAY_RE.match(detail)
    return float(m.group(1)) if m else 0.0


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class Engine:
    """Run registered rules against a :class:`~procdiag.storage.Storage`
    instance and return a :class:`~procdiag.core.Diagnosis`.

    Usage::

        engine = Engine(config)
        with Storage(db_path, read_only=True) as store:
            diag = engine.run(store)
    """

    def __init__(
        self,
        config: ProcdiagConfig,
        *,
        verbose: bool = False,
    ) -> None:
        self.config = config
        self.verbose = verbose
        self._jinja = _make_jinja_env()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(
        self,
        only: Optional[list[str]] = None,
        skip: Optional[list[str]] = None,
    ) -> list[type[Rule]]:
        """Return the rule classes that will be run.

        Applies ``config.rules.disabled`` first, then *only* / *skip*.
        Returns rule class objects (not instances) so the caller can inspect
        class attributes before deciding to instantiate them.
        """
        disabled = set(self.config.rules.disabled)
        registry = all_rules()  # {rule_id: RuleClass}

        candidates: list[type[Rule]] = [
            cls for rule_id, cls in registry.items() if rule_id not in disabled
        ]
        if only:
            only_set = set(only)
            candidates = [c for c in candidates if c.rule_id in only_set]
        if skip:
            skip_set = set(skip)
            candidates = [c for c in candidates if c.rule_id not in skip_set]

        return candidates

    def required_hours(
        self,
        only: Optional[list[str]] = None,
        skip: Optional[list[str]] = None,
    ) -> float:
        """The longest observation any selected rule needs (configured values)."""
        self._load_extra_rules([])
        return max(
            (
                self._required_hours(configure_rule(cls, self.config)[0])
                for cls in self.select(only=only, skip=skip)
            ),
            default=0.0,
        )

    def run(
        self,
        store: "Storage",
        only: Optional[list[str]] = None,
        skip: Optional[list[str]] = None,
        *,
        window: Optional[Window] = None,
    ) -> Diagnosis:
        """Evaluate all selected rules and return a :class:`Diagnosis`.

        *window* defaults to the DB's own bounds (``check``); a given window
        is used verbatim and ``no_data`` then means no system sample inside it.

        Steps:

        0. Load ``config.rules.extra_rules`` modules so their ``@register``
           decorators fire before rule selection (load failures are recorded
           in ``reasoning`` and never abort the diagnosis).
        1. Determine the observation window from the DB.
        2. Run each rule (with try/except firewall). A rule is skipped while
           the window is shorter than its minimum observation time: rules that
           keep the class default follow ``config.rules.min_observation_hours``
           (one knob for the regression rules, per design §6.3/§8), while rules
           that override it (e.g. 0.0 for instantaneous checks like
           SWAP-THRASH-01) use their own value.  A rule whose declared inputs
           (``requires_schema`` / ``requires_rows``) are missing for the
           window is not run and is listed in ``uncovered_rules``.
        3. Render Jinja2 proposal templates into each Finding.
        4. Fold DISK-GROWTH-01 into DISK-FULL-01 (and any future fold pairs).
        5. Sort by severity → confidence.
        6. Build ObservationQuality from DB statistics.
        """
        reasoning: list[str] = []

        # --- Step 0: load user-supplied extra rule modules ---
        # Their @register decorators must fire before select()/all_rules().
        self._load_extra_rules(reasoning)

        # --- Step 1: determine window ---
        if window is None:
            window, has_data = self._determine_window(store, reasoning)
        else:
            has_data = any(
                store.time_bounds(t, window.start_ts, window.end_ts) is not None
                for t in ("sys_samples", "sys_rollup_1m")
            )

        if not has_data:
            reasoning.append(
                "観測データがありません（DB にサンプルが1件もありません）。"
                "procdiag record でデータを記録してから再度実行してください。"
            )

        if self.verbose:
            reasoning.append(
                f"観測ウィンドウ: {window.span_hours:.1f}h "
                f"({window.start_ts:.0f} – {window.end_ts:.0f})"
            )

        # --- Step 2: run rules (per-rule observation guard) ---
        rule_classes = self.select(only=only, skip=skip)
        raw_findings: list[Finding] = []
        evaluated: list[str] = []
        skipped: list[SkippedRule] = []
        errored: list[str] = []
        uncovered: list[UncoveredRule] = []
        coverage_cache: dict[tuple[str, str], bool] = {}

        for rule_cls in rule_classes:
            rule, config_error = configure_rule(rule_cls, self.config)
            if config_error is not None:
                reasoning.append(
                    f"{rule.rule_id}: 設定の適用に失敗 — 既定値で評価: {config_error}"
                )
            eff_min = self._required_hours(rule)
            if window.span_hours < eff_min:
                reasoning.append(
                    f"{rule.rule_id}: 観測時間不足のためスキップ "
                    f"({window.span_hours:.1f}h < {eff_min:.1f}h)"
                )
                skipped.append(
                    SkippedRule(
                        rule_id=rule.rule_id,
                        title=rule.title,
                        required_hours=eff_min,
                        remaining_hours=eff_min - window.span_hours,
                    )
                )
                continue
            gap = self._coverage_gap(store, rule, window, coverage_cache)
            if gap is not None:
                reasoning.append(
                    f"{rule.rule_id}: 入力データが無いため未評価 "
                    f"({', '.join(gap.missing)})"
                )
                uncovered.append(gap)
                continue
            try:
                found = rule.evaluate(store, window)
            except Exception as exc:
                msg = (
                    f"ルール {rule.rule_id} で例外が発生しました: "
                    f"{type(exc).__name__}: {exc}"
                )
                reasoning.append(msg)
                if self.verbose:
                    reasoning.append(traceback.format_exc())
                errored.append(rule.rule_id)
                continue
            evaluated.append(rule.rule_id)

            if self.verbose and found:
                for f in found:
                    reasoning.append(
                        f"{f.rule_id} [{f.severity.value}] {f.subject}: {f.summary}"
                    )

            raw_findings.extend(found)

        # --- Step 3: render proposals ---
        for finding in raw_findings:
            finding.proposal = self._render_proposal(finding, reasoning)

        # --- Step 4: fold ---
        findings = fold(raw_findings)

        # --- Step 5: sort ---
        findings.sort(
            key=lambda f: (
                _SEVERITY_ORDER.get(f.severity, 99),
                _CONFIDENCE_ORDER.get(f.confidence, 99),
            )
        )

        # --- Step 6: build quality ---
        # With no samples the window is a placeholder, so the DB-wide event
        # counts are the only statement about the recorder that can be made.
        quality = self._build_quality(store, window, events_in_window=has_data)

        healthy = not any(
            f.severity in (Severity.WARNING, Severity.CRITICAL) for f in findings
        )

        return Diagnosis(
            findings=findings,
            healthy=healthy,
            reasoning=reasoning,
            quality=quality,
            no_data=not has_data,
            evaluated_rules=evaluated,
            skipped_rules=skipped,
            errored_rules=errored,
            uncovered_rules=uncovered,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _required_hours(self, rule: Rule) -> float:
        """Rules on the class default follow ``config.rules.min_observation_hours``."""
        if rule.min_observation_hours == Rule.min_observation_hours:
            return self.config.rules.min_observation_hours
        return rule.min_observation_hours

    @staticmethod
    def _coverage_gap(
        store: "Storage",
        rule: Rule,
        window: Window,
        cache: dict[tuple[str, str], bool],
    ) -> Optional[UncoveredRule]:
        """Why *rule*'s declared inputs cannot speak for *window*, or None."""

        def exists(name: str) -> bool:
            key = ("schema", name)
            if key not in cache:
                table, _, column = name.partition(".")
                cache[key] = (
                    store.has_column(table, column)
                    if column
                    else store.has_table(table)
                )
            return cache[key]

        def has_rows(table: str) -> bool:
            key = ("rows", table)
            if key not in cache:
                inside = store.time_bounds(table, window.start_ts, window.end_ts)
                cache[key] = inside is not None or (
                    table in OPTIONAL_SAMPLER_TABLES
                    and store.time_bounds(table) is None
                )
            return cache[key]

        missing = tuple(
            n for n in (*rule.requires_schema, *rule.requires_rows) if not exists(n)
        )
        if missing:
            return UncoveredRule(
                rule.rule_id, rule.title, CoverageGap.SCHEMA_MISSING, missing
            )
        empty = tuple(t for t in rule.requires_rows if not has_rows(t))
        if empty:
            return UncoveredRule(
                rule.rule_id, rule.title, CoverageGap.NOT_RECORDED, empty
            )
        return None

    def _load_extra_rules(self, reasoning: list[str]) -> None:
        """Import ``config.rules.extra_rules`` so their @register decorators fire.

        Load failures are recorded in *reasoning* (surfaced by ``--verbose`` and
        always present in the JSON report) and never abort the diagnosis.
        """
        extra = self.config.rules.extra_rules
        if not extra:
            return
        for name, ok, msg in load_extra_rules(extra):
            if not ok:
                reasoning.append(f"追加ルール {name} の読み込みに失敗しました: {msg}")
            elif self.verbose:
                reasoning.append(f"追加ルール {name} を読み込みました")

    def _determine_window(
        self, store: "Storage", reasoning: list[str]
    ) -> tuple[Window, bool]:
        """Derive the observation window and whether the DB held any samples.

        Returns ``(window, has_data)``.  ``has_data`` is False only when neither
        ``sys_samples`` nor ``sys_rollup_1m`` contained a single row; in that
        case the window is a zero-span placeholder and the caller must treat
        the result as "no observation", **not** as "healthy" (otherwise an empty
        DB would render a green『問題なし』and exit 0 — a false negative).
        """
        bounds = store.window_bounds()
        if bounds is None:
            # No data at all — zero-span placeholder; signalled via has_data.
            now = time.time()
            return Window(start_ts=now, end_ts=now), False

        lo, hi, used_fallback = bounds
        if used_fallback:
            reasoning.append(
                "sys_samples が空のため sys_rollup_1m でウィンドウを決定しました"
            )
        return Window(start_ts=lo, end_ts=hi), True

    def _render_proposal(self, finding: Finding, reasoning: list[str]) -> str:
        """Render the Jinja2 proposal template for *finding*."""
        template_name = f"{finding.rule_id}.j2"
        try:
            tmpl = self._jinja.get_template(template_name)
            return tmpl.render(evidence=finding.evidence).strip()
        except Exception as exc:
            msg = (
                f"テンプレート {template_name} のレンダリングに失敗しました: "
                f"{type(exc).__name__}: {exc}"
            )
            reasoning.append(msg)
            return f"[提案テンプレートの生成に失敗: {exc}]"

    def _build_quality(
        self, store: "Storage", window: Window, *, events_in_window: bool
    ) -> ObservationQuality:
        """Assemble ObservationQuality from DB statistics over *window*."""
        span_hours = window.span_hours
        since, until = (
            (window.start_ts, window.end_ts) if events_in_window else (None, None)
        )

        # Gap total from ops_events: sum the delay seconds recorded in each
        # gap event's detail ("<delay>s ジョブ<name>"), not the event count.
        gap_rows = store.rows(
            "ops_events", since=since, until=until, where="kind = 'gap'"
        )
        gap_seconds = sum(_parse_gap_delay_seconds(r["detail"]) for r in gap_rows)

        # Self CPU from sys_samples — aggregated in SQL so a long recording is
        # not materialised into Python just to compute avg/max.
        self_cpu_avg, self_cpu_max = store.self_cpu_stats(
            window.start_ts, window.end_ts
        )

        throttle_events = len(
            store.rows(
                "ops_events", since=since, until=until, where="kind = 'throttle_down'"
            )
        )
        missing_io_pct = store.missing_io_ratio(window.start_ts, window.end_ts)

        notes: list[str] = []
        if missing_io_pct > 0.1:
            notes.append(
                f"io_counters 欠損率 {missing_io_pct:.0%} — "
                "非 root 実行のため他ユーザープロセスの I/O が読めません"
            )

        return ObservationQuality(
            span_hours=span_hours,
            gap_seconds=gap_seconds,
            missing_io_pct=missing_io_pct,
            self_cpu_avg=self_cpu_avg if self_cpu_avg is not None else 0.0,
            self_cpu_max=self_cpu_max if self_cpu_max is not None else 0.0,
            throttle_events=throttle_events,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# fold() — Finding consolidation
# ---------------------------------------------------------------------------


def fold(findings: list[Finding]) -> list[Finding]:
    """Absorb child findings into their parent Finding's ``related`` list.

    Currently: DISK-GROWTH-01 findings whose ``subject`` (mountpoint) matches
    a DISK-FULL-01 finding are removed from the top-level list and their
    ``rule_id`` is appended to the parent's ``related``.

    The fold table ``_FOLD_INTO`` is a plain dict and can be extended for
    future rule relationships without changing this function.
    """
    # Build an index of parent findings by (rule_id, subject).
    parent_index: dict[tuple[str, str], Finding] = {}
    for f in findings:
        parent_id = _FOLD_INTO.get(f.rule_id)
        if parent_id is None:
            # This finding can be a parent — index it.
            parent_index[(f.rule_id, f.subject)] = f

    kept: list[Finding] = []
    for f in findings:
        parent_rule_id = _FOLD_INTO.get(f.rule_id)
        if parent_rule_id is None:
            kept.append(f)
            continue

        # This finding should be absorbed by the parent if a matching parent
        # exists with the same subject.
        parent = parent_index.get((parent_rule_id, f.subject))
        if parent is not None:
            parent.related.append(f.rule_id)
            # Do not add this finding to the kept list.
        else:
            # No parent to fold into — keep as-is.
            kept.append(f)

    return kept
