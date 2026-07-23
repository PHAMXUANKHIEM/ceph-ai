from datetime import datetime, timedelta

from shared import db as db_module
from shared.models import AuditEntry, Incident, IncidentStatus


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _seed_incident_and_entry(incident_id: str, event_type: str, created_at: datetime, actor: str = "system") -> None:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code="OSD_DOWN",
                status=IncidentStatus.RESOLVED.value,
                detected_at=created_at,
            )
        )
        session.add(
            AuditEntry(
                incident_id=incident_id,
                action_id=None,
                event_type=event_type,
                actor=actor,
                created_at=created_at,
            )
        )
        session.commit()


def test_audit_requires_login(dashboard_client):
    response = dashboard_client.get("/audit", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_audit_lists_all_entries_with_no_filter(dashboard_client):
    _seed_incident_and_entry("inc-1", "safe_action_executed", datetime.utcnow())
    _seed_incident_and_entry("inc-2", "risky_action_approved", datetime.utcnow(), actor="admin")
    _login(dashboard_client)

    response = dashboard_client.get("/audit")

    assert response.status_code == 200
    assert "safe_action_executed" in response.text
    assert "risky_action_approved" in response.text
    assert "admin" in response.text


def test_audit_filters_by_incident_id(dashboard_client):
    _seed_incident_and_entry("inc-a", "safe_action_executed", datetime.utcnow())
    _seed_incident_and_entry("inc-b", "risky_action_approved", datetime.utcnow())
    _login(dashboard_client)

    response = dashboard_client.get("/audit", params={"incident_id": "inc-a"})

    assert response.status_code == 200
    assert "safe_action_executed" in response.text
    assert "risky_action_approved" not in response.text


def test_audit_filters_by_time_range(dashboard_client):
    old_time = datetime.utcnow() - timedelta(days=10)
    recent_time = datetime.utcnow()
    _seed_incident_and_entry("inc-old", "safe_action_executed", old_time)
    _seed_incident_and_entry("inc-recent", "risky_action_approved", recent_time)
    _login(dashboard_client)

    since = (datetime.utcnow() - timedelta(days=1)).isoformat(timespec="minutes")
    response = dashboard_client.get("/audit", params={"since": since})

    assert response.status_code == 200
    assert "risky_action_approved" in response.text
    assert "safe_action_executed" not in response.text


def test_audit_shows_empty_state_when_no_entries(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/audit")

    assert response.status_code == 200
    assert "Không có bản ghi audit" in response.text


def test_audit_ignores_unparseable_filter_instead_of_500(dashboard_client):
    _seed_incident_and_entry("inc-x", "safe_action_executed", datetime.utcnow())
    _login(dashboard_client)

    response = dashboard_client.get("/audit", params={"since": "not-a-date"})

    assert response.status_code == 200
    assert "safe_action_executed" in response.text
