"""Operator view for persisted remediation runtime decisions."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.routes.auth import is_admin_user, require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import AutopilotClusterConfigAudit, Cluster, RemediationRuntimeDecision

router = APIRouter()
templates = make_templates()
VALID_MODES = {"ADVISORY", "APPROVAL_REQUIRED", "LIMITED_AUTOPILOT", "LEGACY"}


def _clusters(session):
    return session.query(Cluster).filter_by(is_active=True).order_by(
        Cluster.is_default.desc(), Cluster.name,
    ).all()


@router.get("/remediation-runtime", response_class=HTMLResponse)
async def remediation_runtime_page(request: Request, user: str = Depends(require_login)):
    cluster_id = request.query_params.get("cluster")
    with db.SessionLocal() as session:
        clusters = _clusters(session)
        selected = session.get(Cluster, cluster_id) if cluster_id else None
        selected = selected if selected and selected.is_active else (clusters[0] if clusters else None)
        decisions = session.query(RemediationRuntimeDecision).filter(
            RemediationRuntimeDecision.cluster_id == selected.id if selected else False,
        ).order_by(RemediationRuntimeDecision.created_at.desc()).limit(100).all()
    return templates.TemplateResponse(request, "remediation_runtime.html", {
        "user": user, "is_admin": is_admin_user(user), "clusters": clusters,
        "selected_cluster": selected, "decisions": decisions,
        "valid_modes": sorted(VALID_MODES - {"LEGACY"}),
    })


@router.get("/api/remediation-runtime/decisions")
async def remediation_runtime_decisions(request: Request, user: str = Depends(require_login)):
    cluster_id = request.query_params.get("cluster")
    with db.SessionLocal() as session:
        query = session.query(RemediationRuntimeDecision)
        if cluster_id:
            query = query.filter(RemediationRuntimeDecision.cluster_id == cluster_id)
        rows = query.order_by(RemediationRuntimeDecision.created_at.desc()).limit(100).all()
        return {"decisions": [{
            "id": row.id, "cluster_id": row.cluster_id, "incident_id": row.incident_id,
            "action_id": row.action_id, "worker_id": row.worker_id, "mode": row.mode,
            "classification": row.classification, "decision": row.decision,
            "reason": row.reason, "controls": json.loads(row.controls_json or "{}"),
            "created_at": row.created_at,
        } for row in rows]}


@router.post("/remediation-runtime/clusters/{cluster_id}/mode")
async def update_cluster_mode(
    cluster_id: str, request: Request, mode: str = Form(...),
    reason: str = Form(""), user: str = Depends(require_login),
):
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được thay đổi Autopilot mode")
    mode = mode.strip().upper()
    reason = reason.strip()
    if mode not in VALID_MODES - {"LEGACY"} or len(reason) < 8:
        raise HTTPException(status_code=422, detail="Mode hoặc lý do không hợp lệ.")
    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None or not cluster.is_active:
            raise HTTPException(status_code=404, detail="Không tìm thấy cluster")
        previous = cluster.autopilot_mode
        cluster.autopilot_mode = mode
        session.add(AutopilotClusterConfigAudit(
            cluster_id=cluster.id, actor=user,
            previous_environment=cluster.autonomy_environment,
            new_environment=cluster.autonomy_environment,
            previous_enabled=cluster.autopilot_enabled,
            new_enabled=cluster.autopilot_enabled,
            reason=f"Mode {previous} -> {mode}: {reason}",
        ))
        session.commit()
    return RedirectResponse(f"/remediation-runtime?cluster={cluster_id}", status_code=303)
