"""Resolve generic health-check Incidents only after a quiet period (WP1.3).

Generic Ceph health checks flap: on 2026-10-02/03, while one MON host was
unstable, 135 generic Incidents lived a median of seconds and 55% reopened
within 30 minutes of resolving (MON_DOWN, SLOW_OPS, OSD_DOWN, PG_DEGRADED...),
each with a new Telegram alert and a new diagnosis. Replaying that day, a
30-minute grace before resolving merges a returning check into the Incident
still open: 74 -> 19 reopens, 135 -> 61 Incidents.

State is per process and in memory: a restart only restarts the clock, which
delays resolution by at most one grace period. Two processes polling the same
cluster each keep their own clock; whichever reaches the grace first resolves.
"""

from __future__ import annotations

import threading
from datetime import datetime

from config.settings import settings
from shared.time import utc_now

PRESENT = "present"
HOLD = "hold"
RECURRED = "recurred"
RESOLVE = "resolve"

_absent_since: dict[tuple[str | None, str], datetime] = {}
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        _absent_since.clear()


def decide(cluster_id: str | None, ceph_code: str, present: bool, now: datetime | None = None) -> str:
    """One poll's verdict for an open Incident whose code is ``present`` or not.

    ``RECURRED`` is returned once when a check comes back during the grace,
    so the caller can audit the suppressed reopen.
    """
    grace = settings.incident_resolve_grace_seconds
    key = (cluster_id, ceph_code)
    with _lock:
        if present:
            if _absent_since.pop(key, None) is not None:
                return RECURRED
            return PRESENT
        if grace <= 0:
            return RESOLVE
        now = now or utc_now()
        since = _absent_since.setdefault(key, now)
        if (now - since).total_seconds() < grace:
            return HOLD
        del _absent_since[key]
        return RESOLVE
