"""JSON payload contract of the ``spike_events`` table.

A spike row carries the culprit snapshot inline as JSON so it stays readable
even after the raw samples it was built from have been pruned.  The recorder
writes these payloads and both the report and CPU-SATURATION-01 read them back,
so the encoding lives here — in one place — instead of being re-implemented by
each side (a reader that guessed the wrong shape silently failed in the past).

Shapes::

    top_processes = {"by_cpu": [entry, ...], "by_rss": [entry, ...]}
        entry     = {"ts", "proc_id", "pid", "cpu_pct", "rss"}
    top_cgroups   = {"by_mem": [{"ts", "cg_id", "mem_current"}, ...]}
    psi_snapshot  = SystemSnapshot.psi  (e.g. {"cpu_some10": 23.4, ...}) or {}

Entries hold ids, not names (the recorder keeps the row lean); readers resolve
``proc_id`` through ``Storage.proc_name``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from .storage import Storage

_TOP_PROCESSES = 10
_TOP_CGROUPS = 5
# How many of a spike's top-CPU processes count as its culprits — the ones the
# HTML spike table names and CPU-SATURATION-01 tallies.
CULPRITS_PER_SPIKE = 3


def _top(rows: list[dict], key: str, n: int) -> list[dict]:
    """The *n* rows with the largest *key* (NULL sorts as 0)."""
    return sorted(
        rows, key=lambda d: d[key] if d[key] is not None else 0, reverse=True
    )[:n]


def encode_top_processes(proc_rows: Iterable[dict]) -> str:
    """Encode the top processes by CPU and by RSS from ``proc_samples`` rows."""
    rows = list(proc_rows)

    def pick(d: dict) -> dict:
        return {k: d[k] for k in ("ts", "proc_id", "pid", "cpu_pct", "rss")}

    return json.dumps(
        {
            "by_cpu": [pick(d) for d in _top(rows, "cpu_pct", _TOP_PROCESSES)],
            "by_rss": [pick(d) for d in _top(rows, "rss", _TOP_PROCESSES)],
        }
    )


def encode_top_cgroups(cg_rows: Iterable[dict]) -> str:
    """Encode the top cgroups by memory from ``cg_samples`` rows."""
    by_mem = _top(list(cg_rows), "mem_current", _TOP_CGROUPS)
    return json.dumps(
        {"by_mem": [{k: d[k] for k in ("ts", "cg_id", "mem_current")} for d in by_mem]}
    )


def encode_psi_snapshot(psi: Optional[dict]) -> str:
    """Encode the PSI snapshot that fired the trigger (``{}`` without PSI)."""
    return json.dumps(psi) if psi is not None else "{}"


def decode_top_processes(raw: Any) -> list[dict]:
    """Return the ``by_cpu`` entries of a stored ``top_processes`` payload.

    Defensive by design: a NULL, malformed or differently shaped payload yields
    an empty list instead of raising, so one bad row never aborts a report or a
    rule.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    by_cpu = data.get("by_cpu", []) if isinstance(data, dict) else []
    return [e for e in by_cpu if isinstance(e, dict)]


def culprits(
    raw: Any, store: "Storage", *, limit: int = CULPRITS_PER_SPIKE
) -> list[dict]:
    """Resolve a spike's top-CPU entries to named culprits.

    Returns up to *limit* dicts with ``name`` / ``pid`` / ``cpu_pct`` / ``rss``.
    A failed name lookup degrades to ``proc_<id>`` rather than raising.
    """
    out: list[dict] = []
    for entry in decode_top_processes(raw)[:limit]:
        proc_id = entry.get("proc_id")
        try:
            name = store.proc_name(proc_id) if proc_id is not None else "?"
        except sqlite3.Error:
            name = f"proc_{proc_id}"
        out.append(
            {
                "name": name,
                "pid": entry.get("pid"),
                "cpu_pct": entry.get("cpu_pct"),
                "rss": entry.get("rss"),
            }
        )
    return out
