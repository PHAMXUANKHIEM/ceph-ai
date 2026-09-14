"""Publish the Watcher's critical health result to the shared snapshot store.

The Watcher already owns one polling loop per active cluster.  This module
keeps snapshot shaping separate from incident logic and deliberately reuses
the health payload already fetched by that loop, so publishing a snapshot
does not add another SSH/Ceph round trip.
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from threading import Lock
from typing import Mapping

from shared import ceph_query_cache
from shared.cluster_snapshot import (
    DEFAULT_MAX_STALE_SECONDS,
    SNAPSHOT_NAMESPACE,
    publish_snapshot,
    read_snapshot,
)

SNAPSHOT_SOURCE = "watcher-critical-health"
_METRICS_LOCK = Lock()
_METRICS = {"success_total": 0, "failure_total": 0}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error_text(error: object) -> str:
    return (str(error) or type(error).__name__)[:1000]


def get_metrics() -> dict[str, int]:
    """Return process-local collector counters for diagnostics/metrics export."""
    with _METRICS_LOCK:
        return dict(_METRICS)


def _record_metric(name: str) -> None:
    with _METRICS_LOCK:
        _METRICS[name] += 1


def _sections_from_health(health: Mapping[str, object], duration_ms: float | None) -> dict:
    checks = health.get("checks")
    sections = {
        "health": dict(health),
        "health_status": health.get("status"),
        "health_checks": dict(checks) if isinstance(checks, Mapping) else {},
        "collection": {
            "tier": "critical",
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        },
    }
    return sections


def publish_health_snapshot(
    cluster_id: str | None,
    health: Mapping[str, object],
    *,
    collection_started_monotonic: float | None = None,
) -> dict | None:
    """Publish one successful health result; return ``None`` for legacy loops."""
    if not cluster_id:
        return None
    if not isinstance(health, Mapping):
        raise TypeError("health must be a mapping")
    duration_ms = None
    if collection_started_monotonic is not None:
        duration_ms = max(0.0, (monotonic() - collection_started_monotonic) * 1000)
    try:
        snapshot = publish_snapshot(
            cluster_id,
            _sections_from_health(health, duration_ms),
            collected_at=_utc_now(),
            source=SNAPSHOT_SOURCE,
        )
    except Exception:
        _record_metric("failure_total")
        raise
    _record_metric("success_total")
    return snapshot


def publish_health_error(cluster_id: str | None, error: object) -> dict | None:
    """Retain the last good health payload while recording the failed attempt."""
    if not cluster_id:
        return None
    _record_metric("failure_total")
    previous = read_snapshot(cluster_id, max_stale_seconds=DEFAULT_MAX_STALE_SECONDS)
    if previous is None:
        return None
    message = _error_text(error)
    partial_errors = previous.get("partial_errors")
    if not isinstance(partial_errors, Mapping):
        partial_errors = {}
    partial_errors = dict(partial_errors)
    partial_errors["health"] = message
    return ceph_query_cache.update_value(
        SNAPSHOT_NAMESPACE,
        cluster_id,
        {
            "last_attempted_at": _utc_now(),
            "last_error": message,
            "partial_errors": partial_errors,
        },
    )
