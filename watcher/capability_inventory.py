"""AI roadmap Pha 0.1 (Plan/ai-missing-features-roadmap.md) -- Cluster
capability inventory: periodically snapshots per-daemon Ceph version(s),
detects mixed-version clusters, records the cluster's deployment mode, and
normalizes all of that into a `CapabilityStatus` any later version-aware AI
feature (Pha 0.2+) can gate on before ever proposing an action.

Pure collector -- same role as `watcher/crush_structure_monitor.py`: never
creates an Incident, never sends Telegram, never imports `shared.audit`.
It only ever touches `ClusterCapabilityInventory`.
"""

from __future__ import annotations

import json

from shared import ceph_releases, db
from shared.clusters import ensure_default_cluster
from shared.models import CapabilityStatus, Cluster, ClusterCapabilityInventory
from watcher import ceph_client
from watcher.ceph_client import CephQueryError


def collect_capability_snapshot(cluster: Cluster | None = None) -> dict:
    """Runs `ceph versions` and builds the row-shaped dict `scan_and_store`
    persists. Never raises -- a failed query becomes an UNAVAILABLE result
    with `error_message` set, same best-effort posture as every other
    Watcher scan in this codebase (a bad cluster must not kill the poll
    loop).
    """
    try:
        if cluster is None:
            versions = ceph_client.summarize_cluster_versions()
            deployment_mode = ceph_client.settings.ceph_exec_mode
        else:
            mon_nodes = [h.strip() for h in cluster.ceph_mon_nodes.split(",") if h.strip()]
            _, payload = ceph_client.run_ceph_json_command_with(
                mon_nodes, cluster.ceph_container_name, cluster.ssh_user,
                cluster.ssh_key_path, cluster.ceph_exec_mode, "ceph versions",
            )
            versions = ceph_client.summarize_versions_payload(payload)
            deployment_mode = cluster.ceph_exec_mode
    except CephQueryError as exc:
        return {
            "status": CapabilityStatus.UNAVAILABLE.value,
            "deployment_mode": None,
            "per_type_versions": None,
            "distinct_versions": None,
            "is_mixed_version": False,
            "current_version": None,
            "current_major": None,
            "error_message": str(exc),
        }

    current_version = versions.get("current_version")
    current_major = ceph_releases.major_version(current_version) if current_version else None
    if current_version is None:
        # Mixed-version cluster: no single version to check against
        # shared/ceph_releases.py, so this scan can only report the mix
        # itself, not a SUPPORTED/UNSUPPORTED_VERSION verdict.
        status = CapabilityStatus.UNSUPPORTED_VERSION
    elif current_major is not None and current_major in ceph_releases.RELEASES:
        status = CapabilityStatus.SUPPORTED
    else:
        status = CapabilityStatus.UNSUPPORTED_VERSION

    return {
        "status": status.value,
        "deployment_mode": deployment_mode,
        "per_type_versions": versions.get("per_type"),
        "distinct_versions": versions.get("distinct_versions"),
        "is_mixed_version": bool(versions.get("is_mixed")),
        "current_version": current_version,
        "current_major": current_major,
        "error_message": None,
    }


def scan_and_store(cluster_id: str | None = None, cluster: Cluster | None = None) -> None:
    """One scan cycle -- called from `watcher/main.py`'s poll loop(s) on
    their own `capability_inventory_scan_interval_seconds` cadence. Always
    writes a new row (unlike `crush_structure_monitor.scan_and_store`,
    which dedupes against the latest row) -- this table is intentionally a
    time series (see `ClusterCapabilityInventory`'s own docstring for why a
    mixed-version window must not be overwritten by whatever scan comes
    after the upgrade finishes).
    """
    snapshot = collect_capability_snapshot(cluster)

    with db.SessionLocal() as session:
        if cluster_id is None:
            cluster_id = ensure_default_cluster(session).id
        session.add(
            ClusterCapabilityInventory(
                cluster_id=cluster_id,
                status=snapshot["status"],
                deployment_mode=snapshot["deployment_mode"],
                per_type_versions_json=(
                    json.dumps(snapshot["per_type_versions"])
                    if snapshot["per_type_versions"] is not None
                    else None
                ),
                distinct_versions_json=(
                    json.dumps(snapshot["distinct_versions"])
                    if snapshot["distinct_versions"] is not None
                    else None
                ),
                is_mixed_version=snapshot["is_mixed_version"],
                current_version=snapshot["current_version"],
                current_major=snapshot["current_major"],
                error_message=snapshot["error_message"],
            )
        )
        session.commit()


def latest_snapshot(cluster_id: str, session=None) -> ClusterCapabilityInventory | None:
    """Most recent row for `cluster_id`, or `None` if `scan_and_store` has
    never run for it yet (the UNKNOWN case -- deliberately represented by
    "no row" rather than a synthetic UNKNOWN row, so callers can tell
    "never scanned" apart from "scanned and it was UNKNOWN" without a
    schema change if that distinction is ever needed later).
    """
    if session is not None:
        return (
            session.query(ClusterCapabilityInventory)
            .filter(ClusterCapabilityInventory.cluster_id == cluster_id)
            .order_by(ClusterCapabilityInventory.collected_at.desc())
            .first()
        )
    with db.SessionLocal() as owned_session:
        return (
            owned_session.query(ClusterCapabilityInventory)
            .filter(ClusterCapabilityInventory.cluster_id == cluster_id)
            .order_by(ClusterCapabilityInventory.collected_at.desc())
            .first()
        )
