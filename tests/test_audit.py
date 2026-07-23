from datetime import datetime

from shared import audit
from shared.models import Action, ActionClassification, AuditEntry, Incident


def _seed_incident_and_action(session):
    incident = Incident(ceph_code="MON_CLOCK_SKEW", detected_at=datetime.utcnow())
    session.add(incident)
    session.commit()
    action = Action(
        incident_id=incident.id, action_id="resync_ntp", classification=ActionClassification.SAFE.value
    )
    session.add(action)
    session.commit()
    return incident, action


def test_record_creates_audit_entry_with_all_fields(db_session):
    incident, action = _seed_incident_and_action(db_session)

    audit.record(
        db_session,
        incident_id=incident.id,
        action_id=action.id,
        event_type=audit.EVENT_SAFE_ACTION_EXECUTED,
        actor=audit.ACTOR_SYSTEM,
    )
    db_session.commit()

    entry = db_session.query(AuditEntry).one()
    assert entry.incident_id == incident.id
    assert entry.action_id == action.id
    assert entry.event_type == audit.EVENT_SAFE_ACTION_EXECUTED
    assert entry.actor == audit.ACTOR_SYSTEM
    assert entry.created_at is not None


def test_record_allows_action_id_none(db_session):
    incident = Incident(ceph_code="MON_CLOCK_SKEW", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.commit()

    audit.record(
        db_session,
        incident_id=incident.id,
        action_id=None,
        event_type=audit.EVENT_SAFE_ACTION_EXECUTED,
        actor=audit.ACTOR_SYSTEM,
    )
    db_session.commit()

    entry = db_session.query(AuditEntry).one()
    assert entry.action_id is None


def test_record_does_not_commit_itself(db_session):
    incident, action = _seed_incident_and_action(db_session)

    audit.record(
        db_session,
        incident_id=incident.id,
        action_id=action.id,
        event_type=audit.EVENT_SAFE_ACTION_EXECUTED,
        actor=audit.ACTOR_SYSTEM,
    )
    db_session.rollback()

    assert db_session.query(AuditEntry).count() == 0
