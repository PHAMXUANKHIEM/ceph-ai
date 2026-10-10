"""Thay OSD hỏng: replace one OSD of the selected cephadm cluster, keeping its ID (10/10/2026).

Plan/in-progress/croit-parity-operations-plan-2026-10-10.md, C1. The OSD is
chosen from the cluster's stored CRUSH snapshot (never typed); both steps are
DESTRUCTIVE lifecycle actions an admin approves by typing the confirmation,
and the Worker (worker/executor/osd_replacement.py) checks safety again.
"""

from __future__ import annotations

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
from shared import audit, cluster_snapshot, db
from shared.ceph_topology import _osds_by_host
from shared.models import Action, ActionStatus, Cluster, Incident, IncidentStatus
from shared.time import utc_now
from worker.executor import commands as executor_commands
from worker.executor import osd_replacement
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()

OSD_REPLACE_CEPH_CODE = "OSD_REPLACE"
_IN_FLIGHT = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value)
_ACTION_IDS = (osd_replacement.ACTION_START, osd_replacement.ACTION_FINISH)
_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9/_.-]+$")


def _exec_mode(cluster: Cluster) -> str:
    return settings.ceph_exec_mode if cluster.is_default else (cluster.ceph_exec_mode or "none")


def _osds(cluster: Cluster) -> list[dict]:
    """[{host, osd_id}] from the stored CRUSH snapshot; [] when there is none yet."""
    try:
        crush = cluster_snapshot.read_section_snapshot(cluster.id, "crush") or {}
    except ValueError:
        return []
    payload = crush.get("crush") if isinstance(crush.get("crush"), dict) else crush
    return [{"host": host, "osd_id": osd_id}
            for host, ids in sorted(_osds_by_host(payload).items()) for osd_id in ids]


def _latest(session, cluster: Cluster) -> Action | None:
    scope = Incident.cluster_id == cluster.id
    if cluster.is_default:
        scope = scope | Incident.cluster_id.is_(None)
    return (session.query(Action).join(Incident, Incident.id == Action.incident_id)
            .filter(Action.action_id.in_(_ACTION_IDS), scope)
            .order_by(Action.created_at.desc()).first())


def _loads(raw: str | None) -> object:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _dict(raw: str | None) -> dict:
    value = _loads(raw)
    return value if isinstance(value, dict) else {}


def _list(raw: str | None) -> list:
    value = _loads(raw)
    return value if isinstance(value, list) else []


def _awaiting_disk(action: Action | None) -> bool:
    """The start action finished: the OSD is destroyed (or still draining) and waits for its new disk."""
    return (action is not None and action.action_id == osd_replacement.ACTION_START
            and action.status == ActionStatus.EXECUTED.value)


def _recorded_disk(action: Action | None) -> tuple[str | None, str | None]:
    """(host, device) the start action's preflight recorded in its progress."""
    for step in _list(action.execution_progress if action is not None else None):
        if step.get("step") == "osd_preflight":
            for host in step.get("hosts") or []:
                if host.get("osd_host") and host.get("osd_device"):
                    return str(host["osd_host"]), str(host["osd_device"])
    return None, None


def _confirmation(osd_id: int) -> str:
    return f"THAY osd.{osd_id}"


@router.get("/replace-osd", response_class=HTMLResponse)
async def replace_osd_page(request: Request, user: str = Depends(require_login)):
    cluster = selected_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        if last is not None:
            session.expunge(last)
    pending = last if last is not None and last.status in _IN_FLIGHT else None
    params = _dict(last.action_params if last is not None else None)
    recorded_host, recorded_device = _recorded_disk(last)
    return templates.TemplateResponse(request, "replace_osd.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "cluster_name": cluster.name,
        "exec_mode": _exec_mode(cluster), "osds": _osds(cluster), "pending_action": pending, "last_action": last,
        "progress": _list(last.execution_progress if last is not None else None),
        "confirm_text": _dict(pending.action_params).get("_approval_confirmation") if pending else None,
        "awaiting_disk": _awaiting_disk(last) and pending is None, "last_params": params,
        "recorded_host": recorded_host, "recorded_device": recorded_device,
    })


@router.get("/replace-osd/progress")
async def replace_osd_progress(request: Request, user: str = Depends(require_login)):
    cluster = selected_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        return JSONResponse({"status": last.status if last else None,
                             "progress": _list(last.execution_progress if last else None)})


