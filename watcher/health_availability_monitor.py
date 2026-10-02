"""Alert when Watcher cannot read Ceph health for too long.

On 2026-10-02 the MON leader host was overloaded (load average 34) and every
`ceph health` query, on healthy MONs too, exceeded its deadline for about 2.5
hours. Watcher logged an ERROR and a failed heartbeat each poll, but nothing
reached an operator: every other Ceph alert (BlueStore slow ops, degraded PGs)
was silently impossible to detect for the whole window.

One approval-gated `investigate_manually` Incident is opened once health has
been unreadable for `ceph_health_unavailable_alert_seconds`, its evidence is
refreshed while the outage lasts (no repeated Telegram message), and it is
resolved by the first successful health read.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from shared import alert_lifecycle, audit, db, telegram_outbox
from shared.incident_actions import cancel_pending_actions
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from shared.time import utc_now
from worker.policy import gate

logger = logging.getLogger(__name__)

CEPH_HEALTH_UNAVAILABLE_CODE = "CEPH_HEALTH_UNAVAILABLE"
CEPH_HEALTH_UNAVAILABLE_ACTION_ID = "investigate_manually"
_MAX_ERROR_CHARS = 500
_OPEN_STATUSES = (
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
)


@dataclass
class HealthAvailabilityTracker:
    """In-memory outage clock for one cluster loop.

    A Watcher restart starts the clock again, which can only delay the alert
    by one threshold; it never opens a duplicate (see `report_unavailable`).
    """

    unavailable_since: datetime | None = None
    consecutive_failures: int = 0
    last_error: str = field(default="")
    # False until the first successful read, which also clears an Incident
    # left open by a previous Watcher process.
    startup_checked: bool = False

    def record_failure(self, error: str, now: datetime | None = None) -> dict | None:
        """Count one failed poll; return alert detail once the outage is long enough."""
        now = now or utc_now()
        if self.unavailable_since is None:
            self.unavailable_since = now
        self.consecutive_failures += 1
        self.last_error = error[:_MAX_ERROR_CHARS]
        elapsed = (now - self.unavailable_since).total_seconds()
        if elapsed < settings.ceph_health_unavailable_alert_seconds:
            return None
        return {
            "unavailable_since": self.unavailable_since.isoformat(),
            "unavailable_seconds": int(elapsed),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "threshold_seconds": settings.ceph_health_unavailable_alert_seconds,
        }

    def record_success(self) -> bool:
        """Reset the clock; return whether an outage was in progress."""
        was_unavailable = self.unavailable_since is not None
        self.unavailable_since = None
        self.consecutive_failures = 0
        self.last_error = ""
        return was_unavailable


def _rationale(detail: dict) -> str:
    minutes = max(1, detail["unavailable_seconds"] // 60)
    return (
        f"Watcher không đọc được `ceph health` từ bất kỳ MON nào trong {minutes} phút "
        f"({detail['consecutive_failures']} lần liên tiếp). Trong thời gian này mọi cảnh "
        "báo Ceph khác (OSD, PG, BlueStore...) đều không thể phát hiện. "
        f"Lỗi gần nhất: {detail['last_error'] or 'không rõ'}. "
        "Kiểm tra MON leader và quorum, tải CPU/IO và SSH của các node MON; "
        "hệ thống không tự thao tác."
    )


def _open_incidents(session, cluster_id: str | None, include_legacy_null: bool):
    cluster_filter = (
        or_(Incident.cluster_id == cluster_id, Incident.cluster_id.is_(None))
        if include_legacy_null
        else Incident.cluster_id == cluster_id
    )
    return (
        session.query(Incident)
        .filter(
            Incident.ceph_code == CEPH_HEALTH_UNAVAILABLE_CODE,
            Incident.status.in_(_OPEN_STATUSES),
            cluster_filter,
        )
        .all()
    )


def report_unavailable(
    detail: dict, *, cluster_id: str | None, include_legacy_null: bool = True,
) -> str | None:
    """Open the outage Incident once, or refresh its evidence; return its id."""
    rationale = _rationale(detail)
    evidence = json.dumps(
        {"source": "watcher_health_query", **detail}, ensure_ascii=False, sort_keys=True,
    )
    event_id: str | None = None
    with db.SessionLocal() as session:
        existing = _open_incidents(session, cluster_id, include_legacy_null)
        if existing:
            incident = existing[0]
            incident.signal_evidence_json = evidence
            incident.log_excerpt = rationale
            session.commit()
            return incident.id
        incident = Incident(
            cluster_id=cluster_id,
            ceph_code=CEPH_HEALTH_UNAVAILABLE_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            detected_at=utc_now(),
            log_excerpt=rationale,
            signal_evidence_json=evidence,
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=CEPH_HEALTH_UNAVAILABLE_ACTION_ID,
            classification=gate.classify_action(CEPH_HEALTH_UNAVAILABLE_ACTION_ID).value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=rationale,
            target_nodes=json.dumps([]),
            action_params=json.dumps({}),
        )
        session.add(action)
        session.flush()
        audit.record(
            session, incident_id=incident.id, action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=audit.ACTOR_SYSTEM,
        )
        if not alert_lifecycle.inherit_active_mute(session, incident):
            event_id = telegram_outbox.enqueue_incident_alert(session, incident)
        incident_id = incident.id
        try:
            session.commit()
        except IntegrityError:
            # Another loop opened the in-flight row first (unique index).
            session.rollback()
            return None
    if event_id:
        telegram_outbox.dispatch_due(limit=1, event_ids=[event_id])
    logger.error("health availability: opened %s for cluster %s", incident_id, cluster_id)
    return incident_id


def resolve_unavailable(*, cluster_id: str | None, include_legacy_null: bool = True) -> int:
    """Resolve the open outage Incident after a successful health read."""
    with db.SessionLocal() as session:
        incidents = _open_incidents(session, cluster_id, include_legacy_null)
        for incident in incidents:
            incident.status = IncidentStatus.RESOLVED.value
            cancel_pending_actions(session, incident.id)
        session.commit()
    return len(incidents)


def on_health_read(
    tracker: HealthAvailabilityTracker, *, cluster_id: str | None, include_legacy_null: bool = True,
) -> None:
    """Call after every successful health read; never raises."""
    was_unavailable = tracker.record_success()
    if not was_unavailable and tracker.startup_checked:
        return
    tracker.startup_checked = True
    try:
        resolve_unavailable(cluster_id=cluster_id, include_legacy_null=include_legacy_null)
    except Exception:
        logger.exception("health availability: failed to resolve outage incident")


def on_health_failure(
    tracker: HealthAvailabilityTracker,
    error: str,
    *,
    cluster_id: str | None,
    include_legacy_null: bool = True,
) -> None:
    """Call after every failed health read; never raises."""
    detail = tracker.record_failure(error)
    if detail is None:
        return
    try:
        report_unavailable(detail, cluster_id=cluster_id, include_legacy_null=include_legacy_null)
    except Exception:
        logger.exception("health availability: failed to report outage incident")
