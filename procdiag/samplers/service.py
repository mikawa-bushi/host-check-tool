"""Tier-3 service sampler: systemd automatic restarts and failed units.

A service that keeps crashing and being restarted by systemd (``Restart=``)
looks healthy at any single moment — it is "active" again a few seconds later —
so the crash loop is invisible to point-in-time checks.  systemd counts those
automatic restarts per unit (``NRestarts``); this sampler polls the counter
every few minutes and records an event only when it grows or a unit enters the
``failed`` state, so the table stays tiny.

The cgroup counters are deliberately *not* used for this: a timer-driven
oneshot service (logrotate, apt-daily, …) gets a fresh cgroup on every normal
run, which would read as a restart.  ``NRestarts`` only counts restarts done
by the ``Restart=`` policy.

Under ``Restart=always`` (or ``RestartForceExitStatus=``) a restart may follow
a clean exit.  ``systemctl show`` cannot tell: ``Result`` and
``ExecMainStatus`` describe only the current run once the unit has started
again.  The journal can: systemd logs "Failed with result" for every abnormal
end.  Without permission to read it, such restarts are recorded as
``restart_maybe_clean``.

Capability-gated: without systemd the sampler records nothing.
"""

from __future__ import annotations

import time
from typing import Dict, Mapping, Optional

from .. import kernlog, systemd
from ..core.result import Capability
from ..core.sampler import RunContext, SampleBatch, Sampler, register_sampler
from ..storage import make_row

# Restart= policy -> what every restart it does followed (systemd.service(5)).
# None: clean and abnormal exits alike, so the journal has to tell.  Policies
# not listed restart only after an abnormal exit.
_RESTART_KIND = {"always": None, "on-success": "restart_clean"}
# systemd counts a restart RestartUSec after logging the failure that caused
# it; the margin covers timer slack and a slow start on a loaded host.
_FAILURE_MARGIN_SEC = 10.0
# LogLevelMax= below this (syslog "warning") also drops systemd's own
# "Failed with result" line about the unit.
_WARNING_LEVEL = 4


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
        self._journal = False
        self._restarts: Dict[str, int] = {}  # unit -> last seen NRestarts
        self._failed: Dict[str, bool] = {}  # unit -> was failed at last poll
        self._last_poll: Optional[float] = None
        self._matched: Dict[str, float] = {}  # unit -> last failure line matched

    def setup(self, ctx: RunContext) -> None:
        """Enable only on systemd hosts."""
        self._enabled = systemd.available()
        self._journal = self._enabled and kernlog.journal_readable()
        ctx.capabilities[Capability.SYSTEMD.value] = self._enabled
        if not self._enabled:
            ctx.lacks(
                Capability.SYSTEMD,
                "service: systemd が見つからないため、サービス再起動の記録をスキップします",
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
        since, self._last_poll = self._last_poll, ts
        rows: list[tuple] = []
        unclassified: Dict[str, tuple[int, float, Optional[str]]] = {}
        for unit, props in info.items():
            result = props.get("Result") or None
            n_restarts = systemd.n_restarts(props)
            if n_restarts is not None:
                prev = self._restarts.get(unit, n_restarts)
                # A manual start or reset-failed sets NRestarts back to 0, so a
                # lower value is the number of restarts since then.
                new = n_restarts - prev if n_restarts >= prev else n_restarts
                kind = (
                    None
                    if props.get("RestartForceExitStatus")
                    else _RESTART_KIND.get(props.get("Restart"), "restart")
                )
                if new and kind:
                    rows.append(self._event(ts, unit, kind, new, result))
                elif new and _log_level(props) < _WARNING_LEVEL:
                    rows.append(
                        self._event(ts, unit, "restart_maybe_clean", new, result)
                    )
                elif new:
                    delay = systemd.timespan_sec(props.get("RestartUSec")) or 0.0
                    lo = since - delay - _FAILURE_MARGIN_SEC
                    lo = max(lo, self._matched.get(unit, lo))
                    unclassified[unit] = (new, lo, result)
                self._restarts[unit] = n_restarts
            failed = props.get("ActiveState") == "failed"
            if failed and not self._failed.get(unit, False):
                kind = "failed_at_start" if since is None else "failed"
                rows.append(self._event(ts, unit, kind, 0, result))
            self._failed[unit] = failed

        failures = None
        if unclassified and self._journal:
            failures = systemd.unit_failures(
                unclassified, min(lo for _, lo, _ in unclassified.values())
            )
        for unit, (new, lo, result) in unclassified.items():
            if failures is None:
                rows.append(self._event(ts, unit, "restart_maybe_clean", new, result))
                continue
            caused = [f for f in failures.get(unit, []) if lo < f[0] <= ts][:new]
            if caused:
                self._matched[unit] = caused[-1][0]
                rows.append(
                    self._event(ts, unit, "restart", len(caused), caused[-1][1])
                )
            if new > len(caused):
                rows.append(
                    self._event(ts, unit, "restart_clean", new - len(caused), None)
                )

        return [SampleBatch(table="service_events", rows=rows)] if rows else []

    @staticmethod
    def _event(ts: float, unit: str, kind: str, n: int, result: Optional[str]) -> tuple:
        return make_row(
            "service_events", ts=ts, unit=unit, kind=kind, n_restarts=n, result=result
        )


def _log_level(props: Mapping[str, str]) -> int:
    """The unit's LogLevelMax= as a syslog level; 7 (debug) when unset."""
    try:
        level = int(props.get("LogLevelMax") or -1)
    except ValueError:
        return 7
    return 7 if level < 0 else level
