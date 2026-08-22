"""Runtime rate limits, cooldown and cluster-wide autonomous execution lease."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from shared.models import Action, ActionStatus, AutopilotLease, Incident


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
    if base.filter(Action.executed_at >= now - timedelta(hours=1)).count() >= max_hour:
        return RuntimeResult(False, f"cluster hourly auto-remediation limit ({max_hour}) reached")
    if base.filter(Action.executed_at >= now - timedelta(days=1)).count() >= max_day:
        return RuntimeResult(False, f"cluster daily auto-remediation limit ({max_day}) reached")
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
