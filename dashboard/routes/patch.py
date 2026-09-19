import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config.settings import settings
from dashboard.cluster_scope import cluster_selection, require_default_cluster
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import audit, db
from shared.cluster_nodes import configured_nodes, patch_build_node
from shared.models import Action, ActionStatus, Incident, IncidentStatus, PatchDocument
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# Synthetic Incident.ceph_code for this feature — same trick
# dashboard/routes/upgrade.py uses (CLUSTER_UPGRADE_CEPH_CODE): AuditEntry.
# incident_id is a required FK (AD-7), and this feature has no real detected
# Incident behind it, only an operator explicitly proposing a patch build/
# install.
CLUSTER_PATCH_CEPH_CODE = "CLUSTER_PATCH"

PATCH_BUILD_ACTION_ID = "patch_build_and_stage"
PATCH_INSTALL_ACTION_ID = "patch_install"
PATCH_ACTION_IDS = frozenset({PATCH_BUILD_ACTION_ID, PATCH_INSTALL_ACTION_ID})

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)

ALLOWED_PATCH_EXTENSIONS = (".patch", ".diff")
# Same cap as upgrade.py's ALLOWED_PROCEDURE_EXTENSIONS upload — generous for
# a source patch, small enough to embed as a single base64 payload in one
# SSH command (see worker/executor/commands.py::_patch_build_and_stage_command's
# docstring for why that trick only works at this size, unlike the built
# .rpm artifacts later in the pipeline).
MAX_PATCH_FILE_BYTES = 2 * 1024 * 1024  # 2 MB

_BUILD_PLAN_TEMPLATE = """\
Lệnh sẽ gửi tới build server ({build_node}):
1. Ghi nội dung patch đã upload ({filename}) vào `{source_dir}/ceph-aiops-current.patch`.
2. `git apply` patch đó vào `{source_dir}` (kiểm tra `git apply --check` trước, dừng lại nếu
   patch không áp được — KHÔNG tự sửa conflict).
3. Chạy lệnh build đã cấu hình sẵn (mục Cài đặt): `{build_command}`.
4. Build server tự `scp` toàn bộ file `.rpm` sinh ra ở `{output_dir}` sang thư mục
   `{staging_dir}` trên TỪNG node Ceph đã cấu hình: {target_nodes}.

QUAN TRỌNG: bước 4 cần build server tự kết nối SSH sang từng node Ceph — build server phải
đã có sẵn MỘT BẢN COPY của cùng private key SSH đang dùng cho Watcher/Worker (đường dẫn
`{ssh_key_path}`), vì các node Ceph đã tin tưởng public key tương ứng rồi. Đây là bước cấu
hình thủ công một lần, không phải việc app tự làm được.

Bước này CHƯA đụng gì tới Ceph đang chạy — chỉ build & đặt file lên node, chưa cài đặt.
trên (nếu đang bật, lệnh sẽ không được gửi và đề xuất quay lại trạng thái chờ duyệt)."""

_INSTALL_PLAN_TEMPLATE = """\
Lệnh sẽ gửi tới TỪNG node Ceph đã cấu hình ({target_nodes}), LẦN LƯỢT từng node một:
1. Cài các file `.rpm` đã được build&copy sẵn ở `{staging_dir}` (`rpm`/`yum localinstall`).
2. Khởi động lại toàn bộ daemon Ceph đang chạy trên node đó (dò qua `systemctl`).

QUAN TRỌNG — khác với `ceph orch upgrade`: KHÔNG có orchestrator tự kiểm tra sức khoẻ cụm
giữa các bước. Nếu một node gặp lỗi, hệ thống VẪN thử tiếp node kế tiếp trong danh sách
trong suốt quá trình, đặc biệt với cụm nhiều node.

đầu tiên (nếu đang bật, đề xuất quay lại trạng thái chờ duyệt, chưa node nào bị đụng tới)."""


def _get_patch_document(session) -> PatchDocument | None:
    return session.get(PatchDocument, 1)


def _decode_action_params(action: Action | None) -> dict:
    if action is None or not action.action_params:
        return {}
    try:
        value = json.loads(action.action_params)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _relative_patch_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    seconds = max(0, int((datetime.utcnow() - value).total_seconds()))
    if seconds < 60:
        return "vừa xong"
    if seconds < 3600:
        return f"{seconds // 60} phút trước"
    if seconds < 86400:
        return f"{seconds // 3600} giờ trước"
    return f"{seconds // 86400} ngày trước"


