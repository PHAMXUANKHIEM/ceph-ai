"""Shared contract and persistent store for the latest cluster snapshot.

The collector will publish snapshots here; dashboard routes only need to read
them.  Keeping the envelope in one module prevents each page from inventing a
different freshness/timestamp shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from shared import ceph_query_cache
from shared.cluster_events import publish_event

SNAPSHOT_NAMESPACE = "cluster-snapshot"
SECTION_SNAPSHOT_NAMESPACE = "cluster-section-snapshot"
REFRESH_STATE_NAMESPACE = "cluster-snapshot-refresh-state"
DEFAULT_STALE_AFTER_SECONDS = 30
DEFAULT_MAX_STALE_SECONDS = 900
REFRESH_STATE_MAX_AGE_SECONDS = DEFAULT_MAX_STALE_SECONDS


def _key(cluster_id: str) -> str:
    value = str(cluster_id or "").strip()
    if not value:
        raise ValueError("cluster_id is required")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_timestamp(value: str | None) -> str:
    if value is None:
        return _utc_now()
    if not isinstance(value, str) or not value.strip():
        raise ValueError("collected_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("collected_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("collected_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _age_from_timestamp(value: object, fallback: float) -> float:
    if not isinstance(value, str):
        return max(0.0, fallback)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return max(0.0, fallback)
    except ValueError:
        return max(0.0, fallback)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def make_snapshot(
    cluster_id: str,
    sections: Mapping[str, object],
    *,
    collected_at: str | None = None,
    source: str = "watcher",
    partial_errors: Mapping[str, str] | None = None,
    last_error: str | None = None,
) -> dict:
    """Build a validated snapshot envelope before publishing it."""
    normalized_id = _key(cluster_id)
    if not isinstance(sections, Mapping):
        raise TypeError("sections must be a mapping")
    if not str(source or "").strip():
        raise ValueError("source is required")
    if partial_errors is not None and not isinstance(partial_errors, Mapping):
        raise TypeError("partial_errors must be a mapping")
    normalized_collected_at = _normalize_timestamp(collected_at)
    snapshot = dict(sections)
    snapshot.update(
        {
            "cluster_id": normalized_id,
            "collected_at": normalized_collected_at,
            "last_attempted_at": normalized_collected_at,
            "published_at": _utc_now(),
            "collector": str(source).strip(),
            "stale": False,
            "refreshing": False,
            "last_error": last_error,
            "partial_errors": dict(partial_errors or {}),
        }
    )
    return snapshot


def publish_snapshot(
    cluster_id: str,
    sections: Mapping[str, object],
    *,
    collected_at: str | None = None,
    source: str = "watcher",
    partial_errors: Mapping[str, str] | None = None,
    last_error: str | None = None,
) -> dict:
    """Atomically publish a snapshot and assign its next generation."""
    snapshot = make_snapshot(
        cluster_id,
        sections,
        collected_at=collected_at,
        source=source,
        partial_errors=partial_errors,
        last_error=last_error,
    )
    stored = ceph_query_cache.store_versioned(SNAPSHOT_NAMESPACE, _key(cluster_id), snapshot)
    changed_sections = [
        name for name in ("health", "status", "pools", "pgs", "crush", "nodes")
        if name in sections
    ]
    publish_event(
        cluster_id,
        "snapshot_changed",
        sections=changed_sections,
        generation=stored.get("generation"),
        collected_at=stored.get("collected_at"),
    )
    # A successful publish closes the lifecycle even when the caller did not
    # explicitly clear the marker in its finally block.
    mark_refreshing(cluster_id, False)
    return stored


def mark_refreshing(cluster_id: str, refreshing: bool = True) -> bool:
    """Record whether a cluster snapshot refresh is currently in flight.

    The marker is persisted separately from the snapshot payload so a web
    process can observe a refresh started by Watcher. It expires naturally
    after ``REFRESH_STATE_MAX_AGE_SECONDS`` if a process dies before clearing
    it, preventing a stale flag from surviving indefinitely.
    """
    normalized_id = _key(cluster_id)
    if refreshing:
        ceph_query_cache.store(
            REFRESH_STATE_NAMESPACE,
            normalized_id,
            {"refreshing": True, "marked_at": _utc_now()},
        )
        return True
    ceph_query_cache.invalidate(REFRESH_STATE_NAMESPACE, normalized_id)
    return False


def is_refreshing(cluster_id: str) -> bool:
    """Return the cross-process refresh state for one cluster."""
    normalized_id = _key(cluster_id)
    cached = ceph_query_cache.get_cached(
        REFRESH_STATE_NAMESPACE,
        normalized_id,
        max_age_seconds=REFRESH_STATE_MAX_AGE_SECONDS,
        prefer_disk=True,
    )
    if cached is None:
        return False
    value, _age_seconds = cached
    return isinstance(value, Mapping) and bool(value.get("refreshing"))


def invalidate_snapshot(cluster_id: str) -> None:
    """Remove one cluster's published health snapshot after a confirmed mutation."""
    normalized_id = _key(cluster_id)
    mark_refreshing(normalized_id, False)
    ceph_query_cache.invalidate(SNAPSHOT_NAMESPACE, normalized_id)


