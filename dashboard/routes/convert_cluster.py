import asyncio
import json
import logging
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
from shared.cluster_nodes import configured_nodes
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from watcher import ceph_client
from watcher.ceph_client import CephQueryError
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate
from worker.policy.gate import VALID_CLUSTER_DEPLOY_ACTION_IDS

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# Synthetic Incident.ceph_code for this feature — same trick
# dashboard/routes/deploy_cluster.py/delete_cluster.py/upgrade.py/patch.py
# use: AuditEntry.incident_id is a required FK, and converting an already-
# monitored cluster's management style has no real detected Incident behind
# it, only an operator explicitly clicking "Đề xuất chuyển đổi".
CONVERT_CLUSTER_CEPH_CODE = "CONVERT_CLUSTER_TO_CEPHADM"
CONVERT_ACTION_ID = "convert_cluster_to_cephadm"

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)

_ROLE_UPPER_TO_LOWER = {"MON": "mon", "MGR": "mgr", "OSD": "osd", "RGW": "rgw"}


def _normalize_configured_nodes() -> list[dict]:
    """Same own-copy pattern as dashboard/routes/delete_cluster.py's
    identical helper — shared/cluster_nodes.py::configured_nodes() returns
    {"host": .., "roles": ["MON", "OSD"]} (uppercase, "host" key),
    normalized here to the lowercase {"ip": .., "roles": [...]} shape
    worker/executor/cluster_deploy.py's phase helpers expect."""
    nodes = []
    for n in configured_nodes():
        roles = [_ROLE_UPPER_TO_LOWER[r] for r in n["roles"] if r in _ROLE_UPPER_TO_LOWER]
        nodes.append({"ip": n["host"], "roles": roles})
    return nodes


def _step_clock(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return format_vn_clock(datetime.fromisoformat(value))
    except ValueError:
        return None


def _with_step_display_times(progress: list) -> list:
    """Same fix, same reason as dashboard/routes/deploy_cluster.py's
    identical helper — this page's progress comes from the SAME
    worker/executor/cluster_deploy.py::run() every other cluster-lifecycle
    action already goes through."""
    for step in progress:
        if isinstance(step, dict):
            step["started_at_display"] = _step_clock(step.get("started_at"))
            step["finished_at_display"] = _step_clock(step.get("finished_at"))
    return progress


def _convert_plan_text(nodes: list[dict], version: str) -> str:
    mon = [n["ip"] for n in nodes if "mon" in n["roles"]]
    mgr = [n["ip"] for n in nodes if "mgr" in n["roles"]]
    osd = [n["ip"] for n in nodes if "osd" in n["roles"]]
    rgw = [n["ip"] for n in nodes if "rgw" in n["roles"]]
    node_summary = f"MON: {', '.join(mon)}\nMGR: {', '.join(mgr)}\nOSD: {', '.join(osd)}"

    rgw_note = (
        f"\n\nLƯU Ý: node RGW ({', '.join(rgw)}) sẽ KHÔNG được chuyển đổi — tính năng này chỉ xử "
        f"lý MON/MGR/OSD, daemon RGW vẫn tiếp tục chạy dưới systemd như cũ sau khi xong."
        if rgw
        else ""
    )

    return (
        f"Chuyển đổi cụm Ceph {version} đang cấu hình (chạy systemd) sang cephadm, TẠI CHỖ, "
        f"KHÔNG xoá dữ liệu:\n{node_summary}\n\n"
        f"Các bước sẽ thực hiện, LẦN LƯỢT, sau khi Duyệt:\n"
        f"1. Kiểm tra kết nối SSH tới từng node.\n"
        f"2. Kiểm tra cụm không ở trạng thái HEALTH_ERR.\n"
        f"3. Cài đặt cephadm trên từng node.\n"
        f"4. Chuyển đổi (`cephadm adopt --style legacy`) từng MON, rồi từng MGR.\n"
        f"5. Bật cephadm orchestrator, phân phối khoá SSH của nó, đăng ký từng node.\n"
        f"6. Chuyển đổi từng OSD (không đụng tới dữ liệu OSD, chỉ đổi cách quản lý).\n"
        f"7. Kiểm tra `ceph -s` — dừng lại nếu HEALTH_ERR.\n\n"
        f"CẢNH BÁO: đây là quy trình `cephadm adopt` chính thức của Ceph nhưng CHƯA được kiểm "
        f"chứng trên một cụm systemd thật trong phiên làm việc này — nên thử trên cụm không quan "
        f"trọng trước. Không có chiều ngược lại (cephadm -> systemd) — Ceph không hỗ trợ chính "
        f"thức chiều đó."
        f"{rgw_note}"
    )


@router.get("/convert-cluster", response_class=HTMLResponse)
async def convert_cluster_page(request: Request, user: str = Depends(require_login)):
    try:
        with db.SessionLocal() as session:
            last_action = (
                session.query(Action)
                .filter(Action.action_id == CONVERT_ACTION_ID)
                .order_by(Action.created_at.desc())
                .first()
            )
    except SQLAlchemyError:
        logger.exception("convert_cluster_page: failed to query DB")
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
    progress = _with_step_display_times(progress)

    nodes = _normalize_configured_nodes()
    exec_mode = settings.ceph_exec_mode
    # Only a genuinely native-systemd cluster (CEPH_EXEC_MODE=none) is
    # eligible — "docker"/"podman" exec_mode clusters run daemons as
    # manually-run containers, a different layout `cephadm adopt --style
    # legacy` isn't designed for; "cephadm" is already converted.
    eligible = exec_mode == "none" and bool(nodes)
    first_mon_ip = next((n["ip"] for n in nodes if "mon" in n["roles"]), None)

    return templates.TemplateResponse(
        request,
        "convert_cluster.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "exec_mode": exec_mode,
            "configured_nodes": nodes,
            "eligible": eligible,
            "confirm_text": first_mon_ip,
            "pending_action": pending_action,
            "last_action": last_action,
            "progress": progress,
        },
    )


