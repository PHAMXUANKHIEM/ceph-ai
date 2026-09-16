"""Warm Dashboard cluster snapshots after startup.

The shared cluster snapshot is the source of truth for the realtime path. This
worker hydrates it from disk without issuing a Ceph command, so a Dashboard
restart does not turn into another collector or SSH round trip. Page routes
remain responsible for their own compatibility fallback until RT-05 moves
them to snapshot read models. The worker is deliberately best-effort and
daemonized: the web process becomes ready immediately.
"""

from __future__ import annotations

import logging
import os
from threading import Lock, Thread

from shared import db
from shared.clusters import list_active_clusters
from shared.cluster_snapshot import read_snapshot
from shared.object_storage_cache import get_or_load

logger = logging.getLogger(__name__)
_started = False
_started_lock = Lock()


def start() -> None:
    # Unit tests provide mocked Ceph clients; starting a real SSH warmup from
    # the shared FastAPI lifespan would make every test suite attempt network
    # calls and hide the behavior under test.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    global _started
    with _started_lock:
        if _started:
            return
        _started = True
    Thread(target=_warm, name="dashboard-cache-warmup", daemon=True).start()


def _warm_cluster_snapshots(clusters) -> int:
    """Hydrate the process-local snapshot cache from persistent state.

    The Watcher owns collection. Dashboard startup must only read an existing
    snapshot; otherwise every web restart could create a second Ceph poller.
    """
    warmed = 0
    for cluster in clusters:
        try:
            snapshot = read_snapshot(cluster.id)
        except Exception:
            # A corrupt/unreadable cache for one cluster must not prevent
            # other clusters from being hydrated during the same startup.
            logger.exception(
                "Dashboard snapshot warmup failed for cluster %s",
                cluster.id,
            )
            continue
        if snapshot is None:
            logger.info(
                "Dashboard snapshot warmup: no usable snapshot for cluster %s; waiting for Watcher",
                cluster.id,
            )
            continue
        warmed += 1
        logger.info(
            "Dashboard snapshot warmup: cluster %s generation=%s age_seconds=%s stale=%s",
            cluster.id,
            snapshot.get("generation", "-"),
            snapshot.get("age_seconds", "-"),
            snapshot.get("stale", "-"),
        )
    return warmed


def _warm_block_storage(clusters) -> int:
    """Start inventory refreshes before an operator opens Block Storage.

    The inventory is intentionally loaded in the cache's background executor;
    Dashboard startup and the first browser request stay non-blocking. The
    existing route uses the same cache key and loader, so it immediately sees
    the warmed result when the Ceph query finishes.
    """
    from dashboard.routes.block_storage import _query_block_storage

    scheduled = 0
    for cluster in clusters:
        cache_key = f"{cluster.id}:inventory"
        get_or_load(
            "block-storage",
            cache_key,
            lambda cluster=cluster: _query_block_storage(cluster),
            stale_ttl_seconds=1800,
            background_on_miss=True,
            fallback=[],
        )
        scheduled += 1
    return scheduled


def _warm() -> None:
    try:
        with db.SessionLocal() as session:
            clusters = list_active_clusters(session)
            session.expunge_all()

        _warm_cluster_snapshots(clusters)
        _warm_block_storage(clusters)
    except Exception:
        logger.exception("Dashboard cache warmup could not start")
