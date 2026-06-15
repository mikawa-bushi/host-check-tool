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
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from jinja2 import Environment, FileSystemLoader, UndefinedError

from ..core import (
    Confidence,
    Diagnosis,
    Finding,
    ObservationQuality,
    Rule,
    Severity,
    Window,
    all_rules,
)
from ..config import ProcdiagConfig
from ..plugins import load_extra_rules

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

    def run(
        self,
        store: "Storage",
        only: Optional[list[str]] = None,
        skip: Optional[list[str]] = None,
    ) -> Diagnosis:
        """Evaluate all selected rules and return a :class:`Diagnosis`.

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
           SWAP-THRASH-01) use their own value.
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
        window = self._determine_window(store, reasoning)

        if self.verbose:
            reasoning.append(
                f"観測ウィンドウ: {window.span_hours:.1f}h "
                f"({window.start_ts:.0f} – {window.end_ts:.0f})"
            )

        # --- Step 2: run rules (per-rule observation guard) ---
        base_min_hours = Rule.min_observation_hours
        rule_classes = self.select(only=only, skip=skip)
        raw_findings: list[Finding] = []

        for rule_cls in rule_classes:
            rule = rule_cls()
            eff_min = (
                self.config.rules.min_observation_hours
                if rule.min_observation_hours == base_min_hours
                else rule.min_observation_hours
            )
            if window.span_hours < eff_min:
                reasoning.append(
                    f"{rule.rule_id}: 観測時間不足のためスキップ "
                    f"({window.span_hours:.1f}h < {eff_min:.1f}h)"
                )
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
                continue

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
        findings = _fold(raw_findings)

        # --- Step 5: sort ---
        findings.sort(
            key=lambda f: (
                _SEVERITY_ORDER.get(f.severity, 99),
                _CONFIDENCE_ORDER.get(f.confidence, 99),
            )
        )

        # --- Step 6: build quality ---
        quality = self._build_quality(store, window)

        healthy = not any(
            f.severity in (Severity.WARNING, Severity.CRITICAL) for f in findings
        )

        return Diagnosis(
            findings=findings,
            healthy=healthy,
            reasoning=reasoning,
            quality=quality,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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

    def _determine_window(self, store: "Storage", reasoning: list[str]) -> Window:
        """Derive the observation window from the DB's earliest/latest data."""
        # Try sys_samples first (raw), then fall back to rollup.
        row = store.conn.execute(
            "SELECT MIN(ts) as t_min, MAX(ts) as t_max FROM sys_samples"
        ).fetchone()

        if row and row["t_min"] is not None:
            return Window(start_ts=row["t_min"], end_ts=row["t_max"])

        # Fall back to sys_rollup_1m.
        row = store.conn.execute(
            "SELECT MIN(ts_min) as t_min, MAX(ts_min) as t_max FROM sys_rollup_1m"
        ).fetchone()

        if row and row["t_min"] is not None:
            reasoning.append(
                "sys_samples が空のため sys_rollup_1m でウィンドウを決定しました"
            )
            return Window(start_ts=row["t_min"], end_ts=row["t_max"])

        # No data at all — return a zero-span window (guard will catch it).
        import time

        now = time.time()
        return Window(start_ts=now, end_ts=now)

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

    def _build_quality(self, store: "Storage", window: Window) -> ObservationQuality:
        """Assemble ObservationQuality from DB statistics."""
        span_hours = window.span_hours

        # Gap total from ops_events: sum the delay seconds recorded in each
        # gap event's detail ("<delay>s ジョブ<name>"), not the event count.
        gap_rows = store.rows(
            "ops_events",
            where="kind = 'gap'",
        )
        gap_seconds = sum(_parse_gap_delay_seconds(r["detail"]) for r in gap_rows)

        # Self CPU from sys_samples.
        sys_rows = store.rows(
            "sys_samples",
            since=window.start_ts,
            until=window.end_ts,
        )
        self_cpu_values = [r["self_cpu"] for r in sys_rows if r["self_cpu"] is not None]
        self_cpu_avg = (
            sum(self_cpu_values) / len(self_cpu_values) if self_cpu_values else 0.0
        )
        self_cpu_max = max(self_cpu_values) if self_cpu_values else 0.0

        # Throttle events.
        throttle_rows = store.rows(
            "ops_events",
            where="kind = 'throttle_down'",
        )
        throttle_events = len(throttle_rows)

        # Missing IO percentage from proc_samples.
        proc_count_row = store.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN io_read_delta IS NULL THEN 1 ELSE 0 END) AS missing "
            "FROM proc_samples WHERE ts >= ? AND ts <= ?",
            (window.start_ts, window.end_ts),
        ).fetchone()
        if proc_count_row and proc_count_row["total"] > 0:
            missing_io_pct = (proc_count_row["missing"] or 0) / proc_count_row["total"]
        else:
            missing_io_pct = 0.0

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
            self_cpu_avg=self_cpu_avg,
            self_cpu_max=self_cpu_max,
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
    return _fold(findings)


def _fold(findings: list[Finding]) -> list[Finding]:
    """Internal implementation of finding folding."""
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
