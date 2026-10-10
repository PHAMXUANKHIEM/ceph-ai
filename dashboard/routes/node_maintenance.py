"""Bảo trì node: reboot hosts of the selected cephadm cluster one at a time (10/10/2026).

Plan/in-progress/croit-parity-operations-plan-2026-10-10.md, C2. Hosts are
chosen from the cluster's configured node list (never typed), in the order
they will be maintained; the proposal is a DESTRUCTIVE lifecycle action that
an admin approves by typing the IPs, and the Worker
(worker/executor/host_maintenance.py) checks safety before and after each host.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from dashboard.cluster_scope import selected_cluster
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import audit, db
from shared.cluster_nodes import configured_nodes
from shared.models import Action, ActionStatus, Cluster, Incident, IncidentStatus
from shared.time import utc_now
from worker.executor import commands as executor_commands
from worker.executor import host_maintenance
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()

NODE_MAINTENANCE_CEPH_CODE = "NODE_MAINTENANCE"
_IN_FLIGHT = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value)


def _target_cluster(request: Request) -> tuple[Cluster, Cluster | None, str]:
    cluster = selected_cluster(request)
    if cluster.is_default:
        return cluster, None, settings.ceph_exec_mode
    return cluster, cluster, cluster.ceph_exec_mode or "none"


def _latest(session, cluster: Cluster) -> Action | None:
    scope = Incident.cluster_id == cluster.id
    if cluster.is_default:
        scope = scope | Incident.cluster_id.is_(None)
    return (session.query(Action).join(Incident, Incident.id == Action.incident_id)
            .filter(Action.action_id == host_maintenance.ACTION_ID, scope)
            .order_by(Action.created_at.desc()).first())


def _list(raw: str | None) -> list:
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _dict(raw: str | None) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _confirmation(targets: list[str]) -> str:
    return "BẢO TRÌ " + ", ".join(targets)


@router.get("/node-maintenance", response_class=HTMLResponse)
async def node_maintenance_page(request: Request, user: str = Depends(require_login)):
    cluster, row, exec_mode = _target_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        if last is not None:
            session.expunge(last)
    pending = last if last is not None and last.status in _IN_FLIGHT else None
    return templates.TemplateResponse(request, "node_maintenance.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "cluster_name": cluster.name, "exec_mode": exec_mode,
        "nodes": configured_nodes(row), "pending_action": pending, "last_action": last,
        "progress": _list(last.execution_progress if last is not None else None),
        "confirm_text": _dict(pending.action_params).get("_approval_confirmation") if pending else None,
    })


@router.get("/node-maintenance/progress")
async def node_maintenance_progress(request: Request, user: str = Depends(require_login)):
    cluster, _row, _mode = _target_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        return JSONResponse({"status": last.status if last else None,
                             "progress": _list(last.execution_progress if last else None)})


@router.post("/node-maintenance/propose")
async def propose_node_maintenance(request: Request, user: str = Depends(require_login)):
    cluster, row, exec_mode = _target_cluster(request)
    if exec_mode != "cephadm":
        raise HTTPException(status_code=400, detail=f"Bảo trì node chỉ hỗ trợ cụm cephadm (cụm này: {exec_mode}).")
    body = await request.json()
    picked = body.get("targets")
    known = {node["host"] for node in configured_nodes(row)}
    if not isinstance(picked, list) or not picked:
        raise HTTPException(status_code=400, detail="Chọn ít nhất một node.")
    targets = [str(item) for item in picked]
    if len(set(targets)) != len(targets) or not set(targets) <= known:
        raise HTTPException(status_code=400, detail="Chỉ chọn được node (không trùng) trong danh sách node của cụm này.")
    confirmation = _confirmation(targets)
    if str(body.get("confirmation") or "").strip() != confirmation:
        raise HTTPException(status_code=400, detail=f"Gõ đúng: {confirmation}")
    params = {"targets": targets, "_cluster_id": cluster.id, "_approval_confirmation": confirmation}
    try:
        preview = executor_commands.get_command(host_maintenance.ACTION_ID, targets[0], params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}") from exc
    rationale = (
        f"BẢO TRÌ NODE cụm {cluster.name!r}, lần lượt: {', '.join(targets)}.\n"
        "Với từng host: `ceph orch host ok-to-stop`; chuyển MGR active đi nếu có; `ceph orch host maintenance "
        "enter` (dừng mọi daemon của host, noout cho host); reboot; chờ SSH trả lời (tối đa 15 phút); "
        "`maintenance exit`; chờ health về như trước khi bắt đầu (tối đa 20 phút) rồi mới sang host kế.\n"
        "Host nào không về hoặc cụm không hồi phục thì DỪNG, các host sau không bị đụng tới.")
    with db.SessionLocal() as session:
        busy = (session.query(Action).filter(Action.action_id.in_(gate.VALID_CLUSTER_DEPLOY_ACTION_IDS))
                .filter(Action.status.in_(_IN_FLIGHT)).first())
        if busy is not None:
            raise HTTPException(status_code=409, detail="Đang có một thao tác vòng đời cụm chờ duyệt hoặc đang chạy.")
        incident = Incident(cluster_id=cluster.id, ceph_code=NODE_MAINTENANCE_CEPH_CODE,
                            status=IncidentStatus.PENDING_APPROVAL.value, detected_at=utc_now(),
                            log_excerpt=f"Đề xuất bảo trì {', '.join(targets)} trên cụm {cluster.name!r} bởi {user}")
        session.add(incident)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="Lượt bảo trì trước của cụm này vẫn đang mở.") from exc
        action = Action(
            incident_id=incident.id, action_id=host_maintenance.ACTION_ID,
            classification=gate.classify_action(host_maintenance.ACTION_ID).value,
            status=ActionStatus.PENDING_APPROVAL.value, rationale=rationale, target_nodes=json.dumps(targets),
            action_params=json.dumps(params), proposed_command=preview,
            expires_at=utc_now() + timedelta(hours=max(1, settings.action_approval_expiry_hours)),
            idempotency_key=gate.CLUSTER_LIFECYCLE_IDEMPOTENCY_KEY,
        )
        session.add(action)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="Một thao tác vòng đời cụm khác vừa được tạo.") from exc
        audit.record(session, incident_id=incident.id, action_id=action.id,
                     event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL, actor=user)
        session.commit()
        return JSONResponse({"action_id": action.id}, status_code=201)
