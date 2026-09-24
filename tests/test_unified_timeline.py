import json
from datetime import datetime, timedelta

from shared import db
from shared.clusters import ensure_default_cluster
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    Incident,
    IncidentStatus,
    IncidentTimelineEvent,
    LogFinding,
    LogFindingStatus,
    LogIngestRun,
    LogIngestStatus,
)
from shared.unified_timeline import build_unified_timeline


NOW = datetime(2026, 9, 21, 8, 0)


def _seed(db_session, cluster_id):
    incident = Incident(
        cluster_id=cluster_id, ceph_code="OSD_DOWN", severity="HEALTH_ERR",
        status=IncidentStatus.NEW.value, detected_at=NOW,
        signal_evidence_json=json.dumps({"osd_id": 5}),
    )
    db_session.add(incident)
    db_session.flush()
    action = Action(
        incident_id=incident.id, action_id="resync_ntp",
        classification=ActionClassification.SAFE.value,
        status=ActionStatus.EXECUTED.value, executed_at=NOW + timedelta(minutes=3),
    )
    db_session.add(action)
    db_session.flush()
    db_session.add(AuditEntry(
        incident_id=incident.id, action_id=action.id,
        event_type="operator_approved", actor="alice", created_at=NOW + timedelta(minutes=1),
    ))
    db_session.add(IncidentTimelineEvent(
        incident_id=incident.id, action_id=action.id, event_type="verification_passed",
        actor="watcher", source_type="manual", source_id="verify-1",
        evidence_json=json.dumps({"health": "HEALTH_OK"}), created_at=NOW + timedelta(minutes=4),
    ))
    run = LogIngestRun(
        cluster_id=cluster_id, source="ssh", window_start=NOW, window_end=NOW + timedelta(minutes=5),
        status=LogIngestStatus.PARTIAL.value, hosts_scanned=3, hosts_failed=1,
        lines_scanned=20, patterns_seen=2, patterns_new=1, error_message="node-3 unavailable",
        created_at=NOW + timedelta(minutes=2),
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(LogFinding(
        cluster_id=cluster_id, ingest_run_id=run.id, verdict="FINDING", severity="WARNING",
        confidence="MEDIUM", title="OSD heartbeat anomaly", summary="heartbeat gap",
        evidence_pattern_ids_json="[]", dedupe_key="dedupe-1", status=LogFindingStatus.OPEN.value,
        created_at=NOW + timedelta(minutes=2, seconds=30),
    ))
    db_session.commit()
    return incident, action


def test_unified_timeline_merges_sources_and_keeps_evidence(db_session):
    cluster_id = ensure_default_cluster(db_session).id
    incident, action = _seed(db_session, cluster_id)
    rows = build_unified_timeline(
        db_session, cluster_id=cluster_id, is_default=True,
        since=NOW - timedelta(minutes=1), until=NOW + timedelta(minutes=10), limit=50,
    )

    assert [row["kind"] for row in rows][:2] == ["post_check", "command"]
    assert {row["source"] for row in rows} >= {
        "incident", "action", "audit", "incident_timeline", "log_finding", "log_ingest",
    }
    assert any(row["kind"] == "approval" for row in rows)
    incident_row = next(row for row in rows if row["source_id"] == incident.id)
    assert incident_row["evidence"]["signal_evidence"] == {"osd_id": 5}
    assert any(row["source_id"] == action.id and row["kind"] == "command" for row in rows)


def test_unified_timeline_does_not_duplicate_audit_mirrored_in_timeline(db_session):
    cluster_id = ensure_default_cluster(db_session).id
    incident = Incident(
        cluster_id=cluster_id, ceph_code="MON_DOWN",
        status=IncidentStatus.NEW.value, detected_at=NOW,
    )
    db_session.add(incident)
    db_session.flush()
    audit = AuditEntry(
        incident_id=incident.id, event_type="operator_approved", actor="alice", created_at=NOW,
    )
    db_session.add(audit)
    db_session.flush()
    db_session.add(IncidentTimelineEvent(
        incident_id=incident.id, event_type=audit.event_type, actor=audit.actor,
        source_type="audit", source_id=audit.id, created_at=NOW,
    ))
    db_session.commit()

    rows = build_unified_timeline(db_session, cluster_id=cluster_id, is_default=True, limit=50)
    assert not any(row["source"] == "audit" for row in rows)
    assert sum(row["source"] == "incident_timeline" for row in rows) == 1


def test_event_timeline_api_is_authenticated_and_scoped(dashboard_client, default_cluster_id):
    with db.SessionLocal() as session:
        _seed(session, default_cluster_id)
    unauthenticated = dashboard_client.get("/api/event-timeline", follow_redirects=False)
    assert unauthenticated.status_code == 303

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/event-timeline", params={"since": "2026-09-21T07:59:00", "limit": 20})
    assert response.status_code == 200
    body = response.json()
    assert body["cluster_id"] == default_cluster_id
    assert body["events"]
    assert all(row["cluster_id"] == default_cluster_id for row in body["events"])


def test_event_timeline_rejects_inverted_window(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get(
        "/api/event-timeline", params={"since": "2026-09-21T10:00:00", "until": "2026-09-21T09:00:00"}
    )
    assert response.status_code == 400
