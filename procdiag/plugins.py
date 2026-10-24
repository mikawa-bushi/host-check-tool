"""Third-party rule discovery via entry points.

Mirrors netdiag's ``plugins.py`` with the group name changed to
``procdiag.rules``.  A plugin package declares its rules in its own
``pyproject.toml``:

    [project.entry-points."procdiag.rules"]
    my_rule = "mypkg.module"

Importing the named module is sufficient — the ``@register`` decorators in
that module add the rules to the global registry just like the built-ins do.

The extra ``load_extra_rules`` function handles the ``config.rules.extra_rules``
list: each entry is an import path (e.g. ``"mysite.rules.custom"``) that gets
imported with ``importlib``.
"""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import List, Tuple

ENTRY_POINT_GROUP = "procdiag.rules"


def load_plugins() -> List[Tuple[str, bool, str]]:
    """Import every registered plugin module via the entry-point group.

    Returns a list of ``(name, ok, message)`` so the CLI can report what
    loaded and what failed without aborting on a single bad plugin.
    """
    results: List[Tuple[str, bool, str]] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            target = ep.load()
            # If the entry point resolves to a callable (rather than a module),
            # call it so any deferred registration logic runs.
            if callable(target):
                target()
            results.append((ep.name, True, "loaded"))
        except Exception as e:  # never let a plugin break the tool
            results.append((ep.name, False, str(e)))

    return results


def load_extra_rules(import_paths: List[str]) -> List[Tuple[str, bool, str]]:
    """Import each path in ``config.rules.extra_rules``.

    Each entry should be a fully-qualified Python module path (e.g.
    ``"mysite.procdiag_rules"``).  The module is imported with
    ``importlib.import_module``; its ``@register`` decorators fire on import.

    Returns the same ``(name, ok, message)`` structure as :func:`load_plugins`.
    """
    results: List[Tuple[str, bool, str]] = []
    for path in import_paths:
        try:
            importlib.import_module(path)
            results.append((path, True, "loaded"))
        except Exception as e:  # never let one bad rule break the rest
            results.append((path, False, str(e)))
    return results