@router.post("/convert-cluster/propose")
async def propose_convert(request: Request, user: str = Depends(require_login)):
    nodes = _normalize_configured_nodes()
    if not nodes:
        raise HTTPException(status_code=400, detail="Chưa có cụm nào được cấu hình để chuyển đổi")

    if settings.ceph_exec_mode != "none":
        raise HTTPException(
            status_code=400,
            detail=(
                "Chỉ hỗ trợ chuyển đổi từ cụm đang chạy systemd thuần (CEPH_EXEC_MODE=none) — "
                f"cụm hiện tại đang ở exec_mode={settings.ceph_exec_mode!r}"
            ),
        )

    if not any("mon" in n["roles"] for n in nodes) or not any("mgr" in n["roles"] for n in nodes):
        raise HTTPException(status_code=400, detail="Cần cấu hình ít nhất 1 node MON và 1 node MGR")

    # Detect the CURRENTLY running version live — this is a conversion, not
    # an upgrade, so it must install a matching cephadm release, never one
    # the operator picks by hand (that could silently mismatch what's
    # actually running).
    try:
        versions = await asyncio.to_thread(ceph_client.summarize_cluster_versions)
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không lấy được phiên bản cụm hiện tại: {exc}")
    version = versions["current_version"]
    if not version:
        # 2026-07-28: a mixed cluster isn't automatically unsafe to
        # (re)propose against — a real, live-verified scenario is a
        # PARTIALLY-completed earlier conversion (mon/mgr already adopted
        # at whichever version cephadm picked, OSD not yet touched), which
        # is resumable (see cluster_deploy.py's _cephadm_managed_daemon_ids),
        # not a genuinely inconsistent cluster. MON is adopted FIRST in
        # this flow, so by the time anything else is mixed, MON's version
        # already IS the de facto target everything else needs to
        # converge to — trust it whenever mon itself agrees on exactly
        # one version. Only refuse outright when even MON disagrees with
        # itself (genuinely unclear which version is "the" cluster
        # version — not something this feature has ever seen live).
        mon_versions = versions["per_type"].get("mon") or []
        if len(mon_versions) == 1:
            version = mon_versions[0]
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Không xác định được một phiên bản Ceph duy nhất đang chạy trên toàn cụm "
                    "(cụm có thể đang chạy lẫn nhiều phiên bản) — không thể chuyển đổi an toàn."
                ),
            )

    action_params = {"nodes": nodes, "version": version}
    target_nodes = [n["ip"] for n in nodes]
    try:
        preview_command = executor_commands.get_command(CONVERT_ACTION_ID, target_nodes[0], action_params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")

    with db.SessionLocal() as session:
        # Broader in-flight check than delete_cluster.py's own (which only
        # self-blocks against another delete) — this specifically also
        # blocks while a deploy/delete is in flight, since converting a
        # cluster mid-deploy/mid-delete would be genuinely dangerous, not
        # just redundant. Does NOT (yet) block the reverse (a deploy/delete
        # proposed while a convert is in flight) — that asymmetry already
        # exists between deploy_cluster.py/delete_cluster.py today; fixing
        # it fully is a separate, pre-existing gap, not something this
        # feature introduces.
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
                    "Đã có một đề xuất dựng/xoá/chuyển đổi cụm đang chờ duyệt hoặc đã duyệt — "
                    "không thể tạo thêm."
                ),
            )

        incident = Incident(
            ceph_code=CONVERT_CLUSTER_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất chuyển đổi cụm Ceph đang cấu hình sang cephadm bởi {user} — {len(nodes)} node",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=CONVERT_ACTION_ID,
            classification=gate.classify_action(CONVERT_ACTION_ID).value,  # always RISKY (AD-5)
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=_convert_plan_text(nodes, version),
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


@router.get("/convert-cluster/progress")
async def convert_cluster_progress(user: str = Depends(require_login)):
    with db.SessionLocal() as session:
        action = (
            session.query(Action)
            .filter(Action.action_id == CONVERT_ACTION_ID)
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
