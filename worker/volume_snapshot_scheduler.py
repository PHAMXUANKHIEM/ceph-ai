"""Execution boundary for per-volume scheduled Cinder snapshots.

The scheduler only creates an audited Action after validating live Cinder
ownership and the Ceph pool capacity guard. Retention deletes are proposed as
DESTRUCTIVE approval-gated Actions; they are never auto-executed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from dashboard.cinder_discovery import discover_cinder_snapshots, discover_cinder_volume
from shared import audit, db
from shared.models import Action, ActionStatus, Incident, IncidentStatus, VolumeSnapshotPolicy
from shared.volume_snapshot_policy import snapshot_name
from worker.backup.cluster_scope import get_cluster
from worker.executor import commands
from worker.policy import gate
from watcher import ceph_client
from watcher.ceph_client import CephQueryError

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEDULED_CEPH_CODE = "CINDER_SNAPSHOT_SCHEDULED"
_ACTIVE = (
    ActionStatus.PENDING_APPROVAL.value,
    ActionStatus.APPROVED.value,
    ActionStatus.EXECUTING.value,
)


def _set_policy_status(policy_id: str, status: str, error: str | None = None, *, ran_at: bool = False) -> None:
    with db.SessionLocal() as session:
        policy = session.get(VolumeSnapshotPolicy, policy_id)
        if policy is None:
            return
        policy.last_status = status
        policy.last_error = error[:500] if error else None
        if ran_at:
            policy.last_run_at = datetime.utcnow()
        session.commit()


def _cluster_for_policy(policy: VolumeSnapshotPolicy):
    return get_cluster(policy.cluster_id) if policy.cluster_id else None


def _pool_percent_used(cluster, pool: str) -> float | None:
    if cluster is None or cluster.is_default:
        overview = ceph_client.query_rbd_pool_overview(pool)
    else:
        nodes = [item.strip() for item in (cluster.ceph_mon_nodes or "").split(",") if item.strip()]
        overview = ceph_client.query_rbd_pool_overview_with(
            pool, nodes, cluster.ceph_container_name, cluster.ssh_user,
            cluster.ssh_key_path, cluster.ceph_exec_mode,
        )
    try:
        return float(overview.get("percent_used"))
    except (TypeError, ValueError):
        return None


def _has_active_action(session, action_id: str, policy_id: str, snapshot_id: str | None = None) -> bool:
    rows = session.query(Action).filter(
        Action.action_id == action_id, Action.status.in_(_ACTIVE),
    ).limit(50).all()
    for row in rows:
        try:
            params = json.loads(row.action_params or "{}")
        except (TypeError, ValueError):
            continue
        if params.get("policy_id") != policy_id:
            continue
        if snapshot_id is not None and params.get("snapshot_id") != snapshot_id:
            continue
        return True
    return False


def _create_policy_action(policy: VolumeSnapshotPolicy, cluster, *, action_id: str, params: dict, approved: bool, rationale: str) -> str:
    controllers = [item.strip() for item in (cluster.openstack_controller_nodes if cluster else "").split(",") if item.strip()]
    if not controllers or not cluster or not cluster.openstack_openrc_path:
        raise RuntimeError("Cluster chưa cấu hình OpenStack Controller/openrc")
    preview = commands.get_command(action_id, controllers[0], params)
    classification = gate.classify_action(action_id).value
    status = ActionStatus.APPROVED.value if approved else ActionStatus.PENDING_APPROVAL.value
    incident_status = IncidentStatus.EXECUTING.value if approved else IncidentStatus.PENDING_APPROVAL.value
    with db.SessionLocal() as session:
        if _has_active_action(session, action_id, policy.id, params.get("snapshot_id")):
            return ""
        incident = Incident(
            cluster_id=policy.cluster_id, ceph_code=SNAPSHOT_SCHEDULED_CEPH_CODE,
            dedupe_key=f"snapshot-policy:{policy.id}:{action_id}:{params.get('snapshot_id') or params.get('snapshot_name')}",
            status=incident_status, log_excerpt=rationale, detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id, action_id=action_id, classification=classification,
            status=status, rationale=rationale, target_nodes=json.dumps([controllers[0]]),
            action_params=json.dumps(params), proposed_command=preview,
        )
        session.add(action)
        session.flush()
        audit.record(
            session, incident_id=incident.id, action_id=action.id,
            event_type=(audit.EVENT_RISKY_ACTION_APPROVED if approved else audit.EVENT_RISKY_ACTION_PENDING_APPROVAL),
            actor="snapshot_scheduler",
        )
        session.commit()
        return action.id


def run_policy(policy_id: str) -> None:
    with db.SessionLocal() as session:
        policy = session.get(VolumeSnapshotPolicy, policy_id)
        if policy is None or not policy.enabled:
            return
        session.expunge(policy)
    cluster = _cluster_for_policy(policy)
    try:
        percent_used = _pool_percent_used(cluster, policy.pool)
        if percent_used is None:
            raise RuntimeError("Không xác định được % sử dụng pool")
        if percent_used >= policy.capacity_guard_percent:
            _set_policy_status(policy.id, "capacity_guard", f"Pool {percent_used:.2f}% >= guard {policy.capacity_guard_percent:.2f}%", ran_at=True)
            return
        cinder = discover_cinder_volume(cluster, policy.image)
        if cinder.get("status") != "managed" or not cinder.get("verified"):
            raise RuntimeError("Volume không còn được xác minh bởi Cinder")
        force = str(cinder.get("volume_status") or "").lower() == "in-use"
        now = datetime.utcnow()
        params = {
            "pool_name": policy.pool, "image": policy.image, "volume_id": policy.volume_id,
            "snapshot_name": snapshot_name(policy.snapshot_prefix, now),
            "force": force, "openrc_path": cluster.openstack_openrc_path if cluster else "",
            "requested_by": "snapshot_scheduler", "policy_id": policy.id,
        }
        action_id = _create_policy_action(
            policy, cluster, action_id="cinder_create_snapshot", params=params,
            approved=True, rationale=f"Snapshot theo policy cho {policy.pool}/{policy.image}",
        )
        snapshots = discover_cinder_snapshots(cluster, policy.volume_id)
        if snapshots.get("status") == "ok":
            items = [item for item in snapshots.get("items") or [] if str(item.get("status") or "").lower() == "available"]
            items.sort(key=lambda item: str(item.get("created_at") or item.get("created") or ""))
            for item in items[:-policy.retention_count]:
                snapshot_id = str(item.get("snapshot_id") or item.get("id") or "")
                if not snapshot_id:
                    continue
                delete_params = {
                    "pool_name": policy.pool, "image": policy.image, "volume_id": policy.volume_id,
                    "snapshot_id": snapshot_id, "openrc_path": cluster.openstack_openrc_path if cluster else "",
                    "requested_by": "snapshot_scheduler", "policy_id": policy.id,
                }
                _create_policy_action(
                    policy, cluster, action_id="cinder_delete_snapshot", params=delete_params,
                    approved=False, rationale=f"Retention policy yêu cầu xóa snapshot cũ {snapshot_id}; chờ approval",
                )
                break
        _set_policy_status(policy.id, "scheduled" if action_id else "deduplicated", ran_at=True)
    except (CephQueryError, RuntimeError, ValueError) as exc:
        logger.warning("snapshot policy %s blocked: %s", policy_id, exc)
        _set_policy_status(policy.id, "blocked", str(exc), ran_at=True)
