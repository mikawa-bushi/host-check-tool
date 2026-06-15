"""Built-in samplers for procdiag.

Eager-importing the sampler modules here populates the global sampler registry
(via ``@register_sampler`` decorators) so that ``procdiag explain``,
``procdiag list-samplers``, and any code that calls
:func:`~procdiag.core.sampler.all_samplers` can discover all four built-in
samplers without having to import each module by name.

This mirrors the idiom in ``netdiag.probes.__init__`` where every probe module
is imported for its registration side-effect.  Only the four sampler modules
are imported here; ``trigger`` and ``selfmeter`` are not samplers and are
wired up directly by the :class:`~procdiag.recorder.Recorder`.
"""

from . import (  # noqa: F401  (imported for @register_sampler side-effects)
    cgroup,
    disk,
    process,
    system,
)

__all__ = ["system", "cgroup", "process", "disk"]
