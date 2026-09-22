"""Persistent, cluster-scoped invalidation events.

Events are metadata only. Consumers fetch the authoritative snapshot over the
existing HTTP API after receiving an event, so a missed event is harmless and
large cluster payloads never travel through the WebSocket.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from threading import Lock
from time import time

from shared import ceph_query_cache
from shared.request_context import get_request_id


EVENT_NAMESPACE = "cluster-state-events"
EVENT_MAX_AGE_SECONDS = 900
ALLOWED_EVENTS = {"snapshot_changed", "action_state_changed", "snapshot_refresh_failed"}
ALLOWED_SECTIONS = {"health", "status", "pools", "pgs", "crush", "nodes"}
ACTION_STATES = {"queued", "running", "verifying", "succeeded", "failed", "rejected"}
_METRICS_LOCK = Lock()
_METRICS = {
    "publish_success_total": 0,
    "publish_failure_total": 0,
    "publish_failure_window_15m": 0,
    "last_failure_at": None,
    "last_success_at": None,
    "publish_recovery_state": "UNKNOWN",
    "action_state_changed_total": 0,
    "snapshot_changed_total": 0,
    "snapshot_refresh_failed_total": 0,
}
_PUBLISH_FAILURE_TIMESTAMPS: list[float] = []


def _record_metric(name: str) -> None:
    with _METRICS_LOCK:
        now = time()
        cutoff = now - 15 * 60
        _PUBLISH_FAILURE_TIMESTAMPS[:] = [stamp for stamp in _PUBLISH_FAILURE_TIMESTAMPS if stamp >= cutoff]
        _METRICS[name] += 1
        if name == "publish_failure_total":
            _PUBLISH_FAILURE_TIMESTAMPS.append(now)
            _METRICS["last_failure_at"] = now
        elif name == "publish_success_total":
            _METRICS["last_success_at"] = now
        _METRICS["publish_failure_window_15m"] = len(_PUBLISH_FAILURE_TIMESTAMPS)
        _METRICS["publish_recovery_state"] = _recovery_state_locked()


def _recovery_state_locked() -> str:
    """Return ACTIVE/RECOVERED/UNKNOWN; caller holds ``_METRICS_LOCK``."""
    failures = int(_METRICS.get("publish_failure_total", 0) or 0)
    if failures == 0:
        return "UNKNOWN"
    last_failure = float(_METRICS.get("last_failure_at") or 0)
    last_success = float(_METRICS.get("last_success_at") or 0)
    if last_success > last_failure:
        return "RECOVERED"
    if int(_METRICS.get("publish_failure_window_15m", 0) or 0) > 0:
        return "ACTIVE"
    return "UNKNOWN"


def get_metrics() -> dict[str, object]:
    """Return bounded event-publish counters and recovery timestamps."""
    with _METRICS_LOCK:
        cutoff = time() - 15 * 60
        _PUBLISH_FAILURE_TIMESTAMPS[:] = [stamp for stamp in _PUBLISH_FAILURE_TIMESTAMPS if stamp >= cutoff]
        _METRICS["publish_failure_window_15m"] = len(_PUBLISH_FAILURE_TIMESTAMPS)
        _METRICS["publish_recovery_state"] = _recovery_state_locked()
        return deepcopy(_METRICS)

# Keep the persisted Action enum available for compatibility while exposing a
# small UI contract.  EXECUTED/AUTO_EXECUTED mean that the command finished;
# the cluster is only considered succeeded after the separate post-check
# emits the `snapshot_changed` event.
_ACTION_STATE_BY_STATUS = {
    "PENDING": "queued",
    "PENDING_APPROVAL": "queued",
    "APPROVED": "queued",
    "EXECUTING": "running",
    "GRACE_PENDING": "running",
    "INCONCLUSIVE": "verifying",
    "AUTO_EXECUTED": "verifying",
    "EXECUTED": "verifying",
    "FAILED": "failed",
    "REJECTED": "rejected",
}


def _cluster_id(cluster_id: str) -> str:
    value = str(cluster_id or "").strip()
    if not value:
        raise ValueError("cluster_id is required")
    return value


def _sections(sections: Iterable[str] | None) -> list[str]:
    if sections is None:
        return []
    result = []
    for section in sections:
        value = str(section or "").strip()
        if value and value in ALLOWED_SECTIONS and value not in result:
            result.append(value)
    return result


def action_state_for_status(status: str | None) -> str | None:
    """Map the internal Action status to the bounded realtime UI contract."""
    if status is None:
        return None
    normalized = str(status).strip().upper()
    return _ACTION_STATE_BY_STATUS.get(normalized)


def publish_event(
    cluster_id: str,
    event: str,
    *,
    sections: Iterable[str] | None = None,
    action_id: str | None = None,
    action_status: str | None = None,
    action_state: str | None = None,
    generation: int | None = None,
    collected_at: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Publish one bounded event and return its persisted envelope."""
    normalized_cluster = _cluster_id(cluster_id)
    normalized_event = str(event or "").strip()
    if normalized_event not in ALLOWED_EVENTS:
        raise ValueError(f"unsupported cluster event: {normalized_event!r}")
    payload: dict[str, object] = {
        "event": normalized_event,
        "cluster_id": normalized_cluster,
        "sections": _sections(sections),
    }
    if action_id:
        payload["action_id"] = str(action_id)
    if action_status:
        payload["action_status"] = str(action_status)
    normalized_action_state = action_state or action_state_for_status(action_status)
    if normalized_action_state not in ACTION_STATES:
        if normalized_action_state is not None:
            raise ValueError(f"unsupported action state: {normalized_action_state!r}")
    elif normalized_action_state:
        payload["action_state"] = normalized_action_state
    if generation is not None:
        payload["generation"] = int(generation)
    if collected_at:
        payload["collected_at"] = str(collected_at)
    correlation_id = request_id or get_request_id()
    if isinstance(correlation_id, str) and 1 <= len(correlation_id) <= 128:
        payload["request_id"] = correlation_id
    try:
        stored = ceph_query_cache.store_versioned(EVENT_NAMESPACE, normalized_cluster, payload)
    except Exception:
        _record_metric("publish_failure_total")
        raise
    _record_metric("publish_success_total")
    _record_metric(f"{normalized_event}_total")
    return stored


def publish_action_state_event(
    cluster_id: str,
    action_id: str,
    status: str,
    *,
    request_id: str | None = None,
) -> dict:
    """Publish a committed Action lifecycle transition for the UI.

    The event is metadata only; clients still read the authenticated Action
    API. Keeping the status in the envelope lets a reconnecting client avoid
    guessing which transition it missed, while the action primary key keeps
    the event cluster-scoped and idempotently addressable.
    """
    return publish_event(
        cluster_id,
        "action_state_changed",
        action_id=action_id,
        action_status=status,
        action_state=action_state_for_status(status),
        request_id=request_id,
    )


def read_latest_event(cluster_id: str) -> dict | None:
    """Read the latest event for one cluster, preferring shared disk state."""
    value = ceph_query_cache.get_cached(
        EVENT_NAMESPACE,
        _cluster_id(cluster_id),
        max_age_seconds=EVENT_MAX_AGE_SECONDS,
        prefer_disk=True,
    )
    if value is None or not isinstance(value[0], Mapping):
        return None
    return dict(value[0])