def publish_section_snapshot(
    cluster_id: str,
    section: str,
    data: object,
    *,
    collected_at: str | None = None,
    source: str = "watcher",
    partial_errors: Mapping[str, str] | None = None,
    last_error: str | None = None,
    section_available: bool = True,
) -> dict:
    """Persist one independently refreshed cluster section.

    Inventory sections have different collection cadences and payload sizes,
    so they use separate versioned records while keeping the same envelope
    contract as the health snapshot.
    """
    normalized_id = _key(cluster_id)
    normalized_section = str(section or "").strip()
    if not normalized_section:
        raise ValueError("section is required")
    snapshot = make_snapshot(
        normalized_id,
        {
            normalized_section: data,
            "section_name": normalized_section,
            "section_available": bool(section_available),
        },
        collected_at=collected_at,
        source=source,
        partial_errors=partial_errors,
        last_error=last_error,
    )
    stored = ceph_query_cache.store_versioned(
        SECTION_SNAPSHOT_NAMESPACE,
        f"{normalized_id}:{normalized_section}",
        snapshot,
    )
    publish_event(
        normalized_id,
        "snapshot_changed",
        sections=[normalized_section],
        generation=stored.get("generation"),
        collected_at=stored.get("collected_at"),
    )
    return stored


def record_section_error(
    cluster_id: str,
    section: str,
    error: object,
    *,
    empty_data: object,
    source: str = "watcher",
) -> dict | None:
    """Keep the last good section and expose the latest failed attempt."""
    normalized_id = _key(cluster_id)
    normalized_section = str(section or "").strip()
    message = (str(error) or type(error).__name__)[:1000]
    previous = read_section_snapshot(normalized_id, normalized_section, max_stale_seconds=DEFAULT_MAX_STALE_SECONDS)
    if previous is None:
        return publish_section_snapshot(
            normalized_id,
            normalized_section,
            empty_data,
            source=source,
            partial_errors={normalized_section: message},
            last_error=message,
            section_available=False,
        )
    partial_errors = previous.get("partial_errors")
    if not isinstance(partial_errors, Mapping):
        partial_errors = {}
    partial_errors = dict(partial_errors)
    partial_errors[normalized_section] = message
    return ceph_query_cache.update_value(
        SECTION_SNAPSHOT_NAMESPACE,
        f"{normalized_id}:{normalized_section}",
        {
            "last_attempted_at": _utc_now(),
            "last_error": message,
            "partial_errors": partial_errors,
            "section_available": True,
        },
    )


def read_snapshot(
    cluster_id: str,
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    max_stale_seconds: int = DEFAULT_MAX_STALE_SECONDS,
) -> dict | None:
    """Read the latest snapshot and derive age/stale metadata.

    ``max_stale_seconds`` controls when a snapshot becomes unusable.  A stale
    but usable snapshot is still returned so the UI can keep its last known
    values while showing a warning.
    """
    if stale_after_seconds < 0 or max_stale_seconds < stale_after_seconds:
        raise ValueError("stale thresholds are invalid")
    return _read_snapshot_by_key(
        SNAPSHOT_NAMESPACE,
        _key(cluster_id),
        stale_after_seconds=stale_after_seconds,
        max_stale_seconds=max_stale_seconds,
    )


def read_section_snapshot(
    cluster_id: str,
    section: str,
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    max_stale_seconds: int = DEFAULT_MAX_STALE_SECONDS,
) -> dict | None:
    """Read one independently refreshed inventory section."""
    normalized_id = _key(cluster_id)
    normalized_section = str(section or "").strip()
    if not normalized_section:
        raise ValueError("section is required")
    return _read_snapshot_by_key(
        SECTION_SNAPSHOT_NAMESPACE,
        f"{normalized_id}:{normalized_section}",
        stale_after_seconds=stale_after_seconds,
        max_stale_seconds=max_stale_seconds,
    )


def _read_snapshot_by_key(
    namespace: str,
    storage_key: str,
    *,
    stale_after_seconds: int,
    max_stale_seconds: int,
) -> dict | None:
    if stale_after_seconds < 0 or max_stale_seconds < stale_after_seconds:
        raise ValueError("stale thresholds are invalid")
    cached = ceph_query_cache.get_cached(namespace, storage_key, prefer_disk=True)
    if cached is None:
        return None
    value, age_seconds = cached
    if not isinstance(value, dict):
        return None
    snapshot = dict(value)
    age_seconds = _age_from_timestamp(snapshot.get("collected_at"), age_seconds)
    if age_seconds > max_stale_seconds:
        return None
    snapshot["age_seconds"] = round(age_seconds, 1)
    snapshot["stale"] = age_seconds > stale_after_seconds
    snapshot["refreshing"] = is_refreshing(storage_key)
    snapshot.setdefault("partial_errors", {})
    snapshot.setdefault("last_attempted_at", snapshot.get("collected_at"))
    return snapshot
