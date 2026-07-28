import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.incidents import OPEN_STATUSES
from dashboard.templating import make_templates
from shared import audit, db
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from watcher import ceph_client
from watcher.ceph_client import CephQueryError
from watcher.volume_monitor import ceph_code_for
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# Synthetic Incident.ceph_code for this feature — same trick
# dashboard/routes/deploy_cluster.py/delete_cluster.py/upgrade.py/patch.py
# use: AuditEntry.incident_id is a required FK, and purging an already-
# trashed RBD image has no real detected Incident behind it, only an
# operator explicitly clicking "Xoá" on the Volumes page.
RBD_TRASH_REMOVE_CEPH_CODE = "RBD_TRASH_REMOVE"
RBD_TRASH_REMOVE_ACTION_ID = "rbd_trash_remove"

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)


def _in_flight_trash_actions(pool: str) -> dict[str, Action]:
    """{trash_id: Action} for every rbd_trash_remove Action still
    PENDING_APPROVAL/APPROVED for this pool — action_params is a JSON TEXT
    column (no portable cross-DB JSON-field query in this codebase, same
    posture as everywhere else Action.action_params gets filtered), so this
    loads the (small) set of in-flight trash-removal Actions and matches in
    Python rather than in SQL."""
    result: dict[str, Action] = {}
    with db.SessionLocal() as session:
        rows = (
            session.query(Action)
            .filter(Action.action_id == RBD_TRASH_REMOVE_ACTION_ID)
            .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
            .all()
        )
        for action in rows:
            try:
                params = json.loads(action.action_params) if action.action_params else {}
            except (TypeError, ValueError):
                continue
            if params.get("pool_name") == pool and params.get("trash_id"):
                result[params["trash_id"]] = action
    return result


@router.get("/volumes", response_class=HTMLResponse)
async def volumes_page(request: Request, user: str = Depends(require_login)):
    pools = ceph_client.configured_rbd_pools()
    requested_pool = request.query_params.get("pool")
    if requested_pool and requested_pool not in pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    # No default pool — same "must actually pick one" posture as
    # dashboard/routes/nodes.py's selected_host (landing on /volumes with
    # no ?pool= shows the empty "chọn một pool" state).
    selected_pool = requested_pool

    trash_entries: list[dict] = []
    trash_error: str | None = None
    trash_pending: dict[str, Action] = {}
    if selected_pool:
        try:
            trash_entries = ceph_client.query_rbd_trash(selected_pool)
        except CephQueryError as exc:
            logger.warning("volumes_page: failed to query trash for pool %r: %s", selected_pool, exc)
            trash_error = str(exc)
        trash_pending = _in_flight_trash_actions(selected_pool)

    return templates.TemplateResponse(
        request,
        "volumes.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "pools": pools,
            "selected_pool": selected_pool,
            "trash_entries": trash_entries,
            "trash_error": trash_error,
            "trash_pending": trash_pending,
        },
    )


@router.get("/api/volumes/{pool}/iostat")
async def volume_iostat_api(pool: str, user: str = Depends(require_login)):
    # `pool` is attacker-reachable input feeding into an `rbd` command run
    # over SSH — same SSRF-via-SSH whitelist posture as
    # dashboard/routes/nodes.py::node_metrics_api's `host` check. Only pools
    # the operator already configured (settings.ceph_rbd_pools) are queryable.
    allowed_pools = set(ceph_client.configured_rbd_pools())
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    try:
        samples = ceph_client.query_rbd_iostat(pool)
    except CephQueryError as exc:
        logger.warning("volume_iostat_api: %s", exc)
        raise HTTPException(status_code=502, detail=f"Không lấy được iostat từ cụm: {exc}")

    # Cross-reference each returned image against any currently-OPEN
    # VOLUME_SATURATED: Incident (watcher/volume_monitor.py owns creating/
    # resolving these) — one query for the whole pool rather than one
    # per-image DB round trip.
    with db.SessionLocal() as session:
        open_codes = {
            incident.ceph_code
            for incident in session.query(Incident)
            .filter(Incident.ceph_code.like("VOLUME_SATURATED:%"))
            .filter(Incident.status.in_(OPEN_STATUSES))
            .all()
        }

    images = [
        {
            "image": sample["image"],
            "iops": sample["iops"],
            "read_latency_ms": sample["read_latency_ms"],
            "write_latency_ms": sample["write_latency_ms"],
            "saturated": ceph_code_for(pool, sample["image"]) in open_codes,
        }
        for sample in samples
    ]
    return {"pool": pool, "images": images}


@router.post("/volumes/{pool}/trash/{trash_id}/propose")
async def propose_rbd_trash_remove(pool: str, trash_id: str, user: str = Depends(require_login)):
    """"Xoá" button on the Volumes page's Trash section — creates a
    PENDING_APPROVAL Action the operator must separately approve (via the
    already-generic POST /actions/{id}/approve — no new approval logic
    needed), same propose-then-approve pattern as dashboard/routes/
    delete_cluster.py/upgrade.py/deploy_cluster.py. Always PENDING_APPROVAL
    regardless of rbd_trash_remove's own SAFE/RISKY classification (RISKY,
    see action_policy.yaml) — same as those other dedicated-route features,
    none of which auto-execute even a SAFE action_id; only Chat-with-AI's
    confirm flow does that.
    """
    allowed_pools = set(ceph_client.configured_rbd_pools())
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")

    action_params = {"pool_name": pool, "trash_id": trash_id}
    try:
        preview_command = executor_commands.get_command(RBD_TRASH_REMOVE_ACTION_ID, None, action_params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")

    with db.SessionLocal() as session:
        existing = (
            session.query(Action)
            .filter(Action.action_id == RBD_TRASH_REMOVE_ACTION_ID)
            .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
            .all()
        )
        for action in existing:
            try:
                params = json.loads(action.action_params) if action.action_params else {}
            except (TypeError, ValueError):
                continue
            if params.get("pool_name") == pool and params.get("trash_id") == trash_id:
                raise HTTPException(
                    status_code=409,
                    detail="Đã có một đề xuất xoá cho volume này đang chờ duyệt hoặc đã duyệt.",
                )

        incident = Incident(
            ceph_code=RBD_TRASH_REMOVE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất xoá vĩnh viễn volume trong trash {pool}/{trash_id} bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=RBD_TRASH_REMOVE_ACTION_ID,
            classification=gate.classify_action(RBD_TRASH_REMOVE_ACTION_ID).value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=(
                f"Xoá vĩnh viễn volume {pool}/{trash_id} khỏi trash — dữ liệu không thể khôi phục "
                f"sau khi thực thi."
            ),
            target_nodes=json.dumps([]),
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

    return RedirectResponse(url=f"/volumes?pool={pool}", status_code=303)
