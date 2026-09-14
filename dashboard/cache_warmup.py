"""Warm the expensive dashboard read snapshots after startup.

Cephadm starts a transient shell container for each CLI invocation.  A
dashboard restart must therefore not turn the operator's first click into a
multi-second SSH wait.  This worker is deliberately best-effort and daemonized:
the web process becomes ready immediately, while the common read-only pages
fill their caches in the background.
"""

from __future__ import annotations

import logging
import os
from threading import Lock, Thread

from shared import db
from shared.clusters import list_active_clusters

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


def _warm() -> None:
    try:
        from dashboard.routes import block_storage, pgs, volumes
        from shared.ceph_query_cache import get_or_load as get_ceph_query
        from shared.object_storage_cache import get_or_load

        with db.SessionLocal() as session:
            clusters = list_active_clusters(session)
            session.expunge_all()

        for cluster in clusters:
            try:
                get_or_load(
                    "pools",
                    f"{cluster.id}:inventory",
                    lambda selected=cluster: pgs._query_pool_rows(selected),
                    stale_ttl_seconds=900,
                )
                get_or_load(
                    "pgs",
                    f"{cluster.id}:inventory",
                    lambda selected=cluster: pgs._query_pg_rows(selected),
                    stale_ttl_seconds=900,
                )
                get_or_load(
                    "block-storage",
                    f"{cluster.id}:inventory",
                    lambda selected=cluster: block_storage._query_block_storage(selected),
                    stale_ttl_seconds=1800,
                )

                connection = volumes.cluster_connection(cluster)

                def load_pools(selected=cluster, args=connection):
                    _host, payload = volumes.run_ceph_json_command_with(
                        *args, "ceph osd pool ls detail"
                    )
                    return volumes._pool_names_from_detail(payload)

                get_ceph_query(
                    "rbd-pools",
                    str(cluster.id),
                    load_pools,
                    ttl_seconds=45,
                    stale_ttl_seconds=900,
                )
            except Exception:
                logger.exception("Dashboard cache warmup failed for cluster %s", cluster.id)
    except Exception:
        logger.exception("Dashboard cache warmup could not start")
