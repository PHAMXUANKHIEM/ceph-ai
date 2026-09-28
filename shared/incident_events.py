"""Single append-only write path for exact Incident lifecycle events."""
import json
from datetime import datetime
from shared.time import utc_now
from shared.models import IncidentTimelineEvent


ACTOR_MAX_CHARS = 32  # incident_timeline_events.actor / audit_entries.actor


def record(session, *, incident_id: str, event_type: str, actor: str,
           action_id: str | None = None, evidence: dict | None = None,
           source_type: str | None = None, source_id: str | None = None,
           created_at: datetime | None = None) -> IncidentTimelineEvent:
    actor = str(actor or "")
    if len(actor) > ACTOR_MAX_CHARS:
        # The column is VARCHAR(32); PostgreSQL would reject the whole
        # transaction. Keep the full identity in the evidence instead.
        evidence = {**(evidence or {}), "actor_full": actor}
        actor = actor[:ACTOR_MAX_CHARS]
    event = IncidentTimelineEvent(
        incident_id=incident_id, action_id=action_id, event_type=event_type, actor=actor,
        evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True) if evidence is not None else None,
        source_type=source_type, source_id=source_id, created_at=created_at or utc_now(),
    )
    session.add(event)
    return event
