import asyncio
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
from shared.models import Action, ActionClassification, ActionStatus, Incident, IncidentStatus
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
# 2026-07-28: same synthetic incident, distinct ceph_code — the "Xoá tất cả
# trash" button below (purge_all_rbd_trash) executes immediately with no
# approval step, so it needs its own code to avoid a purge-all's Incident
# ever being mistaken for a per-image proposal still awaiting approval
# (_in_flight_trash_actions/propose_rbd_trash_remove's duplicate-check only
# ever look at RBD_TRASH_REMOVE_ACTION_ID rows, but keeping the ceph_code
# distinct too makes the Incident list/Audit Trail unambiguous at a glance).
RBD_TRASH_PURGE_ALL_CEPH_CODE = "RBD_TRASH_PURGE_ALL"

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)


# 2026-07-28: same "own copy, not a cross-import" posture as
# dashboard/routes/{settings,users,maintenance}.py's identical helper.
def _require_admin_privilege(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép thực hiện thao tác này",
        )


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


def _volumes_page_context(
    user: str,
    pool: str | None,
    *,
    purge_error: str | None = None,
    purge_success: str | None = None,
) -> dict:
    """Shared by the GET page load and the "Xoá tất cả trash" POST below
    (which re-renders this same page directly rather than redirecting —
    unlike propose_rbd_trash_remove's redirect, a purge-all's own result
    has nothing left to look up after the fact via a GET, it only exists
    as this response's own purge_error/purge_success)."""
    pools = ceph_client.configured_rbd_pools()
    trash_entries: list[dict] = []
    trash_error: str | None = None
    trash_pending: dict[str, Action] = {}
    if pool:
        try:
            trash_entries = ceph_client.query_rbd_trash(pool)
        except CephQueryError as exc:
            logger.warning("_volumes_page_context: failed to query trash for pool %r: %s", pool, exc)
            trash_error = str(exc)
        trash_pending = _in_flight_trash_actions(pool)

    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "pools": pools,
        "selected_pool": pool,
        "trash_entries": trash_entries,
        "trash_error": trash_error,
        "trash_pending": trash_pending,
        "purge_error": purge_error,
        "purge_success": purge_success,
    }


@router.get("/volumes", response_class=HTMLResponse)
async def volumes_page(request: Request, user: str = Depends(require_login)):
    pools = ceph_client.configured_rbd_pools()
    requested_pool = request.query_params.get("pool")
    if requested_pool and requested_pool not in pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    # No default pool — same "must actually pick one" posture as
    # dashboard/routes/nodes.py's selected_host (landing on /volumes with
    # no ?pool= shows the empty "chọn một pool" state).
    return templates.TemplateResponse(
        request, "volumes.html", _volumes_page_context(user, requested_pool)
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

    # 2026-07-28 fix: this used to be target_nodes=[] — worker/llm/
    # router_client.py::_execute_approved_action requires a NON-EMPTY host
    # list (dashboard/chat_client.py: "management action_ids, target_nodes
    # must contain exactly ONE host") and marks the Action FAILED outright
    # otherwise, without ever attempting the command. Approving this Action
    # therefore always failed, silently — verified by reading, not by a
    # user report, so treat this as unconfirmed against a real approval
    # until one actually succeeds against a live cluster. rbd_trash_remove
    # is a single global command (like ceph osd pool delete/create), not a
    # per-host one — exactly one MON, no fan-out, same convention.
    mon_nodes = ceph_client.get_mon_nodes()
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="Chưa cấu hình CEPH_MON_NODES")

    action_params = {"pool_name": pool, "trash_id": trash_id}
    try:
        preview_command = executor_commands.get_command(RBD_TRASH_REMOVE_ACTION_ID, mon_nodes[0], action_params)
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
            target_nodes=json.dumps([mon_nodes[0]]),
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


@router.post("/volumes/{pool}/trash/purge-all", response_class=HTMLResponse)
async def purge_all_rbd_trash(request: Request, pool: str, user: str = Depends(require_login)):
    """"Xoá tất cả trash" button — force-removes EVERY entry currently in
    this pool's trash immediately, with NO propose/approve step. By
    explicit operator request, a deliberate one-off exception to this
    codebase's usual RISKY-action posture (worker/policy/action_policy.yaml
    calls rbd_trash_remove out by name as always requiring approval, "no
    exceptions" — this button IS that exception, scoped to exactly this one
    bulk-purge path; the per-image "Xoá" button above still goes through
    the normal propose/approve flow unchanged). Admin-gated as the
    substitute safety check for skipping that second-person approval.

    Mirrors the operator's own shell loop (`for id in $(rbd trash ls pool |
    awk '{print $1}'); do rbd trash rm pool/$id --force; done`) as a single
    click — see watcher/ceph_client.py::force_purge_rbd_trash for the
    --force implications (bypasses the still-in-use-image protection the
    per-image button's Command deliberately keeps).

    Still recorded to the Incident/Action/Audit Trail (RBD_TRASH_PURGE_ALL_
    CEPH_CODE, status EXECUTED — never PENDING_APPROVAL, since nothing here
    is pending anything by the time this Incident/Action row is written),
    same "every action is traceable to who/when/what" guarantee every other
    feature in this app keeps, even though this one skips approval itself.
    """
    _require_admin_privilege(user)
    allowed_pools = set(ceph_client.configured_rbd_pools())
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")

    try:
        results = await asyncio.to_thread(ceph_client.force_purge_rbd_trash, pool)
    except CephQueryError as exc:
        logger.warning("purge_all_rbd_trash: failed to list/purge trash for pool %r: %s", pool, exc)
        return templates.TemplateResponse(
            request,
            "volumes.html",
            _volumes_page_context(
                user, pool, purge_error=f"Không lấy được danh sách trash: {exc}"
            ),
        )

    if not results:
        return templates.TemplateResponse(
            request,
            "volumes.html",
            _volumes_page_context(user, pool, purge_success="Trash của pool này đang trống, không có gì để xoá."),
        )

    failures = [r for r in results if r["error"]]
    succeeded_count = len(results) - len(failures)
    summary = f"Đã xoá {succeeded_count}/{len(results)} volume trong trash của pool {pool}."
    if failures:
        summary += " Lỗi: " + "; ".join(f"{r['name']} ({r['id']}): {r['error']}" for r in failures)

    with db.SessionLocal() as session:
        incident = Incident(
            ceph_code=RBD_TRASH_PURGE_ALL_CEPH_CODE,
            status=IncidentStatus.RESOLVED.value,
            log_excerpt=f"Xoá tất cả trash (force, không qua duyệt) trong pool {pool} bởi {user}. {summary}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=RBD_TRASH_REMOVE_ACTION_ID,
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.EXECUTED.value,
            rationale=summary,
            target_nodes=json.dumps([]),
            action_params=json.dumps(
                {"pool_name": pool, "trash_ids": [r["id"] for r in results], "bulk": True, "force": True}
            ),
        )
        session.add(action)
        session.flush()

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_EXECUTED,
            actor=user,
        )
        session.commit()

    return templates.TemplateResponse(
        request,
        "volumes.html",
        _volumes_page_context(
            user,
            pool,
            purge_error=summary if failures else None,
            purge_success=None if failures else summary,
        ),
    )
