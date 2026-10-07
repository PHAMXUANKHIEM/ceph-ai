"""Gỡ node khỏi cụm: drain hosts out of the selected cephadm cluster (07/10/2026).

The page works on the cluster selected in the switcher. Nodes are chosen
from that cluster's configured node list (never typed), the proposal is a
DESTRUCTIVE lifecycle action that needs an admin to type the node IPs to
approve, and the Worker (worker/executor/node_removal.py) checks safety
again before draining. When the drain outlives the action, a second RISKY
action "Hoàn tất gỡ node" removes the host once its data has moved.
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
from worker.executor import node_removal
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()

CLUSTER_NODE_REMOVE_CEPH_CODE = "CLUSTER_NODE_REMOVE"
_IN_FLIGHT = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value)
_ACTION_IDS = (node_removal.ACTION_START, node_removal.ACTION_FINISH)


def _target_cluster(request: Request) -> tuple[Cluster, Cluster | None, str]:
    """(selected cluster, its row for configured_nodes or None for the default, exec mode)."""
    cluster = selected_cluster(request)
    if cluster.is_default:
        return cluster, None, settings.ceph_exec_mode
    return cluster, cluster, cluster.ceph_exec_mode or "none"


def _latest(session, cluster: Cluster) -> Action | None:
    scope = Incident.cluster_id == cluster.id
    if cluster.is_default:
        scope = scope | Incident.cluster_id.is_(None)
    return (session.query(Action).join(Incident, Incident.id == Action.incident_id)
            .filter(Action.action_id.in_(_ACTION_IDS), scope)
            .order_by(Action.created_at.desc()).first())


def _progress(action: Action | None) -> list:
    try:
        return json.loads(action.execution_progress) if action is not None and action.execution_progress else []
    except (TypeError, ValueError):
        return []


def _params(action: Action | None) -> dict:
    try:
        return json.loads(action.action_params) if action is not None and action.action_params else {}
    except (TypeError, ValueError):
        return {}


def _waiting_for_drain(action: Action | None) -> bool:
    """The start action finished but left the hosts draining (no `ceph orch host rm` yet)."""
    if action is None or action.action_id != node_removal.ACTION_START or action.status != ActionStatus.EXECUTED.value:
        return False
    step: dict = next((item for item in _progress(action) if item.get("step") == "rm_host"), {})
    return any(str(host.get("message", "")).startswith("chưa gỡ") for host in step.get("hosts") or [])


def _confirmation(targets: list[str]) -> str:
    return "GỠ " + ", ".join(targets)


@router.get("/remove-nodes", response_class=HTMLResponse)
async def remove_nodes_page(request: Request, user: str = Depends(require_login)):
    cluster, row, exec_mode = _target_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        if last is not None:
            session.expunge(last)
    pending = last if last is not None and last.status in _IN_FLIGHT else None
    return templates.TemplateResponse(request, "remove_nodes.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "cluster_name": cluster.name, "exec_mode": exec_mode,
        "nodes": configured_nodes(row), "pending_action": pending, "last_action": last,
        "progress": _progress(last), "confirm_text": _params(pending).get("_approval_confirmation"),
        "waiting_for_drain": _waiting_for_drain(last), "last_targets": _params(last).get("targets") or [],
    })


@router.get("/remove-nodes/progress")
async def remove_nodes_progress(request: Request, user: str = Depends(require_login)):
    cluster, _row, _mode = _target_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        return JSONResponse({"status": last.status if last else None, "progress": _progress(last)})


def _propose(user: str, cluster: Cluster, row: Cluster | None, action_id: str, targets: list[str],
             zap_devices: bool) -> JSONResponse:
    confirmation = _confirmation(targets)
    params = {"targets": targets, "zap_devices": zap_devices, "_cluster_id": cluster.id,
              "_node_config_fingerprint": node_removal.config_fingerprint(row),
              "_approval_confirmation": confirmation}
    try:
        preview = executor_commands.get_command(action_id, targets[0], params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}") from exc
    starting = action_id == node_removal.ACTION_START
    rationale = (
        f"GỠ NODE khỏi cụm {cluster.name!r}: {', '.join(targets)}.\n"
        + ("1. Kiểm tra an toàn: còn ≥ 3 MON và ≥ 1 MGR, số host có OSD còn lại ≥ số bản sao lớn nhất của pool, "
           "các OSD còn lại đủ chỗ (dư 20 %, dưới ngưỡng nearfull 5 %), cụm không HEALTH_ERR.\n"
           f"2. `ceph orch host drain`{' --zap-osd-devices (XOÁ dữ liệu đĩa sau khi dời)' if zap_devices else ''}: "
           "cephadm dời daemon đi, dời dữ liệu khỏi các OSD rồi mới gỡ OSD.\n"
           "3. Chờ tối đa 20 phút; xong thì `ceph orch host rm`. Chưa xong thì để drain chạy tiếp và bấm "
           "\"Hoàn tất gỡ node\" sau.\n" if starting else
           "1. Kiểm tra drain đã xong (không còn OSD chờ gỡ, không còn daemon trên host).\n"
           "2. `ceph orch host rm`.\n")
        + "Sau khi gỡ host, IP của nó được bỏ khỏi danh sách node của cụm trong cấu hình."
    )
    with db.SessionLocal() as session:
        busy = (session.query(Action).filter(Action.action_id.in_(gate.VALID_CLUSTER_DEPLOY_ACTION_IDS))
                .filter(Action.status.in_(_IN_FLIGHT)).first())
        if busy is not None:
            raise HTTPException(status_code=409, detail="Đang có một thao tác vòng đời cụm chờ duyệt hoặc đang chạy.")
        incident = Incident(cluster_id=cluster.id, ceph_code=CLUSTER_NODE_REMOVE_CEPH_CODE,
                            status=IncidentStatus.PENDING_APPROVAL.value, detected_at=utc_now(),
                            log_excerpt=f"Đề xuất gỡ {', '.join(targets)} khỏi cụm {cluster.name!r} bởi {user}")
        session.add(incident)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="Lượt gỡ node trước của cụm này vẫn đang mở.") from exc
        action = Action(
            incident_id=incident.id, action_id=action_id, classification=gate.classify_action(action_id).value,
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


@router.post("/remove-nodes/propose")
async def propose_remove_nodes(request: Request, user: str = Depends(require_login)):
    cluster, row, exec_mode = _target_cluster(request)
    if exec_mode != "cephadm":
        raise HTTPException(status_code=400, detail=f"Gỡ node chỉ hỗ trợ cụm cephadm (cụm này: {exec_mode}).")
    body = await request.json()
    known = [node["host"] for node in configured_nodes(row)]
    picked = body.get("targets")
    if not isinstance(picked, list) or not picked:
        raise HTTPException(status_code=400, detail="Chọn ít nhất một node.")
    targets = [ip for ip in known if ip in {str(item) for item in picked}]
    if len(targets) != len(set(picked)):
        raise HTTPException(status_code=400, detail="Chỉ chọn được node trong danh sách node của cụm này.")
    if len(targets) >= len(known):
        raise HTTPException(status_code=400, detail="Không thể gỡ mọi node — dùng Delete Cluster.")
    if str(body.get("confirmation") or "").strip() != _confirmation(targets):
        raise HTTPException(status_code=400, detail=f"Gõ đúng: {_confirmation(targets)}")
    return _propose(user, cluster, row, node_removal.ACTION_START, targets, bool(body.get("zap_devices")))


@router.post("/remove-nodes/finish")
async def finish_remove_nodes(request: Request, user: str = Depends(require_login)):
    cluster, row, _mode = _target_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        waiting, params = _waiting_for_drain(last), _params(last)
    if not waiting:
        raise HTTPException(status_code=400, detail="Không có lượt gỡ node nào đang chờ hoàn tất.")
    return _propose(user, cluster, row, node_removal.ACTION_FINISH, list(params.get("targets") or []),
                    bool(params.get("zap_devices")))
