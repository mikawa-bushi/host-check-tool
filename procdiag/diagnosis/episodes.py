"""Minutes of sustained pressure, read off the time the samples cover.

The system sampler writes a row every second (``sampling.system_interval``), so
"N minutes of pressure" can be neither the number of rows nor the distance
between the first and last row.  A minute counts when the pressure held for at
least half of it, measured as rows times the typical sample interval.  Seconds
of pressure inside a minute with nothing recorded do not count, and a minute
that was not recorded at all ends the run (``find_sustained_episodes`` breaks a
run at any missing minute), so a recorder stop is never added up as pressure.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .trend import find_sustained_episodes

MINUTE = 60.0
HELD_SHARE = 0.5


def sample_interval(timestamps: Sequence[float]) -> float:
    """Typical spacing of the ascending *timestamps* in seconds.

    The median step, so recording gaps do not stretch it.  One minute when
    there are fewer than two distinct timestamps.
    """
    steps = sorted(b - a for a, b in zip(timestamps, timestamps[1:]) if b > a)
    return steps[len(steps) // 2] if steps else MINUTE


def minute_counts(
    stamped: Iterable[tuple[float, bool]],
) -> dict[int, tuple[int, int]]:
    """``{minute start: (rows, hot rows)}`` for ``(ts, is_hot)`` samples."""
    counts: dict[int, tuple[int, int]] = {}
    for ts, hot in stamped:
        minute = int(ts // MINUTE) * int(MINUTE)
        rows, hot_rows = counts.get(minute, (0, 0))
        counts[minute] = (rows + 1, hot_rows + hot)
    return counts


def held(rows: int, interval: float) -> bool:
    """Whether *rows* samples, *interval* seconds apart, cover half a minute."""
    return rows * interval >= MINUTE * HELD_SHARE


def sustained_minutes(
    stamped: Sequence[tuple[float, bool]], *, min_minutes: int
) -> list[tuple[float, float]]:
    """``(first minute start, last minute start)`` of each qualifying run.

    *stamped* is ascending ``(ts, is_hot)``.  A run is consecutive minutes in
    which the hot samples covered half the minute, at least *min_minutes* long.
    """
    interval = sample_interval([ts for ts, _ in stamped])
    minutes = [
        (minute, float(minute), held(hot_rows, interval))
        for minute, (_, hot_rows) in sorted(minute_counts(stamped).items())
    ]
    return find_sustained_episodes(minutes, min_consecutive=min_minutes)
