"""Dedicated fast path for AI diagnosis and autonomous Ceph remediation.

This process deliberately shares the existing database and RabbitMQ queue.
It does only fresh ``ceph health detail`` polling, Incident publication and
post-action verification; RBD, capacity, inventory and Log Intelligence stay
in :mod:`watcher.main` and can no longer delay safety-critical detection.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from config.settings import settings
from shared import db
from shared.clusters import get_default_cluster_id
from shared.cluster_nodes import resolve_ssh_creds
from shared.models import Cluster
from watcher import ceph_client, verify
from watcher.ceph_client import CephQueryError
from watcher.main import (
    _reconcile_terminal_actions,
    _resolve_recovered_incidents,
    build_and_publish_incident,
)

logger = logging.getLogger(__name__)


def run(max_iterations: Optional[int] = None) -> None:
    with db.SessionLocal() as session:
        cluster_id = get_default_cluster_id(session)
        cluster = session.get(Cluster, cluster_id)
        if cluster is None:
            raise RuntimeError(f"default cluster {cluster_id!r} not found")
        mon_nodes = [value.strip() for value in cluster.ceph_mon_nodes.split(",") if value.strip()]
        ssh_user, ssh_key, exec_mode, container = resolve_ssh_creds(cluster)

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        started = time.monotonic()
        try:
            health = ceph_client.query_cluster_health_with(
                mon_nodes, container, ssh_user, ssh_key, exec_mode,
                update_sticky_fallback=True,
            )
            current_checks = set((health.get("checks") or {}).keys())
            _resolve_recovered_incidents(current_checks, cluster_id=cluster_id)
            _reconcile_terminal_actions()
            try:
                verify.verify_pending_incidents(
                    current_checks, health=health, cluster_id=cluster_id,
                )
            except Exception:
                logger.exception("AI remediation watcher: post-action verification failed")

            # Run every tick rather than only on a status fingerprint change.
            # DB state may have become terminal between identical Ceph polls,
            # and a recurring OSD failure must then be eligible immediately.
            build_and_publish_incident(None, health, cluster_id=cluster_id)
        except CephQueryError:
            logger.exception("AI remediation watcher: all MON health queries failed")
        except Exception:
            logger.exception("AI remediation watcher: unexpected poll failure")

        iterations += 1
        elapsed = time.monotonic() - started
        time.sleep(max(0, settings.ai_remediation_poll_interval_seconds - elapsed))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    run()
