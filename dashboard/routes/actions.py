import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from dashboard.routes import patch as patch_routes
from dashboard.routes import upgrade as upgrade_routes
from dashboard.routes.auth import require_login
from shared import audit, db
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from worker.executor import commands as executor_commands

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/actions/{action_id}/approve")
async def approve_action(action_id: str, user: str = Depends(require_login)):
    """Story 4.3: Dashboard only ever flips Action.status (AD-3) — it never
    executes anything itself. The Worker's approval poller
    (worker/llm/router_client.py::poll_approved_actions) picks up
    status=APPROVED independently and runs the command over SSH.

    2026-07-23 fix: that's only safe to do when action_id actually HAS a
    Command (executor_commands.has_command). action_id values like
    investigate_manually/pg_repair_force deliberately have none — routing
    those to Worker execution always raised ExecutorError, which marked
    the Action AND its Incident FAILED for something that was never a real
    execution failure, and made the Dashboard's cluster-status badge
    falsely report "ERR". "Duyệt" on one of these instead directly closes
    it out as acknowledged — no Worker execution attempted, nothing to
    fail.
    """
    with db.SessionLocal() as session:
        action = session.get(Action, action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Action")
        if action.status != ActionStatus.PENDING_APPROVAL.value:
            # Already handled — a double-submit (double-click, back-button
            # resubmit) or a second operator tab. No-op: the page they land
            # back on already reflects reality.
            return RedirectResponse(url="/", status_code=303)

        # 2026-07-23: a live cluster upgrade must never race with some OTHER
        # risky action being approved at the same time (e.g. restarting an
        # OSD daemon mid-upgrade) — the upgrade's OWN approval is exempt
        # from its own gate. Cheap DB check first (covers proposed/approved-
        # awaiting-poll); only falls through to a live `ceph orch upgrade
        # status` check (see is_cluster_upgrade_physically_running's
        # docstring for why it's needed AND why it fails open) when the
        # cheap check didn't already find anything.
        if action.action_id not in upgrade_routes.CLUSTER_UPGRADE_ACTION_IDS:
            if upgrade_routes.is_cluster_upgrade_pending_or_approved(
                session
            ) or await asyncio.to_thread(upgrade_routes.is_cluster_upgrade_physically_running):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Đang có đề xuất/quá trình nâng cấp cụm — tạm khoá duyệt hành động khác "
                        "cho tới khi nâng cấp xong."
                    ),
                )

        # 2026-07-24: symmetric counterpart — a patch_install is just as
        # disruptive to the live cluster as a cluster upgrade (installs
        # packages + restarts daemons on every configured Ceph node), so it
        # gets the same mutual-exclusion treatment, both ways: approving
        # some OTHER action (including an upgrade) is blocked while a patch
        # install is in-flight, and patch_install's OWN approval is exempt
        # from this specific check (same "exempt from its own gate" posture
        # as CLUSTER_UPGRADE_ACTION_IDS above). No live-cluster-state
        # equivalent of is_cluster_upgrade_physically_running is needed here
        # — there's no orchestrator to query for a patch install's progress,
        # only the DB Action/Incident state.
        if action.action_id != patch_routes.PATCH_INSTALL_ACTION_ID:
            if patch_routes.is_patch_install_pending_or_approved(session):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Đang có đề xuất/quá trình cài đặt patch Ceph — tạm khoá duyệt hành động "
                        "khác cho tới khi xong."
                    ),
                )

        incident = session.get(Incident, action.incident_id)

        if not executor_commands.has_command(action.action_id):
            action.status = ActionStatus.EXECUTED.value
            if incident is not None:
                incident.status = IncidentStatus.RESOLVED.value
            audit.record(
                session,
                incident_id=action.incident_id,
                action_id=action.id,
                event_type=audit.EVENT_RISKY_ACTION_ACKNOWLEDGED_NO_COMMAND,
                actor=user,
            )
            session.commit()
            return RedirectResponse(url="/", status_code=303)

        action.status = ActionStatus.APPROVED.value
        if incident is not None:
            incident.status = IncidentStatus.APPROVED.value
        audit.record(
            session,
            incident_id=action.incident_id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_APPROVED,
            # The operator who clicked, not "system" — FR9 requires "ai duyệt".
            actor=user,
        )
        session.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/actions/{action_id}/reject")
async def reject_action(action_id: str, user: str = Depends(require_login)):
    with db.SessionLocal() as session:
        action = session.get(Action, action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Action")
        if action.status != ActionStatus.PENDING_APPROVAL.value:
            return RedirectResponse(url="/", status_code=303)

        action.status = ActionStatus.REJECTED.value
        incident = session.get(Incident, action.incident_id)
        if incident is not None:
            # AC (Story 4.3): Incident vẫn lưu lại để tham khảo, không thử
            # thêm hành động nào — REJECTED is a terminal Incident status too.
            incident.status = IncidentStatus.REJECTED.value
        audit.record(
            session,
            incident_id=action.incident_id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_REJECTED,
            actor=user,
        )
        session.commit()
    return RedirectResponse(url="/", status_code=303)
