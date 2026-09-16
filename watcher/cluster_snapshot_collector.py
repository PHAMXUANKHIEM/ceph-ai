"""Publish the Watcher's critical health result to the shared snapshot store.

The Watcher already owns one polling loop per active cluster.  This module
keeps snapshot shaping separate from incident logic and deliberately reuses
the health payload already fetched by that loop, so publishing a snapshot
does not add another SSH/Ceph round trip.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import copy_context
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from time import monotonic
from threading import Lock
from typing import Mapping

from shared import ceph_query_cache
from config.settings import settings
from shared.cluster_snapshot import (
    DEFAULT_MAX_STALE_SECONDS,
    SNAPSHOT_NAMESPACE,
    publish_section_snapshot,
    publish_snapshot,
    record_section_error,
    read_snapshot,
)
from shared.cluster_nodes import configured_nodes
from watcher.ceph_client import query_cluster_health_with, query_cluster_status_with
from watcher.inventory_queries import build_crush_tree_response, collect_pg_rows, collect_pool_rows

SNAPSHOT_SOURCE = "watcher-critical-health"
STATUS_SNAPSHOT_SOURCE = "watcher-critical-status"
COLLECTION_LOCK_NAMESPACE = "cluster-health-collection"
STATUS_LOCK_NAMESPACE = "cluster-status-collection"
INVENTORY_LOCK_NAMESPACE = "cluster-inventory-collection"
INVENTORY_SECTIONS = ("pools", "pgs", "crush", "nodes")
_METRICS_LOCK = Lock()
_METRICS = {
    "success_total": 0,
    "failure_total": 0,
    "collector_success_total": 0,
    "collector_failure_total": 0,
    "commands": {},
    "last_duration_ms": None,
    "last_cluster_id": None,
    "last_completed_at": None,
}
logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def collection_timestamp() -> str:
    """Return the wall-clock timestamp captured at collection start."""
    return _utc_now()


@contextmanager
def health_collection_lock(cluster_id: str | None):
    """Serialize health queries for one cluster across all app processes.

    Lock acquisition is bounded by the health poll budget. A stuck peer must
    not turn a dashboard refresh or watcher loop into an unbounded wait.
    """
    if not cluster_id:
        yield
        return
    lock_timeout = min(max(float(settings.ceph_health_timeout), 1.0), 10.0)
    try:
        with ceph_query_cache.key_lock(
            COLLECTION_LOCK_NAMESPACE,
            cluster_id,
            timeout_seconds=lock_timeout,
        ):
            yield
    except ceph_query_cache.CacheLockError as exc:
        raise TimeoutError(
            f"health collection lock acquisition exceeded {lock_timeout:g}s"
        ) from exc


@contextmanager
def inventory_collection_lock(cluster_id: str | None):
    """Serialize the inventory tier for one cluster across processes."""
    if not cluster_id:
        yield
        return
    with ceph_query_cache.key_lock(INVENTORY_LOCK_NAMESPACE, cluster_id):
        yield


@contextmanager
def status_collection_lock(cluster_id: str | None):
    """Serialize full ``ceph -s`` queries for one cluster."""
    if not cluster_id:
        yield
        return
    with ceph_query_cache.key_lock(STATUS_LOCK_NAMESPACE, cluster_id):
        yield


def _error_text(error: object) -> str:
    return (str(error) or type(error).__name__)[:1000]


def get_metrics() -> dict:
    """Return collector counters, including per-tier command outcomes."""
    with _METRICS_LOCK:
        return deepcopy(_METRICS)


def _record_metric(name: str, command: str | None = None) -> None:
    with _METRICS_LOCK:
        _METRICS[name] += 1
        if name == "success_total":
            _METRICS["collector_success_total"] += 1
        elif name == "failure_total":
            _METRICS["collector_failure_total"] += 1
        if command:
            command_metrics = _METRICS["commands"].setdefault(
                command, {"success_total": 0, "failure_total": 0}
            )
            command_metrics[name] += 1


def _record_collection_duration(cluster_id: str | None, started: float) -> None:
    with _METRICS_LOCK:
        _METRICS["last_duration_ms"] = round(max(0.0, monotonic() - started) * 1000, 2)
        _METRICS["last_cluster_id"] = str(cluster_id) if cluster_id is not None else None
        _METRICS["last_completed_at"] = _utc_now()


def collect_and_publish_health(
    cluster,
    *,
    mon_nodes: list[str] | None = None,
    collection_started_monotonic: float | None = None,
    collection_started_at: str | None = None,
) -> dict:
    """Collect one critical health snapshot for an explicit cluster.

    This is used by the operator's manual refresh endpoint. Normal polling
    remains owned by Watcher; the endpoint only schedules this function and
    never runs it inside the HTTP request.
    """
    nodes = mon_nodes
    if nodes is None:
        nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    with health_collection_lock(cluster.id):
        try:
            health = query_cluster_health_with(
                nodes,
                cluster.ceph_container_name,
                cluster.ssh_user,
                cluster.ssh_key_path,
                cluster.ceph_exec_mode,
                update_sticky_fallback=False,
            )
            publish_health_snapshot(
                cluster.id,
                health,
                collection_started_monotonic=collection_started_monotonic,
                collection_started_at=collection_started_at,
            )
        except Exception as exc:
            try:
                publish_health_error(cluster.id, exc)
            except Exception:
                logger.exception("collect_and_publish_health: failed to record refresh error")
            raise
    return health


def collect_and_publish_status(
    cluster,
    *,
    mon_nodes: list[str] | None = None,
    collection_started_at: str | None = None,
) -> dict:
    """Collect and publish the full read-only ``ceph -s`` status payload.

    The health snapshot is intentionally small and cheap. Dashboard cards
    also need the OSD, MON, pool, PG, capacity, and throughput maps that are
    only present in ``ceph -s``; keep that slower query in its own section so
    a failed status query cannot erase the last good health snapshot.
    """
    nodes = mon_nodes
    if nodes is None:
        nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    with status_collection_lock(cluster.id):
        try:
            status = query_cluster_status_with(
                nodes,
                cluster.ceph_container_name,
                cluster.ssh_user,
                cluster.ssh_key_path,
                cluster.ceph_exec_mode,
                update_sticky_fallback=False,
            )
            publish_section_snapshot(
                cluster.id,
                "status",
                status,
                collected_at=collection_started_at or collection_timestamp(),
                source=STATUS_SNAPSHOT_SOURCE,
            )
            _record_metric("success_total", "status")
        except Exception as exc:
            _record_metric("failure_total", "status")
            record_section_error(
                cluster.id,
                "status",
                exc,
                empty_data={},
                source=STATUS_SNAPSHOT_SOURCE,
            )
            raise
    return status


def _collect_pool_rows(cluster) -> list[dict]:
    return collect_pool_rows(cluster)


def _collect_pg_rows(cluster) -> list[dict]:
    return collect_pg_rows(cluster)


def _collect_crush_tree(cluster) -> dict:
    from shared import db
    from shared.models import CrushStructureSnapshot
    from sqlalchemy import and_, or_

    with db.SessionLocal() as session:
        latest = (
            session.query(CrushStructureSnapshot)
            .filter(or_(
                CrushStructureSnapshot.cluster_id == cluster.id,
                and_(cluster.is_default, CrushStructureSnapshot.cluster_id.is_(None)),
            ))
            .order_by(CrushStructureSnapshot.created_at.desc())
            .first()
        )
        if latest is None:
            return {"state": "no_snapshot_yet"}
        return build_crush_tree_response(latest, cluster.id, cluster.is_default)


def _collect_node_summary(cluster) -> dict:
    nodes = configured_nodes(cluster)
    return {"nodes": nodes, "total": len(nodes)}


class CephSnapshotCollector:
    """Collect independent inventory sections with bounded parallelism.

    Each loader owns its Ceph batch query and publishes independently. A
    failed pool/PG query therefore cannot discard a successful nodes or CRUSH
    section, and the worker count is capped by the central Ceph concurrency
    setting rather than the number of inventory sections.
    """

    def __init__(self, *, max_workers: int | None = None):
        requested = settings.ceph_max_concurrency if max_workers is None else max_workers
        self.max_workers = max(1, min(int(requested), len(INVENTORY_SECTIONS)))

    def collect_inventory(
        self,
        cluster,
        *,
        collection_started_at: str | None = None,
    ) -> dict[str, object]:
        """Collect the slow inventory tier once and publish each section.

        Pools and PGs are the only Ceph/SSH-backed sections here. CRUSH is read
        from the Watcher's persisted structure snapshot and Nodes are derived
        from the cluster configuration, so browser requests never start a
        remote query. A failure in one section leaves the other sections
        publishable.
        """
        started_at = collection_started_at or collection_timestamp()
        started = monotonic()
        loaders = {
            "pools": (_collect_pool_rows, []),
            "pgs": (_collect_pg_rows, []),
            "crush": (_collect_crush_tree, {"state": "no_snapshot_yet"}),
            "nodes": (_collect_node_summary, {"nodes": [], "total": 0}),
        }
        published: dict[str, object] = {}
        try:
            with inventory_collection_lock(cluster.id):
                with ThreadPoolExecutor(
                    max_workers=self.max_workers,
                    thread_name_prefix="ceph-inventory",
                ) as executor:
                    futures = {
                        executor.submit(copy_context().run, loader, cluster): (section, empty_data)
                        for section, (loader, empty_data) in loaders.items()
                    }
                    for future in as_completed(futures):
                        section, empty_data = futures[future]
                        try:
                            data = future.result()
                            published[section] = publish_section_snapshot(
                                cluster.id,
                                section,
                                data,
                                collected_at=started_at,
                                source="watcher-inventory",
                            )
                            _record_metric("success_total", section)
                        except Exception as exc:
                            _record_metric("failure_total", section)
                            logger.warning(
                                "inventory(%s): %s collection failed: %s",
                                cluster.name,
                                section,
                                exc,
                            )
                            record_section_error(
                                cluster.id,
                                section,
                                exc,
                                empty_data=empty_data,
                                source="watcher-inventory",
                            )
        finally:
            _record_collection_duration(cluster.id, started)
        return published


def collect_and_publish_inventory(
    cluster,
    *,
    collection_started_at: str | None = None,
) -> dict[str, object]:
    """Compatibility wrapper for the existing Watcher and refresh callers."""
    return CephSnapshotCollector().collect_inventory(
        cluster,
        collection_started_at=collection_started_at,
    )


def _sections_from_health(health: Mapping[str, object], duration_ms: float | None) -> dict:
    checks = health.get("checks")
    sections = {
        "health": dict(health),
        "health_status": health.get("status"),
        "health_checks": dict(checks) if isinstance(checks, Mapping) else {},
        "health_available": True,
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
    collection_started_at: str | None = None,
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
            collected_at=collection_started_at or _utc_now(),
            source=SNAPSHOT_SOURCE,
        )
    except Exception:
        _record_metric("failure_total", "health")
        raise
    _record_metric("success_total", "health")
    return snapshot


def publish_health_error(cluster_id: str | None, error: object) -> dict | None:
    """Retain the last good health payload while recording the failed attempt."""
    if not cluster_id:
        return None
    _record_metric("failure_total", "health")
    previous = read_snapshot(cluster_id, max_stale_seconds=DEFAULT_MAX_STALE_SECONDS)
    message = _error_text(error)
    attempted_at = _utc_now()
    if previous is None:
        return publish_snapshot(
            cluster_id,
            {
                "health": {"status": "UNKNOWN", "checks": {}},
                "health_status": "UNKNOWN",
                "health_checks": {},
                "health_available": False,
                "collection": {"tier": "critical", "duration_ms": None},
            },
            collected_at=attempted_at,
            source=SNAPSHOT_SOURCE,
            partial_errors={"health": message},
            last_error=message,
        )
    previous_attempted_at = previous.get("last_attempted_at")
    try:
        previous_dt = datetime.fromisoformat(str(previous_attempted_at).replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(attempted_at.replace("Z", "+00:00"))
        if previous_dt.tzinfo is not None and now_dt <= previous_dt:
            attempted_at = (previous_dt + timedelta(milliseconds=1)).astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
    except (TypeError, ValueError):
        pass
    partial_errors = previous.get("partial_errors")
    if not isinstance(partial_errors, Mapping):
        partial_errors = {}
    partial_errors = dict(partial_errors)
    partial_errors["health"] = message
    return ceph_query_cache.update_value(
        SNAPSHOT_NAMESPACE,
        cluster_id,
        {
            "last_attempted_at": attempted_at,
            "last_error": message,
            "partial_errors": partial_errors,
        },
    )
