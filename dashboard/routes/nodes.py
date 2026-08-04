import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import audit, db
from shared.cluster_nodes import configured_nodes as _configured_nodes
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from watcher.ceph_client import CephQueryError, list_osds
from watcher.node_metrics import NodeMetricsError, collect_node_metrics
from watcher.rgw_log import RgwLogError, fetch_rgw_log
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# 2026-08-04: BlueStore per-pool omap quick-fix — see worker/executor/
# commands.py::_bluestore_omap_quick_fix_command for the full background.
BLUESTORE_QUICK_FIX_ACTION_ID = "bluestore_omap_quick_fix"
_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)
# Same synthetic-Incident trick every other propose route in this codebase
# uses (AuditEntry.incident_id is a required FK, and this has no real
# detected Incident behind it — just an operator explicitly picking an
# osd_id and clicking "Đề xuất").
_BLUESTORE_QUICK_FIX_CEPH_CODE = "BLUESTORE_OMAP_QUICK_FIX"


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request, user: str = Depends(require_login)):
    nodes = _configured_nodes()
    requested_host = request.query_params.get("host")
    known_hosts = {n["host"] for n in nodes}
    if requested_host and requested_host not in known_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    # No default node — landing on /nodes with no ?host= shows the empty
    # "chọn một node" state, not whichever node happened to be configured
    # first. A node is only selected when the operator actually picks one
    # (or deep-links with ?host=).
    selected_host = requested_host
    selected_node = next((n for n in nodes if n["host"] == selected_host), None)

    osds: list[dict] = []
    osds_error: str | None = None
    try:
        osds = list_osds()
    except CephQueryError as exc:
        osds_error = str(exc)

    return templates.TemplateResponse(
        request,
        "nodes.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "nodes": nodes,
            "selected_host": selected_host,
            "selected_node": selected_node,
            "osds": osds,
            "osds_error": osds_error,
            "osd_nodes": [n for n in nodes if "OSD" in n["roles"]],
        },
    )


@router.get("/api/nodes/{host}/metrics")
async def node_metrics_api(host: str, user: str = Depends(require_login)):
    # `host` is attacker-reachable input feeding straight into an SSH
    # connect() — without this whitelist, any logged-in user could make the
    # Dashboard open an SSH session to an arbitrary address of their
    # choosing using the Watcher keypair (SSRF-via-SSH). Only nodes the
    # operator already configured for this cluster are queryable.
    allowed_hosts = {n["host"] for n in _configured_nodes()}
    if host not in allowed_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    try:
        metrics = collect_node_metrics(host)
    except NodeMetricsError as exc:
        logger.warning("node_metrics_api: %s", exc)
        raise HTTPException(status_code=502, detail=f"Không lấy được metrics từ node: {exc}")
    return {"host": host, **metrics}


@router.get("/api/nodes/{host}/rgw-log")
async def rgw_log_api(host: str, filter: str = "", user: str = Depends(require_login)):
    """Backs the Nodes page's "Log RGW" panel — tails this host's radosgw
    daemon log (watcher/rgw_log.py), optionally grepped server-side by
    `filter`. Live-only, like node_metrics_api above: nothing here is
    persisted — this is a monitoring view, not a discrete auditable action.
    """
    # Restricted to hosts actually carrying the RGW role, not just any
    # configured node — same SSRF-via-SSH whitelist posture as
    # node_metrics_api, narrowed further here since a non-RGW host has no
    # radosgw container/daemon to read from anyway.
    rgw_hosts = {n["host"] for n in _configured_nodes() if "RGW" in n["roles"]}
    if host not in rgw_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách RGW đã cấu hình")
    try:
        output = fetch_rgw_log(host, filter)
    except RgwLogError as exc:
        logger.warning("rgw_log_api: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    lines = output.splitlines()
    return {"host": host, "filter": filter, "lines": lines}


@router.post("/nodes/bluestore-quick-fix/propose")
async def propose_bluestore_quick_fix(request: Request, user: str = Depends(require_login)):
    """Proposes bluestore_omap_quick_fix for ONE operator-picked osd_id —
    always RISKY (worker/policy/action_policy.yaml), needs a separate
    Dashboard approval before Worker ever touches anything, same as every
    other propose route in this codebase. See worker/executor/commands.py::
    _bluestore_omap_quick_fix_command's own comment for why this action_id
    is proposed from here (a real osd_id picked from `ceph osd tree`) and
    not Chat-with-AI free text.
    """
    body = await request.json()
    osd_id = body.get("osd_id")
    host = str(body.get("host", "")).strip()

    if not isinstance(osd_id, int) or isinstance(osd_id, bool):
        raise HTTPException(status_code=400, detail="osd_id phải là số nguyên")

    # Same SSRF-via-SSH whitelist posture as node_metrics_api/rgw_log_api
    # above — narrowed to OSD-role hosts, since that's the only kind of
    # node this command is ever meant to run on.
    osd_hosts = {n["host"] for n in _configured_nodes() if "OSD" in n["roles"]}
    if host not in osd_hosts:
        raise HTTPException(
            status_code=400, detail=f"{host} không nằm trong danh sách node OSD đã cấu hình"
        )

    action_params = {"osd_id": osd_id}
    try:
        preview_command = executor_commands.get_command(
            BLUESTORE_QUICK_FIX_ACTION_ID, host, action_params
        )
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")

    with db.SessionLocal() as session:
        existing = (
            session.query(Action)
            .filter(Action.action_id == BLUESTORE_QUICK_FIX_ACTION_ID)
            .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
            .filter(Action.action_params == json.dumps(action_params))
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Đã có đề xuất quick-fix cho osd.{osd_id} đang chờ duyệt hoặc đã duyệt.",
            )

        incident = Incident(
            ceph_code=_BLUESTORE_QUICK_FIX_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=(
                f"Đề xuất chạy ceph-bluestore-tool quick-fix cho osd.{osd_id} trên {host} bởi {user}"
            ),
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=BLUESTORE_QUICK_FIX_ACTION_ID,
            classification=gate.classify_action(BLUESTORE_QUICK_FIX_ACTION_ID).value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=(
                f"Dừng osd.{osd_id}, chạy `ceph-bluestore-tool quick-fix` để chuyển sang theo dõi "
                f"omap theo từng pool (sửa BLUESTORE_NO_PER_POOL_OMAP), rồi khởi động lại. Có rủi ro "
                f"đã biết với phiên bản ceph-bluestore-tool cũ (xem ghi chú trong code) — chỉ duyệt "
                f"khi cluster đang khoẻ mạnh và đã hiểu rõ rủi ro."
            ),
            target_nodes=json.dumps([host]),
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