def _patch_status_display(status: str | None) -> tuple[str, str]:
    labels = {
        ActionStatus.EXECUTED.value: ("Thành công", "is-success"),
        ActionStatus.FAILED.value: ("Lỗi", "is-failed"),
        ActionStatus.REJECTED.value: ("Đã từ chối", "is-failed"),
        ActionStatus.PENDING_APPROVAL.value: ("Chờ duyệt", "is-pending"),
        ActionStatus.APPROVED.value: ("Đã duyệt", "is-pending"),
        ActionStatus.EXECUTING.value: ("Đang chạy", "is-pending"),
    }
    return labels.get(status or "", ("Chưa có", "is-pending"))


def _patch_history(session, limit: int = 10) -> list[dict]:
    builds = (
        session.query(Action)
        .filter(Action.action_id == PATCH_BUILD_ACTION_ID)
        .order_by(Action.created_at.desc())
        .limit(limit)
        .all()
    )
    installs = (
        session.query(Action)
        .filter(Action.action_id == PATCH_INSTALL_ACTION_ID)
        .order_by(Action.created_at.desc())
        .limit(max(20, limit * 2))
        .all()
    )
    install_by_build = {}
    for install in installs:
        params = _decode_action_params(install)
        build_id = params.get("patch_build_action_id")
        if build_id and build_id not in install_by_build:
            install_by_build[build_id] = install

    history = []
    for build in builds:
        params = _decode_action_params(build)
        incident = session.get(Incident, build.incident_id)
        actor = params.get("patch_uploaded_by") or "admin"
        if incident and incident.log_excerpt:
            actor_match = re.search(r"\bbởi\s+(.+)$", incident.log_excerpt)
            if actor_match:
                actor = actor_match.group(1).strip()
        install = install_by_build.get(build.id)
        build_label, build_class = _patch_status_display(build.status)
        install_label, install_class = _patch_status_display(install.status if install else None)
        history.append({
            "created_at": build.created_at.isoformat() if build.created_at else "",
            "relative_time": _relative_patch_time(build.created_at),
            "filename": params.get("patch_filename") or "patch không xác định",
            "build_label": build_label,
            "build_class": build_class,
            "install_label": install_label if install else "Chưa áp",
            "install_class": install_class,
            "actor": actor,
            "install_created_at": install.created_at.isoformat() if install and install.created_at else "",
        })
    return history


def _progress_summary(action: Action | None) -> tuple[int, str, str]:
    progress = _action_progress(action)
    if action is None:
        return 0, "Upload patch để bắt đầu.", ""
    if action.status == ActionStatus.EXECUTED.value:
        return 100, "Build & Copy hoàn tất.", ""
    if action.status == ActionStatus.FAILED.value:
        return 100, "Build & Copy thất bại.", ""
    if action.status in _IN_FLIGHT_ACTION_STATUSES:
        percent = 12
        if progress:
            done = sum(
                item.get("status") == "done"
                for item in progress
                if isinstance(item, dict)
            )
            percent = max(12, min(90, round(100 * done / len(progress))))
        return percent, "Đang chờ duyệt hoặc worker bắt đầu…", ""
    return 0, "Chưa chạy Build & Copy.", ""


def _action_progress(action: Action | None) -> list[dict]:
    if action is None or not action.execution_progress:
        return []
    try:
        value = json.loads(action.execution_progress)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _latest_patch_action(session, action_id: str | None = None) -> tuple[Action | None, Incident | None]:
    query = session.query(Action).filter(Action.action_id.in_(PATCH_ACTION_IDS))
    if action_id is not None:
        query = query.filter(Action.action_id == action_id)
    action = query.order_by(Action.created_at.desc()).first()
    if action is None:
        return None, None
    return action, session.get(Incident, action.incident_id)


def _reject_duplicate_patch_proposal(session) -> None:
    """Only one patch-pipeline Action (build&stage OR install) may be
    in-flight at a time — mirrors upgrade.py's _reject_duplicate_proposal
    exactly."""
    existing, _ = _latest_patch_action(session)
    if existing is not None and existing.status in _IN_FLIGHT_ACTION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Đã có một đề xuất patch đang chờ duyệt hoặc đã duyệt — không thể tạo thêm.",
        )


