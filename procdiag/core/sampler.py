"""Sampler base class, RunContext, shared types, and registry.

Maps netdiag's ``core/probe.py`` Probe pattern to the sampler domain.
A Sampler records facts from the OS into SampleBatch rows; it never
interprets or diagnoses.  Unlike netdiag Probes (which are stateless and
discarded after each run), Sampler *instances* live for the full duration of
a ``record`` session because Tier-2b needs a persistent psutil Process
cache and Tier-2a needs cumulative cgroup delta state.

``RunContext`` carries the per-session shared state (config, filesystem roots
swappable for tests, capability flags) analogous to netdiag's ``Context``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Type

if TYPE_CHECKING:
    from ..config import ProcdiagConfig


@dataclass
class SampleBatch:
    """One collector's output for a single tick.

    ``table`` must be a key in ``storage.TABLE_COLUMNS``; ``rows`` are tuples
    whose element order exactly matches the column list for that table.
    Samplers build them with ``storage.make_row`` (named columns) rather than
    ordering tuples by hand.
    """

    table: str
    rows: list[tuple]


@dataclass
class SystemSnapshot:
    """Typed view of a Tier-1 system sample consumed by the trigger and selfmeter.

    All numeric fields use the same units and sign conventions as the
    corresponding ``sys_samples`` columns so downstream code can be written
    against this type and the DB interchangeably.
    """

    ts: float
    cpu_total: float  # 0-100 %
    cpu_max_core: float  # 0-100 %
    mem_total: int  # bytes
    mem_available: int  # bytes
    swap_out_delta: int  # bytes of swap-out since the previous collect
    psi: Optional[Dict[str, float]]
    # PSI keys: "cpu_some10", "mem_some10", "mem_full10", "io_some10", "io_full10"
    # None when the kernel does not support PSI


class RunContext:
    """Shared state for one record session.

    All samplers receive the same RunContext so they can coordinate (e.g.
    the T1 sampler deposits a SystemSnapshot that the trigger reads, the
    recorder attaches the SelfMeter instance that T1 then uses to record
    self-CPU overhead).

    ``proc_root`` and ``cgroup_root`` default to the real paths but can be
    replaced with a fixture directory in tests — the same injection pattern
    as netdiag's ``Context.config`` carrying the target list.
    """

    def __init__(
        self,
        config: "ProcdiagConfig",
        *,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> None:
        self.config = config
        self.proc_root = proc_root
        self.cgroup_root = cgroup_root
        # Capability flags filled in by each sampler's setup():
        #   "psi"       — /proc/pressure/* readable
        #   "cgroup_v2" — unified cgroup hierarchy present
        #   "root"      — process is running as root (uid 0)
        self.capabilities: Dict[str, bool] = {}
        # Demotion notes transcribed into ObservationQuality.notes at report time.
        self.notes: List[str] = []
        # SelfMeter instance attached by the recorder; T1 sampler reads it.
        self.selfmeter: Any = None  # SelfMeter | None


class Sampler:
    """Base class for all samplers.

    Subclasses set class-level attributes (same placement as Probe) and
    implement ``collect``.  ``run`` wraps ``collect`` with a catch-all that
    degrades one tick's failure to a note rather than killing the record loop
    — the same defensive wall as ``Probe.run`` in netdiag.
    """

    name: str = ""
    title: str = ""
    tier: str = ""  # "T1" | "T2a" | "T2b" | "T3"
    interval_key: str = ""  # attribute name on SamplingConfig, e.g. "system_interval"
    requires_root: bool = False
    what: str = ""
    why: str = ""
    manual_commands: list[str] = []

    def setup(self, ctx: RunContext) -> None:
        """One-time initialisation called before the first collect.

        Subclasses probe capabilities (PSI, cgroup v2, root) and record
        demotion reasons in ``ctx.notes``.  The base implementation does
        nothing; override only what the sampler actually needs.
        """

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Collect one tick's worth of samples and return them as batches.

        Must be overridden.  Must not raise — unrecoverable errors should be
        silenced here and noted in ``ctx.notes``.
        """
        raise NotImplementedError(
            f"サンプラー {self.__class__.__name__} は collect() を実装していません"
        )

    def run(self, ctx: RunContext) -> list[SampleBatch]:
        """Collect with a catch-all defensive wall (mirrors Probe.run).

        Any exception from ``collect`` is caught, a note is appended to
        ``ctx.notes``, and an empty list is returned so the record loop
        continues uninterrupted.

        ``ctx.notes`` is never drained during a record session, so under a
        *persistent* failure (PSI vanished, recurring OSError) appending one
        note per tick would grow it without bound — an unbounded resident-memory
        leak that ``selfmeter`` cannot throttle (it watches CPU, not RSS).
        Consecutive identical notes from the same sampler are therefore
        de-duplicated; a successful collect resets the guard so a *new* failure
        is still recorded once.
        """
        try:
            batches = self.collect(ctx)
            self._last_run_note = None
            return batches
        except Exception as e:  # never let one sampler kill the loop
            note = f"{self.name}: collect失敗 ({e})"
            if getattr(self, "_last_run_note", None) != note:
                ctx.notes.append(note)
                self._last_run_note = note
            return []


# --- registry -----------------------------------------------------------------

_SAMPLER_REGISTRY: Dict[str, Type[Sampler]] = {}


def register_sampler(cls: Type[Sampler]) -> Type[Sampler]:
    """Class decorator: add a sampler to the global registry by its ``name``."""
    if not cls.name:
        raise ValueError(
            f"サンプラークラス {cls.__name__} に name が設定されていません"
        )
    _SAMPLER_REGISTRY[cls.name] = cls
    return cls


def get_sampler(name: str) -> Optional[Type[Sampler]]:
    """Look up a sampler class by name, or return None."""
    return _SAMPLER_REGISTRY.get(name)


def all_samplers() -> Dict[str, Type[Sampler]]:
    """Return a shallow copy of the full sampler registry."""
    return dict(_SAMPLER_REGISTRY)


def sampler_registry_names() -> List[str]:
    """Return sorted list of registered sampler names."""
    return sorted(_SAMPLER_REGISTRY)