def _propose(user: str, cluster: Cluster, action_id: str, params: dict) -> JSONResponse:
    osd_id = int(params["osd_id"])
    params = {**params, "_cluster_id": cluster.id, "_approval_confirmation": _confirmation(osd_id)}
    try:
        preview = executor_commands.get_command(action_id, None, params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}") from exc
    if action_id == osd_replacement.ACTION_START:
        rationale = (
            f"THAY OSD HỎNG osd.{osd_id} trên cụm {cluster.name!r}.\n"
            "1. Kiểm tra an toàn: cụm không HEALTH_ERR; nếu OSD còn up thì phải còn đủ host khác cho số bản sao lớn "
            "nhất của pool và đủ chỗ trống (dư 20 %, dưới ngưỡng nearfull 5 %).\n"
            f"2. `ceph orch osd rm {osd_id} --replace`: cephadm dời dữ liệu đi, đánh dấu OSD destroyed và GIỮ ID.\n"
            "3. Chờ tối đa 20 phút. Sau đó thay ổ vật lý và bấm \"Hoàn tất thay OSD\".")
    else:
        rationale = (
            f"HOÀN TẤT THAY osd.{osd_id} trên cụm {cluster.name!r}: kiểm tra OSD đã destroyed, "
            f"XOÁ SẠCH ổ {params.get('device') or params.get('_device')} trên {params.get('_hostname')} "
            "(`ceph orch device zap --force`), tạo lại OSD cùng ID và chờ OSD up/in.")
    with db.SessionLocal() as session:
        busy = (session.query(Action).filter(Action.action_id.in_(gate.VALID_CLUSTER_DEPLOY_ACTION_IDS))
                .filter(Action.status.in_(_IN_FLIGHT)).first())
        if busy is not None:
            raise HTTPException(status_code=409, detail="Đang có một thao tác vòng đời cụm chờ duyệt hoặc đang chạy.")
        incident = Incident(cluster_id=cluster.id, ceph_code=OSD_REPLACE_CEPH_CODE,
                            status=IncidentStatus.PENDING_APPROVAL.value, detected_at=utc_now(),
                            log_excerpt=f"Đề xuất thay osd.{osd_id} trên cụm {cluster.name!r} bởi {user}")
        session.add(incident)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="Lượt thay OSD trước của cụm này vẫn đang mở.") from exc
        action = Action(
            incident_id=incident.id, action_id=action_id, classification=gate.classify_action(action_id).value,
            status=ActionStatus.PENDING_APPROVAL.value, rationale=rationale,
            target_nodes=json.dumps([str(params.get("_hostname") or f"osd.{osd_id}")]),
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


@router.post("/replace-osd/propose")
async def propose_replace_osd(request: Request, user: str = Depends(require_login)):
    cluster = selected_cluster(request)
    if _exec_mode(cluster) != "cephadm":
        raise HTTPException(status_code=400, detail=f"Thay OSD chỉ hỗ trợ cụm cephadm (cụm này: {_exec_mode(cluster)}).")
    body = await request.json()
    try:
        osd_id = int(body.get("osd_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Chọn một OSD.") from exc
    if osd_id not in {item["osd_id"] for item in _osds(cluster)}:
        raise HTTPException(status_code=400, detail="Chỉ chọn được OSD có trong cây CRUSH của cụm này.")
    if str(body.get("confirmation") or "").strip() != _confirmation(osd_id):
        raise HTTPException(status_code=400, detail=f"Gõ đúng: {_confirmation(osd_id)}")
    return _propose(user, cluster, osd_replacement.ACTION_START, {"osd_id": osd_id})


@router.post("/replace-osd/finish")
async def finish_replace_osd(request: Request, user: str = Depends(require_login)):
    cluster = selected_cluster(request)
    with db.SessionLocal() as session:
        last = _latest(session, cluster)
        awaiting = _awaiting_disk(last)
        params = _dict(last.action_params if last is not None else None)
        recorded_host, recorded_device = _recorded_disk(last)
    if not awaiting:
        raise HTTPException(status_code=400, detail="Không có OSD nào đang chờ thay ổ.")
    body = await request.json()
    device = str(body.get("device") or "").strip()
    if device and not _DEVICE_RE.match(device):
        raise HTTPException(status_code=400, detail="Đường dẫn ổ phải dạng /dev/<tên>.")
    osd_id = int(params["osd_id"])
    if str(body.get("confirmation") or "").strip() != _confirmation(osd_id):
        raise HTTPException(status_code=400, detail=f"Gõ đúng: {_confirmation(osd_id)}")
    if not recorded_host:
        raise HTTPException(status_code=400, detail="Không tìm thấy host/ổ của OSD trong tiến trình lượt thay trước.")
    finish = {"osd_id": osd_id, "_hostname": recorded_host, "_device": recorded_device}
    if device:
        finish["device"] = device
    return _propose(user, cluster, osd_replacement.ACTION_FINISH, finish)
