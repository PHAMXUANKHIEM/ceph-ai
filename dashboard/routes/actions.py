import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

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
