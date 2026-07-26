import json
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import audit, db
from shared.cluster_nodes import configured_nodes
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()

# Synthetic Incident.ceph_code for this feature — same trick
# dashboard/routes/deploy_cluster.py/upgrade.py/patch.py/chat.py use:
# AuditEntry.incident_id is a required FK, and tearing down a cluster has
# no real detected Incident behind it, only an operator explicitly
# requesting it.
CLUSTER_DELETE_CEPH_CODE = "CLUSTER_DELETE"

DELETE_CEPHADM_ACTION_ID = "delete_cluster_cephadm"
DELETE_MANUAL_ACTION_ID = "delete_cluster_manual"
CLUSTER_DELETE_ACTION_IDS = frozenset({DELETE_CEPHADM_ACTION_ID, DELETE_MANUAL_ACTION_ID})

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)

_ROLE_UPPER_TO_LOWER = {"MON": "mon", "MGR": "mgr", "OSD": "osd", "RGW": "rgw"}

_OSD_DISK_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")


def _normalize_configured_nodes() -> list[dict]:
    """shared/cluster_nodes.py::configured_nodes() returns
    {"host": .., "roles": ["MON", "OSD"]} (uppercase roles, "host" key) —
    normalized here to the SAME lowercase {"ip": .., "roles": [...],
    "osd_disk": None} shape worker/executor/cluster_deploy.py's phase
    helpers already expect (e.g. _first_mon_ip, _node_ips_with_role), so
    those are reused completely unchanged for deletion too."""
    nodes = []
    for n in configured_nodes():
        roles = [_ROLE_UPPER_TO_LOWER[r] for r in n["roles"] if r in _ROLE_UPPER_TO_LOWER]
        nodes.append({"ip": n["host"], "roles": roles, "osd_disk": None})
    return nodes


def _delete_plan_text(exec_mode: str, nodes: list[dict], wipe_osd_disks: bool) -> str:
    mon = [n["ip"] for n in nodes if "mon" in n["roles"]]
    mgr = [n["ip"] for n in nodes if "mgr" in n["roles"]]
    osd_nodes = [n for n in nodes if "osd" in n["roles"]]
    if wipe_osd_disks:
        osd = [f"{n['ip']} ({n.get('osd_disk')})" for n in osd_nodes]
    else:
        osd = [n["ip"] for n in osd_nodes]
    node_summary = f"MON: {', '.join(mon)}\nMGR: {', '.join(mgr)}\nOSD: {', '.join(osd)}"

    wipe_note = (
        "CÓ xoá — dữ liệu trên đĩa OSD của từng node liệt kê ở trên sẽ bị XOÁ VĨNH VIỄN, "
        "KHÔNG THỂ HOÀN TÁC."
        if wipe_osd_disks
        else "KHÔNG xoá — dữ liệu trên đĩa OSD được giữ nguyên (chỉ dừng daemon và xoá cấu hình Ceph)."
    )

    if exec_mode == "cephadm":
        steps = (
            f"Các bước sẽ thực hiện, LẦN LƯỢT, sau khi Duyệt:\n"
            f"1. Kiểm tra kết nối SSH tới từng node.\n"
            f"2. `cephadm rm-cluster --force{'--zap-osds' if wipe_osd_disks else ''}` cho từng "
            f"fsid tìm thấy trên node MON đầu tiên — dừng/xoá toàn bộ daemon, container, "
            f"/etc/ceph, /var/lib/ceph/<fsid>{' và ZAP luôn đĩa OSD' if wipe_osd_disks else ''}.\n"
        )
    else:
        steps = (
            f"Các bước sẽ thực hiện, LẦN LƯỢT, sau khi Duyệt:\n"
            f"1. Kiểm tra kết nối SSH tới từng node.\n"
            f"2. Dừng & vô hiệu hoá mọi daemon Ceph (systemctl stop/disable) trên từng node.\n"
            f"3. Xoá /etc/ceph và /var/lib/ceph trên từng node.\n"
            f"4. {'Xoá dữ liệu đĩa OSD bằng `ceph-volume lvm zap --destroy`' if wipe_osd_disks else 'KHÔNG đụng tới đĩa OSD'} "
            f"trên từng node OSD.\n"
        )

    return (
        f"XOÁ CỤM CEPH đang cấu hình (phương thức hiện tại: {exec_mode}) — {len(nodes)} node.\n"
        f"{node_summary}\n\n"
        f"{steps}\n"
        f"Xoá dữ liệu đĩa OSD: {wipe_note}\n\n"
        f"An toàn: đây là hành động PHÁ HUỶ, không thể hoàn tác một khi đã chạy — kill-switch được "
        f"kiểm tra lại trước MỖI bước; nếu một bước lỗi, các bước sau không chạy. Sau khi xoá thành "
        f"công, cấu hình cụm trong .env cũng được xoá theo — Dashboard sẽ không còn theo dõi cụm này."
    )