def is_patch_install_pending_or_approved(session) -> bool:
    """True while a patch_install Action specifically is PENDING_APPROVAL or
    APPROVED — used by dashboard/routes/actions.py::approve_action as the
    symmetric counterpart to upgrade.py's is_cluster_upgrade_pending_or_approved
    (a live patch install is just as disruptive to the cluster as a cluster
    upgrade, so approving one must be blocked while the other is in-flight,
    both ways). patch_build_and_stage is deliberately NOT included here — it
    never touches the live Ceph cluster, only the separate build server."""
    return (
        session.query(Action)
        .filter(Action.action_id == PATCH_INSTALL_ACTION_ID)
        .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
        .first()
        is not None
    )


def _safe_command_preview(action_id: str, host: str, params: dict) -> str:
    """Same best-effort-preview posture as upgrade.py's helper of the same
    name — illustrative only, the real execution always re-resolves the
    command fresh at approval time."""
    try:
        command = executor_commands.get_command(action_id, host, params)
        return f"[Xem trước trên {host} — lệnh thực tế được tính lại khi thực thi]\n{command}"
    except ExecutorError as exc:
        return f"[Không xem trước được lệnh trên {host}: {exc} — lệnh thực tế vẫn sẽ được tính khi thực thi]"


def _patch_context(request: Request, user: str) -> dict:
    clusters, cluster = cluster_selection(request)
    with db.SessionLocal() as session:
        document = _get_patch_document(session)
        build_action, build_incident = _latest_patch_action(session, PATCH_BUILD_ACTION_ID)
        install_action, install_incident = _latest_patch_action(session, PATCH_INSTALL_ACTION_ID)
        latest_action, latest_incident = _latest_patch_action(session)

        any_in_flight = latest_action is not None and latest_action.status in _IN_FLIGHT_ACTION_STATUSES
        can_propose_build = document is not None and not any_in_flight
        can_propose_install = (
            build_action is not None
            and build_action.status == ActionStatus.EXECUTED.value
            and not any_in_flight
        )
        progress = _action_progress(build_action)
        progress_percent, progress_phase, progress_elapsed = _progress_summary(build_action)

        # Sidebar tab (2026-07-24) — lands on whichever step has an
        # in-flight proposal (so its approve/reject buttons are immediately
        # visible), else defaults to "upload" (the natural starting point).
        if any_in_flight and latest_action.action_id == PATCH_INSTALL_ACTION_ID:
            active_tab = "install"
        elif any_in_flight and latest_action.action_id == PATCH_BUILD_ACTION_ID:
            active_tab = "build"
        else:
            active_tab = "upload"

        history_limit = 50 if request.query_params.get("history") == "all" else 10
        patch_history = _patch_history(session, history_limit)
        return {
            "user": user,
            "clusters": clusters,
            "selected_cluster": cluster,
            "patch_history": patch_history,
            "progress": progress,
            "is_admin": auth.is_admin_user(user),
            "active_tab": active_tab,
            "patch_document": document,
            "build_action": build_action,
            "build_incident": build_incident,
            "install_action": install_action,
            "install_incident": install_incident,
            "pending_action": latest_action if any_in_flight else None,
            "pending_incident": latest_incident if any_in_flight else None,
            "can_propose_build": can_propose_build,
            "can_propose_install": can_propose_install,
            "build_node": settings.ceph_patch_build_node,
            "progress_percent": progress_percent,
            "progress_phase": progress_phase,
            "progress_elapsed": progress_elapsed,
        }


@router.get("/patch", response_class=HTMLResponse)
async def patch_page(request: Request, user: str = Depends(require_login)):
    require_default_cluster(request, "Patch Ceph")
    response = templates.TemplateResponse(request, "patch.html", _patch_context(request, user))
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@router.get("/patch/progress")
async def patch_progress(request: Request, user: str = Depends(require_login)):
    require_default_cluster(request, "Patch Ceph")
    with db.SessionLocal() as session:
        action, _ = _latest_patch_action(session, PATCH_BUILD_ACTION_ID)
        return JSONResponse({
            "action_id": action.id if action else None,
            "status": action.status if action else None,
            "progress": _action_progress(action),
            "updated_at": action.created_at.isoformat() if action and action.created_at else None,
        })


