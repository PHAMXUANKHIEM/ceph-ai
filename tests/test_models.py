import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    Incident,
    IncidentStatus,
    WatcherHeartbeat,
)


def test_incident_insert_and_query(db_session):
    # naive UTC — SQLite's DateTime column round-trips without tzinfo
    detected_at = datetime.utcnow()
    incident = Incident(
        ceph_code="OSD_DOWN",
        log_excerpt="osd.3 marked down after no beacon",
        detected_at=detected_at,
    )
    db_session.add(incident)
    db_session.commit()

    fetched = db_session.query(Incident).one()
    assert fetched.ceph_code == "OSD_DOWN"
    assert fetched.log_excerpt == "osd.3 marked down after no beacon"
    assert fetched.detected_at == detected_at


def test_incident_id_defaults_to_valid_uuid4(db_session):
    incident = Incident(ceph_code="MON_CLOCK_SKEW", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.commit()

    parsed = uuid.UUID(incident.id, version=4)
    assert str(parsed) == incident.id


def test_incident_status_defaults_to_new(db_session):
    incident = Incident(ceph_code="PG_DEGRADED", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.commit()
    db_session.refresh(incident)

    assert incident.status == IncidentStatus.NEW.value


def test_incident_rejects_status_outside_enum(db_session):
    incident = Incident(ceph_code="OSD_DOWN", status="NOT_A_REAL_STATUS", detected_at=datetime.utcnow())
    db_session.add(incident)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_incident_has_required_columns():
    columns = {c.name for c in Incident.__table__.columns}
    assert columns == {
        "id",
        "ceph_code",
        "status",
        "severity",
        "log_excerpt",
        "diagnosis_text",
        "detected_at",
        "created_at",
        "updated_at",
    }


def test_action_has_required_columns():
    columns = {c.name for c in Action.__table__.columns}
    assert columns == {
        "id",
        "incident_id",
        "action_id",
        "classification",
        "status",
        "proposed_command",
        "rationale",
        "target_nodes",
        "action_params",
        "execution_progress",
        "telegram_message_id",
        "telegram_notified_at",
        "executed_at",
        "created_at",
        "updated_at",
    }


def test_action_foreign_key_links_to_incident(db_session):
    incident = Incident(ceph_code="MON_CLOCK_SKEW", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.commit()

    action = Action(
        incident_id=incident.id,
        action_id="resync_ntp",
        classification=ActionClassification.SAFE.value,
    )
    db_session.add(action)
    db_session.commit()
    db_session.refresh(action)

    fetched = db_session.query(Action).one()
    assert fetched.incident_id == incident.id
    assert fetched.action_id == "resync_ntp"


def test_action_status_defaults_to_pending(db_session):
    incident = Incident(ceph_code="OSD_DOWN", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.commit()

    action = Action(
        incident_id=incident.id, action_id="restart_osd_daemon", classification=ActionClassification.RISKY.value
    )
    db_session.add(action)
    db_session.commit()
    db_session.refresh(action)

    assert action.status == ActionStatus.PENDING.value


def test_action_rejects_classification_outside_enum(db_session):
    incident = Incident(ceph_code="OSD_DOWN", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.commit()

    action = Action(incident_id=incident.id, action_id="x", classification="NOT_A_REAL_CLASSIFICATION")
    db_session.add(action)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_action_rejects_status_outside_enum(db_session):
    incident = Incident(ceph_code="OSD_DOWN", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.commit()

    action = Action(
        incident_id=incident.id,
        action_id="x",
        classification=ActionClassification.SAFE.value,
        status="NOT_A_REAL_STATUS",
    )
    db_session.add(action)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_action_foreign_key_is_actually_enforced_by_sqlite(db_session):
    # Regression guard for the PRAGMA foreign_keys=ON fix — without it,
    # SQLite silently accepts an Action pointing at a non-existent Incident.
    action = Action(
        incident_id="00000000-0000-0000-0000-000000000000",  # no such Incident
        action_id="x",
        classification=ActionClassification.SAFE.value,
    )
    db_session.add(action)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_audit_entry_has_required_columns():
    columns = {c.name for c in AuditEntry.__table__.columns}
    assert columns == {"id", "incident_id", "action_id", "event_type", "actor", "created_at"}


def test_audit_entry_foreign_keys_link_to_incident_and_action(db_session):
    incident = Incident(ceph_code="MON_CLOCK_SKEW", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.commit()
    action = Action(
        incident_id=incident.id, action_id="resync_ntp", classification=ActionClassification.SAFE.value
    )
    db_session.add(action)
    db_session.commit()

    entry = AuditEntry(
        incident_id=incident.id, action_id=action.id, event_type="safe_action_executed", actor="system"
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)

    fetched = db_session.query(AuditEntry).one()
    assert fetched.incident_id == incident.id
    assert fetched.action_id == action.id


def test_audit_entry_action_id_can_be_null(db_session):
    incident = Incident(ceph_code="MON_CLOCK_SKEW", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.commit()

    entry = AuditEntry(incident_id=incident.id, action_id=None, event_type="some_event", actor="system")
    db_session.add(entry)
    db_session.commit()  # must not raise

    assert db_session.query(AuditEntry).one().action_id is None


def test_audit_entry_foreign_key_is_actually_enforced_by_sqlite(db_session):
    entry = AuditEntry(
        incident_id="00000000-0000-0000-0000-000000000000",
        action_id=None,
        event_type="some_event",
        actor="system",
    )
    db_session.add(entry)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_watcher_heartbeat_has_required_columns():
    columns = {c.name for c in WatcherHeartbeat.__table__.columns}
    assert columns == {"id", "success", "mon_node", "error_message", "polled_at"}


def test_watcher_heartbeat_insert_and_query(db_session):
    heartbeat = WatcherHeartbeat(
        id=1,
        success=True,
        mon_node="10.20.1.150",
        error_message=None,
        polled_at=datetime.utcnow(),
    )
    db_session.add(heartbeat)
    db_session.commit()

    fetched = db_session.query(WatcherHeartbeat).one()
    assert fetched.id == 1
    assert fetched.success is True
    assert fetched.mon_node == "10.20.1.150"


def test_watcher_heartbeat_error_message_can_be_null(db_session):
    heartbeat = WatcherHeartbeat(
        id=1, success=True, mon_node="10.20.1.150", error_message=None, polled_at=datetime.utcnow()
    )
    db_session.add(heartbeat)
    db_session.commit()  # must not raise

    assert db_session.query(WatcherHeartbeat).one().error_message is None
