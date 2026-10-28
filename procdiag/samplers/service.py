"""Tier-3 service sampler: systemd automatic restarts and failed units.

A service that keeps crashing and being restarted by systemd (``Restart=``)
looks healthy at any single moment — it is "active" again a few seconds later —
so the crash loop is invisible to point-in-time checks.  systemd counts those
automatic restarts per unit (``NRestarts``); this sampler polls the counter
every few minutes and records an event only when it grows or a unit enters the
``failed`` state, so the table stays tiny.

The cgroup counters are deliberately *not* used for this: a timer-driven
oneshot service (logrotate, apt-daily, …) gets a fresh cgroup on every normal
run, which would read as a restart.  ``NRestarts`` only counts restarts caused
by the ``Restart=`` policy (i.e. after a failure).

Capability-gated: without systemd the sampler records nothing.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from .. import systemd
from ..core.sampler import RunContext, SampleBatch, Sampler, register_sampler
from ..storage import make_row


@register_sampler
class ServiceSampler(Sampler):
    """Tier-3: record automatic restarts and failed transitions of services."""

    name = "service"
    title = "systemd サービスの再起動・failed"
    tier = "T3"
    interval_key = "service_interval"
    what = "systemd が自動再起動した回数 (NRestarts) の増加と failed 状態への遷移を記録する"
    why = (
        "クラッシュ→自動再起動を繰り返すサービスは一瞬で active に戻るため、"
        "その時点の状態確認では見つからない。systemd の再起動回数で検出する"
    )
    manual_commands = [
        "systemctl --failed",
        "systemctl show -p NRestarts,Result <unit>",
        "journalctl -u <unit> -n 50",
    ]

    def __init__(self) -> None:
        self._enabled = False
        self._restarts: Dict[str, int] = {}  # unit -> last seen NRestarts
        self._failed: Dict[str, bool] = {}  # unit -> was failed at last poll

    def setup(self, ctx: RunContext) -> None:
        """Enable only on systemd hosts."""
        self._enabled = systemd.available()
        ctx.capabilities["systemd"] = self._enabled
        if not self._enabled:
            ctx.notes.append(
                "service: systemd が見つからないため、サービス再起動の記録をスキップします"
            )

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Poll ``systemctl show`` and emit restart / failed events."""
        if not self._enabled:
            return []
        units = systemd.list_service_units()
        info = systemd.show_units(units) if units else None
        if not info:
            return []

        ts = time.time()
        rows: list[tuple] = []
        for unit, props in info.items():
            result = props.get("Result") or None
            n_restarts = systemd.n_restarts(props)
            if n_restarts is not None:
                prev = self._restarts.get(unit)
                # NRestarts is reset by a manual start; a drop is a new baseline.
                if prev is not None and n_restarts > prev:
                    rows.append(
                        self._event(ts, unit, "restart", n_restarts - prev, result)
                    )
                self._restarts[unit] = n_restarts
            failed = props.get("ActiveState") == "failed"
            if failed and not self._failed.get(unit, False):
                rows.append(self._event(ts, unit, "failed", 0, result))
            self._failed[unit] = failed

        return [SampleBatch(table="service_events", rows=rows)] if rows else []

    @staticmethod
    def _event(ts: float, unit: str, kind: str, n: int, result: Optional[str]) -> tuple:
        return make_row(
            "service_events", ts=ts, unit=unit, kind=kind, n_restarts=n, result=result
        )
