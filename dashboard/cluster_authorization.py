"""Server-side user/cluster capability checks."""

from __future__ import annotations

from fastapi import HTTPException, Request

from dashboard.routes.auth import is_admin_user
from shared import db
from shared.models import Cluster, ClusterCapabilityGrant, User


CAPABILITY_READ = "cluster.read"
CAPABILITY_MANAGE = "cluster.manage"
CAPABILITY_APPROVE = "action.approve"
CAPABILITY_EXECUTE = "action.execute"


def _capability_matches(granted: str, requested: str) -> bool:
    if granted == requested:
        return True
    if requested == CAPABILITY_READ:
        return granted.startswith("cluster.") or granted.startswith("action.")
    if requested in {CAPABILITY_APPROVE, CAPABILITY_EXECUTE}:
        return granted == CAPABILITY_MANAGE
    return False


def has_cluster_capability(username: str, cluster_id: str, capability: str) -> bool:
    if not username or not cluster_id or not capability:
        return False
    if is_admin_user(username):
        return True
    with db.SessionLocal() as session:
        user = session.query(User).filter(
            User.username == username, User.is_active.is_(True)
        ).one_or_none()
        if user is None:
            return False
        # Preserve the existing dashboard contract for active non-admin
        # accounts: they may still read the configured default cluster. Any
        # additional cluster, and every approval/mutation capability, requires
        # an explicit grant below.
        cluster = session.get(Cluster, cluster_id)
        if capability == CAPABILITY_READ and cluster is not None and cluster.is_default:
            return True
        grants = session.query(ClusterCapabilityGrant.capability).filter(
            ClusterCapabilityGrant.user_id == user.id,
            ClusterCapabilityGrant.cluster_id == cluster_id,
            ClusterCapabilityGrant.is_active.is_(True),
        ).all()
        return any(_capability_matches(row[0], capability) for row in grants)


def require_cluster_capability(
    request: Request, cluster_id: str, capability: str, *, username: str | None = None,
) -> str:
    actor = username or str(request.session.get("user") or "")
    if not has_cluster_capability(actor, cluster_id, capability):
        raise HTTPException(
            status_code=403,
            detail=f"Tài khoản không có capability {capability!r} trên cụm đã chọn",
        )
    return actor


def authorized_cluster_ids(username: str) -> set[str] | None:
    """Return allowed cluster IDs; None means global administrator access."""
    if is_admin_user(username):
        return None
    with db.SessionLocal() as session:
        user = session.query(User).filter(
            User.username == username, User.is_active.is_(True)
        ).one_or_none()
        if user is None:
            return set()
        rows = session.query(ClusterCapabilityGrant.cluster_id).filter(
            ClusterCapabilityGrant.user_id == user.id,
            ClusterCapabilityGrant.is_active.is_(True),
        ).distinct().all()
        return {row[0] for row in rows}
