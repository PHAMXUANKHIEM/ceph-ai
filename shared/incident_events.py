"""Single append-only write path for exact Incident lifecycle events."""
import json
from datetime import datetime
from shared.models import IncidentTimelineEvent


def record(session, *, incident_id: str, event_type: str, actor: str,
           action_id: str | None = None, evidence: dict | None = None,
           source_type: str | None = None, source_id: str | None = None,
           created_at: datetime | None = None) -> IncidentTimelineEvent:
    event = IncidentTimelineEvent(
        incident_id=incident_id, action_id=action_id, event_type=event_type, actor=actor,
        evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True) if evidence is not None else None,
        source_type=source_type, source_id=source_id, created_at=created_at or datetime.utcnow(),
    )
    session.add(event)
    return event
