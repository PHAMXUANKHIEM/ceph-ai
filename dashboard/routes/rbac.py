"""Administrative API for per-user, per-cluster capability grants."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dashboard.routes.auth import is_admin_user, require_login
from shared import db
from shared.models import Cluster, ClusterCapabilityGrant, SingleFullAudit, User
from shared.time import utc_now

router = APIRouter()


class GrantRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    cluster_id: str = Field(min_length=1, max_length=36)
    capability: str = Field(min_length=3, max_length=96, pattern=r"^[a-z][a-z0-9_.:-]{2,95}$")


def _require_admin(user: str) -> None:
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin mới được quản lý capability grant")


@router.get("/api/rbac/grants")
async def list_grants(user: str = Depends(require_login)) -> dict:
    _require_admin(user)
    with db.SessionLocal() as session:
        rows = session.query(ClusterCapabilityGrant, User, Cluster).join(
            User, User.id == ClusterCapabilityGrant.user_id
        ).join(Cluster, Cluster.id == ClusterCapabilityGrant.cluster_id).order_by(
            ClusterCapabilityGrant.created_at.desc()
        ).all()
        return {"grants": [{
            "id": grant.id, "username": account.username,
            "cluster_id": cluster.id, "cluster_name": cluster.name,
            "capability": grant.capability, "is_active": grant.is_active,
            "granted_by": grant.granted_by,
            "created_at": grant.created_at.isoformat(),
        } for grant, account, cluster in rows]}


@router.post("/api/rbac/grants")
async def upsert_grant(payload: GrantRequest, user: str = Depends(require_login)) -> dict:
    _require_admin(user)
    with db.SessionLocal() as session:
        account = session.query(User).filter(User.username == payload.username).one_or_none()
        cluster = session.get(Cluster, payload.cluster_id)
        if account is None or not account.is_active:
            raise HTTPException(status_code=404, detail="Không tìm thấy user đang hoạt động")
        if cluster is None or not cluster.is_active:
            raise HTTPException(status_code=404, detail="Không tìm thấy cluster đang hoạt động")
        grant = session.query(ClusterCapabilityGrant).filter_by(
            user_id=account.id, cluster_id=cluster.id, capability=payload.capability
        ).one_or_none()
        if grant is None:
            grant = ClusterCapabilityGrant(
                id=str(uuid.uuid4()), user_id=account.id, cluster_id=cluster.id,
                capability=payload.capability, granted_by=user,
            )
            session.add(grant)
        else:
            grant.is_active = True
            grant.granted_by = user
            grant.updated_at = utc_now()
        session.commit()
        return {"id": grant.id, "status": "active"}


@router.delete("/api/rbac/grants/{grant_id}")
async def revoke_grant(grant_id: str, user: str = Depends(require_login)) -> dict:
    _require_admin(user)
    with db.SessionLocal() as session:
        grant = session.get(ClusterCapabilityGrant, grant_id)
        if grant is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy capability grant")
        grant.is_active = False
        grant.updated_at = utc_now()
        session.commit()
        return {"id": grant.id, "status": "revoked"}


@router.get("/api/rbac/single-full-audits")
async def list_single_full_audits(
    limit: int = 50, user: str = Depends(require_login)
) -> dict:
    """Expose redacted Single Full audit metadata to administrators only."""
    _require_admin(user)
    limit = max(1, min(limit, 200))
    with db.SessionLocal() as session:
        rows = session.query(SingleFullAudit).order_by(
            SingleFullAudit.created_at.desc()
        ).limit(limit).all()
        return {"audits": [{
            "run_id": row.run_id,
            "actor": row.actor,
            "telegram_chat_id": row.telegram_chat_id,
            "cluster_id": row.cluster_id,
            "cluster_ref": row.cluster_ref,
            "prompt_sha256": row.prompt_sha256,
            "command_class": row.command_class,
            "operator_acknowledged": row.operator_acknowledged,
            "status": row.status,
            "result_code": row.result_code,
            "error_type": row.error_type,
            "started_at": row.started_at.isoformat(),
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        } for row in rows]}
