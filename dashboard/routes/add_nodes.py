"""Thêm node: add hosts and their OSDs to the selected running cephadm cluster (10/10/2026).

Plan/in-progress/croit-parity-operations-plan-2026-10-10.md, C3. The proposal
is a RISKY lifecycle action approved by typing the new IPs; the Worker
(worker/executor/cluster_expansion.py) checks SSH, OS and that every chosen
disk is empty before touching anything, and refuses hosts already in the
cluster.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
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
from worker.executor import cluster_expansion
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()

CLUSTER_NODE_ADD_CEPH_CODE = "CLUSTER_NODE_ADD"
_IN_FLIGHT = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value)
_DISK_RE = re.compile(r"^/dev/[A-Za-z0-9/_.-]+$")
MAX_NEW_NODES = 10


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
            .filter(Action.action_id == cluster_expansion.ACTION_ID, scope)
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


def _confirmation(ips: list[str]) -> str:
    return "THÊM " + ", ".join(ips)


def _parse_nodes(raw: object, known: set[str]) -> list[dict]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_NEW_NODES:
        raise HTTPException(status_code=400, detail=f"Nhập 1–{MAX_NEW_NODES} node mới.")
    nodes, seen = [], set()
    for item in raw:
        ip = str((item or {}).get("ip") or "").strip()
        try:
            ipaddress.IPv4Address(ip)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"IP không hợp lệ: {ip!r}") from exc
        if ip in seen or ip in known:
            raise HTTPException(status_code=400, detail=f"{ip} bị trùng hoặc đã có trong cụm.")
        seen.add(ip)
        disks = [str(disk).strip() for disk in (item or {}).get("osd_disks") or [] if str(disk).strip()]
        bad = [disk for disk in disks if not _DISK_RE.match(disk)]
        if bad or len(set(disks)) != len(disks):
            raise HTTPException(status_code=400, detail=f"{ip}: đĩa phải dạng /dev/<tên>, không trùng ({bad or disks}).")
        nodes.append({"ip": ip, "roles": ["osd"] if disks else [], "osd_disks": disks})
    return nodes


@router.get("/add-nodes", response_class=HTMLResponse)
async def add_nodes_page(request: Request, user: str = Depends(require_login)):
    cluster, row, exec_mode = _target_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        if last is not None:
            session.expunge(last)
    pending = last if last is not None and last.status in _IN_FLIGHT else None
    return templates.TemplateResponse(request, "add_nodes.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "cluster_name": cluster.name, "exec_mode": exec_mode,
        "nodes": configured_nodes(row), "pending_action": pending, "last_action": last,
        "progress": _list(last.execution_progress if last is not None else None),
        "confirm_text": _dict(pending.action_params).get("_approval_confirmation") if pending else None,
    })


@router.get("/add-nodes/progress")
async def add_nodes_progress(request: Request, user: str = Depends(require_login)):
    cluster, _row, _mode = _target_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        return JSONResponse({"status": last.status if last else None,
                             "progress": _list(last.execution_progress if last else None)})


@router.post("/add-nodes/propose")
async def propose_add_nodes(request: Request, user: str = Depends(require_login)):
    cluster, row, exec_mode = _target_cluster(request)
    if exec_mode != "cephadm":
        raise HTTPException(status_code=400, detail=f"Thêm node chỉ hỗ trợ cụm cephadm (cụm này: {exec_mode}).")
    current = configured_nodes(row)
    mons = [node["host"] for node in current if "MON" in node.get("roles", [])]
    if not mons:
        raise HTTPException(status_code=400, detail="Cụm chưa có MON nào trong cấu hình.")
    body = await request.json()
    nodes = _parse_nodes(body.get("nodes"), {node["host"] for node in current})
    ips = [node["ip"] for node in nodes]
    confirmation = _confirmation(ips)
    if str(body.get("confirmation") or "").strip() != confirmation:
        raise HTTPException(status_code=400, detail=f"Gõ đúng: {confirmation}")
    params = {"nodes": nodes, "_admin_mon": mons[0], "_cluster_id": cluster.id,
              "_approval_confirmation": confirmation}
    try:
        preview = executor_commands.get_command(cluster_expansion.ACTION_ID, ips[0], params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}") from exc
    rationale = (
        f"THÊM NODE vào cụm {cluster.name!r} (qua MON {mons[0]}): "
        + "; ".join(f"{n['ip']} — {', '.join(n['osd_disks']) or 'không thêm OSD'}" for n in nodes) + ".\n"
        "1. Kiểm tra SSH, hệ điều hành, đĩa trống (không filesystem/LVM, không mount) và host chưa thuộc cụm.\n"
        "2. Cài chrony + podman/docker; thêm khoá SSH của cephadm; `ceph orch host add`.\n"
        "3. `ceph orch daemon add osd` trên các đĩa đã chọn; chờ OSD up/in (tối đa 15 phút).\n"
        "Sau đó IP mới được thêm vào danh sách node OSD của cụm trong cấu hình.")
    with db.SessionLocal() as session:
        busy = (session.query(Action).filter(Action.action_id.in_(gate.VALID_CLUSTER_DEPLOY_ACTION_IDS))
                .filter(Action.status.in_(_IN_FLIGHT)).first())
        if busy is not None:
            raise HTTPException(status_code=409, detail="Đang có một thao tác vòng đời cụm chờ duyệt hoặc đang chạy.")
        incident = Incident(cluster_id=cluster.id, ceph_code=CLUSTER_NODE_ADD_CEPH_CODE,
                            status=IncidentStatus.PENDING_APPROVAL.value, detected_at=utc_now(),
                            log_excerpt=f"Đề xuất thêm {', '.join(ips)} vào cụm {cluster.name!r} bởi {user}")
        session.add(incident)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="Lượt thêm node trước của cụm này vẫn đang mở.") from exc
        action = Action(
            incident_id=incident.id, action_id=cluster_expansion.ACTION_ID,
            classification=gate.classify_action(cluster_expansion.ACTION_ID).value,
            # The MON the Worker runs `ceph orch` on: the new hosts are not configured nodes yet, and the
            # Worker refuses targets outside the configuration (seen with OSD replacement on 12/10/2026).
            status=ActionStatus.PENDING_APPROVAL.value, rationale=rationale, target_nodes=json.dumps([mons[0]]),
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
