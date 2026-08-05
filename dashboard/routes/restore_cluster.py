"""Khôi phục cụm sau thảm họa (Story 9.7, `restore_cluster_from_backup`).

Dispatches through `worker/executor/cluster_deploy.py::run()`, family
`cluster_deploy_action_ids:` — same propose/approve/poll-progress shape as
`dashboard/routes/deploy_cluster.py` (this route's own node-table form is
an intentionally trimmed copy of that one: no install-method choice, since
`restore_cluster_from_backup` always reuses `deploy_cluster_ceph_deploy`'s
phase list, see `worker/executor/cluster_deploy.py`'s own comment on that
action_id's `_PHASES_BY_ACTION_ID` entry).
"""

from __future__ import annotations

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
from dashboard.vntime import format_vn_clock
from shared import audit, db
from shared.ceph_releases import codenames_oldest_first, versions_by_codename
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate
from worker.policy.gate import VALID_CLUSTER_DEPLOY_ACTION_IDS

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# Synthetic Incident.ceph_code — same trick every other cluster-lifecycle
# route in this project uses (deploy_cluster.py/delete_cluster.py/
# convert_cluster.py): AuditEntry.incident_id is a required FK, and this
# feature has no real detected Incident behind it, only an operator
# explicitly filling in a node table after a total cluster loss.
RESTORE_CLUSTER_CEPH_CODE = "RESTORE_CLUSTER_FROM_BACKUP"
RESTORE_ACTION_ID = "restore_cluster_from_backup"

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_VALID_ROLES = ("mon", "mgr", "osd", "mds", "rgw")


