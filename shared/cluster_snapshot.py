"""Shared contract and persistent store for the latest cluster snapshot.

The collector will publish snapshots here; dashboard routes only need to read
them.  Keeping the envelope in one module prevents each page from inventing a
different freshness/timestamp shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from shared import ceph_query_cache

SNAPSHOT_NAMESPACE = "cluster-snapshot"
DEFAULT_STALE_AFTER_SECONDS = 30
DEFAULT_MAX_STALE_SECONDS = 900


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
    return ceph_query_cache.store_versioned(SNAPSHOT_NAMESPACE, _key(cluster_id), snapshot)


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
    cached = ceph_query_cache.get_cached(
        SNAPSHOT_NAMESPACE,
        _key(cluster_id),
        prefer_disk=True,
    )
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
    snapshot.setdefault("refreshing", False)
    snapshot.setdefault("partial_errors", {})
    snapshot.setdefault("last_attempted_at", snapshot.get("collected_at"))
    return snapshot
