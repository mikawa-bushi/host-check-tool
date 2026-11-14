"""Recording-DB overview shared by ``procdiag status`` and ``procdiag bundle``.

Everything here is a SQL aggregate or a bounded query, so it stays instant
even on a long recording (no full ``sys_samples`` load).  The recorded span
and "has data" cover the 1-minute rollups too: the raw samples expire first,
and a DB holding only rollups is still a recording.  The size includes the
``-wal`` file, where a running recorder's newest rows live.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from .core.util import humanize_duration
from .storage import Storage

TIME_FMT = "%Y-%m-%d %H:%M:%S"
_RECENT_EVENTS = 5


def collect_status(store: Storage) -> Dict[str, Any]:
    """Return the ``procdiag status --format json`` payload for *store*."""
    counts = store.counts()
    try:
        counts["db_bytes"] += Path(f"{store.path}-wal").stat().st_size
    except OSError:
        pass
    self_cpu_avg, self_cpu_max = store.self_cpu_stats()
    bounds = store.system_bounds()

    span = start = end = "—"
    if bounds is not None:
        lo, hi = bounds
        span = humanize_duration(hi - lo)
        start = time.strftime(TIME_FMT, time.localtime(lo))
        end = time.strftime(TIME_FMT, time.localtime(hi))

    return {
        "meta": store.meta(),
        "counts": counts,
        "has_data": counts.get("sys_samples", 0) + counts.get("sys_rollup_1m", 0) > 0,
        "span": span,
        "start": start,
        "end": end,
        "self_cpu_avg": self_cpu_avg,
        "self_cpu_max": self_cpu_max,
        "recent_events": [
            {"ts": r["ts"], "kind": r["kind"], "detail": r["detail"]}
            for r in store.recent_events(_RECENT_EVENTS)
        ],
    }