def _is_valid_ip(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or not (0 <= int(part) <= 255):
            return False
        if len(part) > 1 and part[0] == "0":
            return False
    return True


def _validate_nodes(nodes_raw) -> tuple[list[dict], str | None]:
    """Same own-copy pattern `dashboard/routes/convert_cluster.py`'s
    `_normalize_configured_nodes` docstring already established relative to
    `delete_cluster.py` — this is `deploy_cluster.py::_validate_nodes`'s
    logic, duplicated here rather than imported (that function is
    module-private there, and every cluster-lifecycle route in this project
    already keeps its own copy of this exact shape of helper)."""
    if not isinstance(nodes_raw, list) or not nodes_raw:
        return [], "Cần ít nhất 1 node"

    normalized: list[dict] = []
    seen_ips: set[str] = set()
    for entry in nodes_raw:
        if not isinstance(entry, dict):
            return [], "Dữ liệu node không hợp lệ"
        ip = str(entry.get("ip", "")).strip()
        if not _is_valid_ip(ip):
            return [], f"IP không hợp lệ: {ip!r}"
        if ip in seen_ips:
            return [], f"IP bị trùng: {ip}"
        seen_ips.add(ip)

        roles_raw = entry.get("roles") or []
        if not isinstance(roles_raw, list) or any(r not in _VALID_ROLES for r in roles_raw):
            return [], f"Vai trò không hợp lệ cho node {ip}"
        roles = [r for r in _VALID_ROLES if r in roles_raw]

        osd_disk = str(entry.get("osd_disk", "")).strip() if "osd" in roles else None
        normalized.append({"ip": ip, "roles": roles, "osd_disk": osd_disk})

    mon_count = sum(1 for n in normalized if "mon" in n["roles"])
    mgr_count = sum(1 for n in normalized if "mgr" in n["roles"])
    osd_count = sum(1 for n in normalized if "osd" in n["roles"])
    if mon_count < 1:
        return [], "Cần ít nhất 1 node MON"
    if mgr_count < 1:
        return [], "Cần ít nhất 1 node MGR"
    if osd_count < 1:
        return [], "Cần ít nhất 1 node OSD"

    for n in normalized:
        if "osd" in n["roles"] and not n["osd_disk"]:
            return [], f"Chưa điền đĩa OSD cho node {n['ip']}"

    return normalized, None


def _step_clock(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return format_vn_clock(datetime.fromisoformat(value))
    except ValueError:
        return None


def _with_step_display_times(progress: list) -> list:
    """Same fix, same reason as `deploy_cluster.py`/`convert_cluster.py`'s
    identical helper — this page's progress comes from the SAME
    `worker/executor/cluster_deploy.py::run()` every other cluster-lifecycle
    action already goes through."""
    for step in progress:
        if isinstance(step, dict):
            step["started_at_display"] = _step_clock(step.get("started_at"))
            step["finished_at_display"] = _step_clock(step.get("finished_at"))
    return progress


def _restore_plan_text(nodes: list[dict], version: str) -> str:
    mon = [n["ip"] for n in nodes if "mon" in n["roles"]]
    mgr = [n["ip"] for n in nodes if "mgr" in n["roles"]]
    osd = [n["ip"] for n in nodes if "osd" in n["roles"]]
    # RGW is OPTIONAL (see worker/executor/cluster_deploy.py::
    # _phase_ceph_deploy_rgw_create's own docstring) — matches deploy_cluster.py's
    # own _deploy_plan_text posture of always showing this line, even blank.
    rgw = [n["ip"] for n in nodes if "rgw" in n["roles"]]
    node_summary = (
        f"MON: {', '.join(mon)}\nMGR: {', '.join(mgr)}\nOSD: {', '.join(osd)}\n"
        f"RGW: {', '.join(rgw) if rgw else '(không có)'}"
    )

    return (
        f"Khôi phục toàn bộ cụm Ceph {version} sau thảm họa, TRÊN NODE MỚI:\n{node_summary}\n\n"
        f"Các bước sẽ thực hiện, LẦN LƯỢT, sau khi Duyệt:\n"
        f"1-11. Dựng cụm trống bằng phương thức ceph-deploy (ssh_check → dependencies → repo → "
        f"packages → mon_init → wait_quorum → mon_security → mgr_create → osd_create → rgw_create "
        f"[nếu có node RGW] → verify) — y hệt trang Dựng cụm, phương thức ceph-deploy.\n"
        f"12. Khôi phục auth keys + CRUSH map từ bản backup metadata gần nhất; cố gắng khôi phục "
        f"monmap (best-effort — có thể không thành công do khác fsid, xem docs/runbook-dr.md).\n"
        f"13. Khôi phục từng RBD image đã cấu hình trong tracked_images (full export + toàn bộ "
        f"chain export-diff, theo đúng thứ tự tạo).\n"
        f"14. Đối chiếu kích thước từng image sau khôi phục với bản backup full gốc.\n\n"
        f"CẢNH BÁO: đây là thao tác khôi phục sau thảm họa — chỉ dùng khi cụm cũ đã sập hoàn toàn. "
        f"Xem đầy đủ trước khi bắt đầu tại docs/runbook-dr.md."
    )


@router.get("/restore-cluster", response_class=HTMLResponse)
async def restore_cluster_page(request: Request, user: str = Depends(require_login)):
    try:
        with db.SessionLocal() as session:
            last_action = (
                session.query(Action)
                .filter(Action.action_id == RESTORE_ACTION_ID)
                .order_by(Action.created_at.desc())
                .first()
            )
    except SQLAlchemyError:
        logger.exception("restore_cluster_page: failed to query DB")
        raise HTTPException(
            status_code=503,
            detail="Không kết nối được database — đã chạy `alembic upgrade head` chưa?",
        )

    pending_action = (
        last_action if last_action is not None and last_action.status in _IN_FLIGHT_ACTION_STATUSES else None
    )

    last_action_params: dict = {}
    if last_action is not None and last_action.action_params:
        try:
            last_action_params = json.loads(last_action.action_params) or {}
        except (TypeError, ValueError):
            last_action_params = {}

    progress: list = []
    if last_action is not None and last_action.execution_progress:
        try:
            progress = json.loads(last_action.execution_progress) or []
        except (TypeError, ValueError):
            progress = []
    progress = _with_step_display_times(progress)

    return templates.TemplateResponse(
        request,
        "restore_cluster.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "codenames": codenames_oldest_first(),
            "versions_by_codename": versions_by_codename(),
            "default_ssh_user": settings.ssh_user,
            "default_ssh_key_path": settings.ssh_key_path,
            "pending_action": pending_action,
            "last_action": last_action,
            "last_action_params": last_action_params,
            "progress": progress,
        },
    )


@router.post("/restore-cluster/propose")
async def propose_restore(request: Request, user: str = Depends(require_login)):
    body = await request.json()

    version = str(body.get("version", "")).strip()
    if not _VERSION_RE.match(version):
        raise HTTPException(
            status_code=400, detail="Phiên bản không hợp lệ (định dạng x.y.z, vd 18.2.8)"
        )

    nodes, nodes_error = _validate_nodes(body.get("nodes"))
    if nodes_error:
        raise HTTPException(status_code=400, detail=nodes_error)

    public_network = str(body.get("public_network", "")).strip()
    cluster_network = str(body.get("cluster_network", "")).strip() or public_network

    try:
        osd_pool_default_size = int(body.get("osd_pool_default_size", 3))
        osd_pool_default_min_size = int(body.get("osd_pool_default_min_size", 2))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="osd_pool_default_size/osd_pool_default_min_size phải là số nguyên"
        )

    # Same action_params shape as deploy_cluster_ceph_deploy — restore_cluster_from_backup
    # reuses that method's ENTIRE phase list unchanged (worker/executor/cluster_deploy.py).
    action_params = {
        "version": version,
        "method": "ceph-deploy",
        "nodes": nodes,
        "public_network": public_network,
        "cluster_network": cluster_network,
        "osd_pool_default_size": osd_pool_default_size,
        "osd_pool_default_min_size": osd_pool_default_min_size,
    }

    target_nodes = [n["ip"] for n in nodes]
    try:
        preview_command = executor_commands.get_command(RESTORE_ACTION_ID, target_nodes[0], action_params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")

    with db.SessionLocal() as session:
        # Same mutual-exclusion posture as convert_cluster.py's own propose
        # route — blocks against ANY in-flight dựng/xoá/chuyển đổi/khôi phục
        # cụm, not just another restore.
        existing = (
            session.query(Action)
            .filter(Action.action_id.in_(VALID_CLUSTER_DEPLOY_ACTION_IDS))
            .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Đã có một đề xuất dựng/xoá/chuyển đổi/khôi phục cụm đang chờ duyệt hoặc đã "
                    "duyệt — không thể tạo thêm."
                ),
            )

        incident = Incident(
            ceph_code=RESTORE_CLUSTER_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=(
                f"Đề xuất khôi phục cụm Ceph sau thảm họa ({version}) bởi {user} — {len(nodes)} node"
            ),
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=RESTORE_ACTION_ID,
            classification=gate.classify_action(RESTORE_ACTION_ID).value,  # always RISKY (AD-5)
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=_restore_plan_text(nodes, version),
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


@router.get("/restore-cluster/progress")
async def restore_cluster_progress(user: str = Depends(require_login)):
    with db.SessionLocal() as session:
        action = (
            session.query(Action)
            .filter(Action.action_id == RESTORE_ACTION_ID)
            .order_by(Action.created_at.desc())
            .first()
        )
        if action is None:
            return JSONResponse({"status": None, "progress": []})
        try:
            progress = json.loads(action.execution_progress) if action.execution_progress else []
        except (TypeError, ValueError):
            progress = []
        progress = _with_step_display_times(progress)
        return JSONResponse({"status": action.status, "progress": progress})
