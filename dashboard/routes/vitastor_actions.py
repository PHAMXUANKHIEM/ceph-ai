"""Vitastor remediation approvals + audit trail — the Vitastor counterpart of
dashboard/routes/actions.py, isolated under the ``/vitastor`` namespace."""

import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from dashboard.routes import auth
from dashboard.routes.vitastor import _cluster_or_404, require_vitastor_login
from shared import db
from shared.models import (
    VitastorActionStatus,
    VitastorAuditEntry,
    VitastorCluster,
    VitastorRemediationAction,
)
from vitastor.operations import VitastorOperationError
from vitastor.remediation import (
    VitastorRemediationError,
    known_hosts,
    record_audit,
    run_remediation,
)

router = APIRouter(prefix="/vitastor", tags=["vitastor-actions"])


def _require_admin(user: str) -> None:
    if not auth.is_vitastor_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ Vitastor admin được duyệt hoặc từ chối hành động")


def _action_dict(row: VitastorRemediationAction) -> dict:
    return {
        "id": row.id, "cluster_id": row.cluster_id, "source": row.source,
        "action_id": row.action_id, "classification": row.classification,
        "status": row.status, "target_host": row.target_host,
        "params": json.loads(row.action_params) if row.action_params else {},
        "command": row.proposed_command, "rationale": row.rationale,
        "result_output": row.result_output, "error": row.error_message,
        "requested_by": row.requested_by, "approved_by": row.approved_by,
        "created_at": row.created_at.isoformat() + "Z",
        "executed_at": row.executed_at.isoformat() + "Z" if row.executed_at else None,
    }


@router.get("/api/actions")
async def list_actions(cluster_id: str, user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        _cluster_or_404(session, cluster_id)
        base = session.query(VitastorRemediationAction).filter_by(cluster_id=cluster_id)
        pending = base.filter(
            VitastorRemediationAction.status == VitastorActionStatus.PENDING_APPROVAL.value
        ).order_by(VitastorRemediationAction.created_at.desc()).all()
        recent = base.filter(
            VitastorRemediationAction.status != VitastorActionStatus.PENDING_APPROVAL.value
        ).order_by(VitastorRemediationAction.updated_at.desc()).limit(50).all()
        return {
            "is_admin": auth.is_vitastor_admin_user(user),
            "pending": [_action_dict(row) for row in pending],
            "recent": [_action_dict(row) for row in recent],
        }


@router.get("/api/audit")
async def list_audit(cluster_id: str, user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        _cluster_or_404(session, cluster_id)
        rows = session.query(VitastorAuditEntry).filter_by(cluster_id=cluster_id).order_by(
            VitastorAuditEntry.created_at.desc()
        ).limit(100).all()
        return {"entries": [{
            "id": row.id, "action_pk": row.action_pk, "event_type": row.event_type,
            "actor": row.actor, "detail": row.detail,
            "created_at": row.created_at.isoformat() + "Z",
        } for row in rows]}


def _execute_approved(action_pk: str, actor: str) -> None:
    """Run an APPROVED remediation over SSH (Starlette runs this sync function
    in a threadpool, so its blocking SSH never stalls the event loop — same
    posture as dashboard/routes/vitastor_lifecycle.py::_execute). Each DB
    transition is its own short session; the SSH call happens between them."""
    with db.SessionLocal() as session:
        row = session.get(VitastorRemediationAction, action_pk)
        if not row or row.status != VitastorActionStatus.APPROVED.value:
            return
        cluster = session.query(VitastorCluster).filter_by(id=row.cluster_id).first()
        if not cluster:
            row.status = VitastorActionStatus.FAILED.value
            row.error_message = "Cụm Vitastor không còn tồn tại"
            record_audit(session, row.cluster_id, row.id, "FAILED", actor, row.error_message)
            session.commit()
            return
        action_id, params = row.action_id, json.loads(row.action_params or "{}")
        target_host, command = row.target_host, row.proposed_command
        ssh_user, ssh_key = cluster.ssh_user, cluster.ssh_key_path
        allowed = known_hosts(cluster)
        row.status = VitastorActionStatus.EXECUTING.value
        record_audit(session, row.cluster_id, row.id, "EXECUTING", actor, command)
        session.commit()
    try:
        output = run_remediation(action_id, params, target_host, ssh_user, ssh_key, allowed)
    except (VitastorRemediationError, VitastorOperationError) as exc:
        with db.SessionLocal() as session:
            row = session.get(VitastorRemediationAction, action_pk)
            row.status = VitastorActionStatus.FAILED.value
            row.error_message = str(exc)
            record_audit(session, row.cluster_id, row.id, "FAILED", actor, str(exc))
            session.commit()
        return
    with db.SessionLocal() as session:
        row = session.get(VitastorRemediationAction, action_pk)
        row.status = VitastorActionStatus.EXECUTED.value
        row.result_output = output
        row.executed_at = datetime.utcnow()
        record_audit(session, row.cluster_id, row.id, "EXECUTED", actor, output[-500:] or "(không có output)")
        session.commit()


@router.post("/api/actions/{action_pk}/approve")
async def approve_action(action_pk: str, background: BackgroundTasks, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        row = session.get(VitastorRemediationAction, action_pk)
        if not row or row.status != VitastorActionStatus.PENDING_APPROVAL.value:
            raise HTTPException(status_code=409, detail="Hành động không còn ở trạng thái chờ duyệt")
        if not session.query(VitastorCluster).filter_by(id=row.cluster_id).first():
            raise HTTPException(status_code=404, detail="Cụm Vitastor không còn tồn tại")
        row.status = VitastorActionStatus.APPROVED.value
        row.approved_by = user
        record_audit(session, row.cluster_id, row.id, "APPROVED", user)
        session.commit()
    background.add_task(_execute_approved, action_pk, user)
    return {"status": VitastorActionStatus.APPROVED.value}


@router.post("/api/actions/{action_pk}/reject")
async def reject_action(action_pk: str, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        row = session.get(VitastorRemediationAction, action_pk)
        if not row or row.status != VitastorActionStatus.PENDING_APPROVAL.value:
            raise HTTPException(status_code=409, detail="Không thể từ chối hành động này")
        row.status = VitastorActionStatus.REJECTED.value
        row.approved_by = user
        record_audit(session, row.cluster_id, row.id, "REJECTED", user)
        session.commit()
    return {"status": VitastorActionStatus.REJECTED.value}


@router.get("/api/actions/{action_pk}")
async def action_detail(action_pk: str, user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        row = session.get(VitastorRemediationAction, action_pk)
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy hành động")
        return {"action": _action_dict(row)}
