import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    CrushOsdDistribution,
    CrushStructureSnapshot,
    Incident,
    IncidentStatus,
    NodeUpgradeGate,
    NodeUpgradeGateState,
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
        "cluster_id",
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
        "telegram_message_ids",
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
    assert columns == {"id", "cluster_id", "success", "mon_node", "error_message", "polled_at"}


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


def test_node_upgrade_gate_insert_and_query(db_session):
    gate = NodeUpgradeGate(host="10.20.1.83", target_version="18.2.4")
    db_session.add(gate)
    db_session.commit()

    fetched = db_session.query(NodeUpgradeGate).one()
    assert fetched.host == "10.20.1.83"
    assert fetched.target_version == "18.2.4"
    assert fetched.roles_snapshot is None
    assert fetched.osd_backup is None
    assert fetched.prepare_action_id is None


def test_node_upgrade_gate_id_defaults_to_valid_uuid4(db_session):
    gate = NodeUpgradeGate(host="10.20.1.83", target_version="18.2.4")
    db_session.add(gate)
    db_session.commit()

    parsed = uuid.UUID(gate.id, version=4)
    assert str(parsed) == gate.id


def test_node_upgrade_gate_state_defaults_to_preparing(db_session):
    gate = NodeUpgradeGate(host="10.20.1.83", target_version="18.2.4")
    db_session.add(gate)
    db_session.commit()
    db_session.refresh(gate)

    assert gate.state == NodeUpgradeGateState.PREPARING.value


def test_node_upgrade_gate_rejects_state_outside_enum(db_session):
    gate = NodeUpgradeGate(host="10.20.1.83", target_version="18.2.4", state="NOT_A_REAL_STATE")
    db_session.add(gate)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_node_upgrade_gate_explicit_id_is_respected(db_session):
    # Story 11.3's Prepare route generates the gate id client-side (to claim
    # the CAS lock BEFORE this row exists) and passes it explicitly here —
    # the default= factory must not override a caller-supplied id.
    explicit_id = str(uuid.uuid4())
    gate = NodeUpgradeGate(id=explicit_id, host="10.20.1.83", target_version="18.2.4")
    db_session.add(gate)
    db_session.commit()

    assert gate.id == explicit_id


def test_crush_structure_snapshot_insert_and_query(db_session):
    snapshot = CrushStructureSnapshot(tree_json='{"roots": []}')
    db_session.add(snapshot)
    db_session.commit()

    fetched = db_session.query(CrushStructureSnapshot).one()
    assert fetched.tree_json == '{"roots": []}'
    assert fetched.diff_json is None  # first-ever snapshot has no diff (AC #1)


def test_crush_structure_snapshot_id_defaults_to_valid_uuid4(db_session):
    snapshot = CrushStructureSnapshot(tree_json='{"roots": []}')
    db_session.add(snapshot)
    db_session.commit()

    parsed = uuid.UUID(snapshot.id, version=4)
    assert str(parsed) == snapshot.id


def test_crush_structure_snapshot_stores_diff_json(db_session):
    snapshot = CrushStructureSnapshot(
        tree_json='{"roots": []}', diff_json='{"added": [], "removed": [], "reweighted": []}'
    )
    db_session.add(snapshot)
    db_session.commit()
    db_session.refresh(snapshot)

    assert snapshot.diff_json == '{"added": [], "removed": [], "reweighted": []}'


def test_crush_osd_distribution_insert_and_query(db_session):
    row = CrushOsdDistribution(osd_id=3, host="node2", bytes_used=1000, bytes_total=2000, pgs=42)
    db_session.add(row)
    db_session.commit()

    fetched = db_session.get(CrushOsdDistribution, 3)
    assert fetched.host == "node2"
    assert fetched.bytes_used == 1000
    assert fetched.bytes_total == 2000
    assert fetched.pgs == 42


def test_crush_osd_distribution_upsert_overwrites_existing_row(db_session):
    # osd_id is the real Ceph osd id (caller-assigned), not an
    # autoincrement surrogate — updating in place (not inserting a second
    # row) is the whole point of this table (AD-27, "latest value only").
    row = CrushOsdDistribution(osd_id=3, host="node2", bytes_used=1000, bytes_total=2000, pgs=42)
    db_session.add(row)
    db_session.commit()

    fetched = db_session.get(CrushOsdDistribution, 3)
    fetched.bytes_used = 1500
    fetched.pgs = 50
    db_session.commit()

    assert db_session.query(CrushOsdDistribution).count() == 1
    refetched = db_session.get(CrushOsdDistribution, 3)
    assert refetched.bytes_used == 1500
    assert refetched.pgs == 50