@router.post("/patch/upload")
async def upload_patch(
    request: Request, file: UploadFile = File(...), user: str = Depends(require_login)
):
    require_default_cluster(request, "Patch Ceph")
    filename = file.filename or "upload.patch"
    if not filename.lower().endswith(ALLOWED_PATCH_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail=f"Chỉ hỗ trợ file patch ({', '.join(ALLOWED_PATCH_EXTENSIONS)}).",
        )
    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_PATCH_FILE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File quá lớn (tối đa {MAX_PATCH_FILE_BYTES // (1024 * 1024)}MB).",
        )
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400, detail="Không đọc được nội dung file — cần file văn bản UTF-8 thuần."
        )
    if not content.strip():
        raise HTTPException(status_code=400, detail="File rỗng.")

    with db.SessionLocal() as session:
        doc = _get_patch_document(session)
        if doc is None:
            doc = PatchDocument(id=1)
            session.add(doc)
        doc.filename = filename
        doc.content = content
        doc.uploaded_by = user
        doc.uploaded_at = datetime.utcnow()
        session.commit()

    return RedirectResponse(url="/patch", status_code=303)


@router.post("/patch/propose-build")
async def propose_patch_build(request: Request, user: str = Depends(require_login)):
    require_default_cluster(request, "Patch Ceph")
    build_node = patch_build_node()
    if build_node is None:
        raise HTTPException(status_code=400, detail="Chưa cấu hình build server (xem trang Cài đặt)")

    with db.SessionLocal() as session:
        _reject_duplicate_patch_proposal(session)

        document = _get_patch_document(session)
        if document is None:
            raise HTTPException(status_code=400, detail="Chưa upload patch nào")

        action_params = {
            "patch_content": document.content,
            "patch_filename": document.filename,
            "patch_uploaded_by": document.uploaded_by,
        }
        target_nodes = [build_node]
        preview_command = _safe_command_preview(PATCH_BUILD_ACTION_ID, build_node, action_params)

        incident = Incident(
            ceph_code=CLUSTER_PATCH_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất build & copy patch {document.filename!r} bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()

        action = Action(
            incident_id=incident.id,
            action_id=PATCH_BUILD_ACTION_ID,
            classification=gate.classify_action(PATCH_BUILD_ACTION_ID).value,  # always RISKY
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=_BUILD_PLAN_TEMPLATE.format(
                build_node=build_node,
                filename=document.filename,
                source_dir=settings.ceph_patch_source_dir,
                build_command=settings.ceph_patch_build_command,
                output_dir=settings.ceph_patch_output_dir,
                staging_dir=settings.ceph_patch_node_staging_dir,
                target_nodes=", ".join(n["host"] for n in configured_nodes()) or "(chưa cấu hình node nào)",
                ssh_key_path=settings.ssh_key_path,
            ),
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

    return RedirectResponse(url="/patch", status_code=303)


@router.post("/patch/propose-install")
async def propose_patch_install(request: Request, user: str = Depends(require_login)):
    require_default_cluster(request, "Patch Ceph")
    with db.SessionLocal() as session:
        _reject_duplicate_patch_proposal(session)

        build_action, _ = _latest_patch_action(session, PATCH_BUILD_ACTION_ID)
        if build_action is None or build_action.status != ActionStatus.EXECUTED.value:
            raise HTTPException(
                status_code=409,
                detail="Chưa có lần build & copy nào thành công — đề xuất Build & Copy trước.",
            )

        target_nodes = [n["host"] for n in configured_nodes()]
        if not target_nodes:
            raise HTTPException(status_code=400, detail="Chưa cấu hình node Ceph nào (xem trang Cài đặt)")

        action_params: dict = {
            "patch_build_action_id": build_action.id,
            "patch_filename": _decode_action_params(build_action).get("patch_filename"),
        }
        preview_command = _safe_command_preview(PATCH_INSTALL_ACTION_ID, target_nodes[0], action_params)

        incident = Incident(
            ceph_code=CLUSTER_PATCH_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất cài đặt patch đã build lên {len(target_nodes)} node bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()

        action = Action(
            incident_id=incident.id,
            action_id=PATCH_INSTALL_ACTION_ID,
            classification=gate.classify_action(PATCH_INSTALL_ACTION_ID).value,  # always RISKY
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=_INSTALL_PLAN_TEMPLATE.format(
                target_nodes=", ".join(target_nodes),
                staging_dir=settings.ceph_patch_node_staging_dir,
            ),
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

    return RedirectResponse(url="/patch", status_code=303)