@router.get("/delete-cluster", response_class=HTMLResponse)
async def delete_cluster_page(request: Request, user: str = Depends(require_login)):
    try:
        with db.SessionLocal() as session:
            last_action = (
                session.query(Action)
                .filter(Action.action_id.in_(CLUSTER_DELETE_ACTION_IDS))
                .order_by(Action.created_at.desc())
                .first()
            )
    except SQLAlchemyError:
        logger.exception("delete_cluster_page: failed to query DB")
        raise HTTPException(
            status_code=503,
            detail="Không kết nối được database — đã chạy `alembic upgrade head` chưa?",
        )

    pending_action = (
        last_action if last_action is not None and last_action.status in _IN_FLIGHT_ACTION_STATUSES else None
    )

    progress: list = []
    if last_action is not None and last_action.execution_progress:
        try:
            progress = json.loads(last_action.execution_progress) or []
        except (TypeError, ValueError):
            progress = []

    nodes = _normalize_configured_nodes()
    osd_nodes = [n for n in nodes if "osd" in n["roles"]]
    first_mon_ip = next((n["ip"] for n in nodes if "mon" in n["roles"]), None)

    return templates.TemplateResponse(
        request,
        "delete_cluster.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "exec_mode": settings.ceph_exec_mode,
            "configured_nodes": nodes,
            "osd_nodes": osd_nodes,
            "has_configured_cluster": bool(nodes),
            "confirm_text": first_mon_ip,
            "pending_action": pending_action,
            "last_action": last_action,
            "progress": progress,
        },
    )


@router.post("/delete-cluster/propose")
async def propose_delete(request: Request, user: str = Depends(require_login)):
    body = await request.json()

    nodes = _normalize_configured_nodes()
    if not nodes:
        raise HTTPException(status_code=400, detail="Chưa có cụm nào được cấu hình để xoá")

    wipe_osd_disks = bool(body.get("wipe_osd_disks"))
    osd_disks_raw = body.get("osd_disks")
    if wipe_osd_disks and not isinstance(osd_disks_raw, dict):
        raise HTTPException(status_code=400, detail="Dữ liệu đĩa OSD không hợp lệ")

    if wipe_osd_disks:
        for node in nodes:
            if "osd" not in node["roles"]:
                continue
            disk = str((osd_disks_raw or {}).get(node["ip"], "")).strip()
            if not _OSD_DISK_RE.match(disk):
                raise HTTPException(
                    status_code=400, detail=f"Đĩa OSD không hợp lệ cho node {node['ip']} (vd /dev/vdc)"
                )
            node["osd_disk"] = disk

    exec_mode = settings.ceph_exec_mode
    action_id = DELETE_CEPHADM_ACTION_ID if exec_mode == "cephadm" else DELETE_MANUAL_ACTION_ID

    action_params = {"nodes": nodes, "wipe_osd_disks": wipe_osd_disks}

    target_nodes = [n["ip"] for n in nodes]
    try:
        preview_command = executor_commands.get_command(action_id, target_nodes[0], action_params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")

    with db.SessionLocal() as session:
        existing = (
            session.query(Action)
            .filter(Action.action_id.in_(CLUSTER_DELETE_ACTION_IDS))
            .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="Đã có một đề xuất xoá cụm đang chờ duyệt hoặc đã duyệt — không thể tạo thêm.",
            )

        incident = Incident(
            ceph_code=CLUSTER_DELETE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất xoá cụm Ceph đang cấu hình bởi {user} — {len(nodes)} node",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=gate.classify_action(action_id).value,  # always RISKY (AD-5)
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=_delete_plan_text(exec_mode, nodes, wipe_osd_disks),
            target_nodes=json.dumps(target_nodes),
            action_params=json.dumps(action_params),
            proposed_command=preview_command,
        )
        session.add(action)
        session.flush()

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=user,
        )
        session.commit()
        action_pk = action.id

    return JSONResponse({"action_id": action_pk}, status_code=201)


@router.get("/delete-cluster/progress")
async def delete_cluster_progress(user: str = Depends(require_login)):
    with db.SessionLocal() as session:
        action = (
            session.query(Action)
            .filter(Action.action_id.in_(CLUSTER_DELETE_ACTION_IDS))
            .order_by(Action.created_at.desc())
            .first()
        )
        if action is None:
            return JSONResponse({"status": None, "progress": []})
        try:
            progress = json.loads(action.execution_progress) if action.execution_progress else []
        except (TypeError, ValueError):
            progress = []
        return JSONResponse({"status": action.status, "progress": progress})
