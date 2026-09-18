"""Persistent, cluster-scoped invalidation events.

Events are metadata only. Consumers fetch the authoritative snapshot over the
existing HTTP API after receiving an event, so a missed event is harmless and
large cluster payloads never travel through the WebSocket.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from shared import ceph_query_cache


EVENT_NAMESPACE = "cluster-state-events"
EVENT_MAX_AGE_SECONDS = 900
ALLOWED_EVENTS = {"snapshot_changed", "action_state_changed", "snapshot_refresh_failed"}
ALLOWED_SECTIONS = {"health", "status", "pools", "pgs", "crush", "nodes"}


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


def publish_event(
    cluster_id: str,
    event: str,
    *,
    sections: Iterable[str] | None = None,
    action_id: str | None = None,
    action_status: str | None = None,
    generation: int | None = None,
    collected_at: str | None = None,
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
    if generation is not None:
        payload["generation"] = int(generation)
    if collected_at:
        payload["collected_at"] = str(collected_at)
    return ceph_query_cache.store_versioned(EVENT_NAMESPACE, normalized_cluster, payload)


def publish_action_state_event(
    cluster_id: str,
    action_id: str,
    status: str,
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
