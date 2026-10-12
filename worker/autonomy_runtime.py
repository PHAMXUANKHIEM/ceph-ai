"""Runtime rate limits, cooldown and cluster-wide autonomous execution lease."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from shared import audit, incident_events, remediation_cases
from shared.remediation_state_machine import recover_after_worker_restart
from shared.models import Action, ActionStatus, AutopilotLease, Incident, IncidentStatus


@dataclass(frozen=True)
class RuntimeResult:
    allowed: bool
    reason: str | None = None


def check_limits(session, *, cluster_id: str, action_id: str, target_nodes: str | None,
                 now: datetime, max_hour: int, max_day: int, cooldown_seconds: int) -> RuntimeResult:
    base = session.query(Action).join(Incident, Incident.id == Action.incident_id).filter(
        Incident.cluster_id == cluster_id,
        Action.status == ActionStatus.AUTO_EXECUTED.value,
        Action.executed_at.isnot(None),
    )
    # A verified OSD restart is driven by fresh Ceph evidence and exact
    # osd.ID->host mapping at every attempt.  Do not let unrelated earlier
    # remediations, or a recurring crash of the same daemon, suppress it;
    # the cluster-wide execution lease still serializes actual writes.
    unlimited_verified_osd_restart = action_id == "restart_osd_daemon"
    if (not unlimited_verified_osd_restart and max_hour > 0
            and base.filter(Action.executed_at >= now - timedelta(hours=1)).count() >= max_hour):
        return RuntimeResult(False, f"cluster hourly auto-remediation limit ({max_hour}) reached")
    if (not unlimited_verified_osd_restart and max_day > 0
            and base.filter(Action.executed_at >= now - timedelta(days=1)).count() >= max_day):
        return RuntimeResult(False, f"cluster daily auto-remediation limit ({max_day}) reached")
    # Enabling msgr2 is an idempotent cluster-map operation.  If an operator
    # or automation removes the v2 addresses again, leaving the cluster in
    # HEALTH_WARN for the generic target cooldown is worse than safely
    # re-applying the same command.  Hour/day limits and the cluster write
    # lease still protect against a runaway loop.
    cooldown_exempt = action_id in {"restart_osd_daemon", "enable_mon_msgr2"}
    repeated = None
    if not cooldown_exempt and cooldown_seconds > 0:
        repeated = base.filter(
            Action.action_id == action_id, Action.target_nodes == target_nodes,
            Action.executed_at >= now - timedelta(seconds=cooldown_seconds),
        ).first()
    if repeated is not None:
        return RuntimeResult(False, f"action+target cooldown ({cooldown_seconds}s) is active")
    return RuntimeResult(True)


def acquire_lease(session, *, cluster_id: str, action_id: str, now: datetime,
                  ttl_seconds: int) -> RuntimeResult:
    session.query(AutopilotLease).filter(AutopilotLease.expires_at <= now).delete()
    session.add(AutopilotLease(
        cluster_id=cluster_id, action_id=action_id, acquired_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return RuntimeResult(False, "another autonomous write action holds the cluster lease")
    return RuntimeResult(True)


def release_lease(session, *, action_id: str) -> None:
    session.query(AutopilotLease).filter(AutopilotLease.action_id == action_id).delete()
    session.commit()


def reconcile_interrupted_approved_executions(session, *, now: datetime) -> int:
    """Close operator-approved executions a stopped Worker left behind; never re-run them.

    The approved-action path claims an action by moving its Incident to
    EXECUTING while the Action stays APPROVED until the result is recorded.
    A Worker stopped in between (every deploy restarts it; lifecycle actions
    run for 20-60 minutes) left that pair forever: each later poll saw the
    claim and skipped it, nothing reported it, and an APPROVED lifecycle
    action blocked every new lifecycle proposal (Plan readiness-gap-review
    R3, 12/10/2026). Only the Worker runs approved actions, so at its start,
    before any poller runs, every such pair belongs to the previous process.
    The command may have run in part: the Action becomes INCONCLUSIVE, the
    Incident FAILED (so production escalation picks it up), and nothing is
    retried.
    """
    rows = (session.query(Action, Incident).join(Incident, Incident.id == Action.incident_id)
            .filter(Action.status == ActionStatus.APPROVED.value,
                    Incident.status == IncidentStatus.EXECUTING.value)
            .all())
    reason = "Worker dừng giữa lúc thực thi; kết quả không xác định, không tự chạy lại — hãy kiểm tra cụm"
    for action, incident in rows:
        try:
            progress = json.loads(action.execution_progress or "[]")
        except (TypeError, ValueError):
            progress = []
        if not isinstance(progress, list):
            progress = []
        progress.append({"step": "worker_restart", "label": "Worker khởi động lại", "pct": 100,
                         "status": "failed", "message": reason, "finished_at": now.isoformat()})
        action.execution_progress = json.dumps(progress, ensure_ascii=False)
        action.status = ActionStatus.INCONCLUSIVE.value
        remediation_cases.record_inconclusive(session, action_id=action.id, at=now, reason=reason)
        incident.status = IncidentStatus.FAILED.value
        incident.diagnosis_text = f"{incident.diagnosis_text or ''}\n\n[Worker restart] {reason}.".strip()
        audit.record(session, incident_id=incident.id, action_id=action.id,
                     event_type=audit.EVENT_APPROVED_EXECUTION_INTERRUPTED, actor=audit.ACTOR_SYSTEM,
                     evidence={"action_id": action.action_id, "auto_retry": False})
    session.commit()
    return len(rows)


def reconcile_expired_executions(session, *, now: datetime) -> int:
    """Mark orphaned autonomous executions inconclusive; never re-run them."""
    rows = session.query(Action).filter(
        Action.status == ActionStatus.EXECUTING.value,
        Action.classification == "SAFE",
    ).all()
    changed = 0
    for action in rows:
        lease = session.query(AutopilotLease).filter_by(action_id=action.id).one_or_none()
        active_lease = lease is not None and lease.expires_at > now
        recovery = recover_after_worker_restart(action.status, lock_present=active_lease)
        if not recovery.allowed:
            continue
        reason = "cluster execution lease expired" if lease is not None else "cluster execution lease is missing"
        if lease is not None:
            session.delete(lease)
        action.status = ActionStatus.INCONCLUSIVE.value
        remediation_cases.record_inconclusive(
            session, action_id=action.id, at=now, reason=reason,
        )
        incident = session.get(Incident, action.incident_id)
        if incident is not None:
            incident.status = IncidentStatus.FAILED.value
            incident.diagnosis_text = (
                f"{incident.diagnosis_text or ''}\n\n"
                f"[Autopilot recovery] Kết quả không xác định: {reason}; không tự chạy lại."
            ).strip()
            audit.record(
                session, incident_id=incident.id, action_id=action.id,
                event_type=audit.EVENT_AUTOPILOT_EXECUTION_INCONCLUSIVE,
                actor=audit.ACTOR_SYSTEM,
            )
            incident_events.record(
                session, incident_id=incident.id, action_id=action.id,
                event_type="autopilot_execution_recovery",
                actor=audit.ACTOR_SYSTEM,
                evidence={"outcome": "INCONCLUSIVE", "reason": reason, "auto_retry": False},
            )
        changed += 1
    session.commit()
    return changed
