from datetime import datetime

from shared import db as db_module
from shared.models import Action, ActionClassification, ActionStatus, AuditEntry, Incident, IncidentStatus


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _pending_action(incident_id: str = "inc-1") -> str:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code="OSD_DOWN",
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
            )
        )
        action = Action(
            incident_id=incident_id,
            action_id="restart_osd_daemon",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale="looks like a stuck OSD",
            proposed_command="docker restart ceph-osd-B",
            target_nodes='["10.20.1.83"]',
        )
        session.add(action)
        session.commit()
        return action.id


def test_index_shows_pending_action_card(dashboard_client):
    _pending_action()
    _login(dashboard_client)

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Chờ duyệt" in response.text
    assert "restart_osd_daemon" in response.text
    assert "looks like a stuck OSD" in response.text
    assert "docker restart ceph-osd-B" in response.text


def test_approve_action_sets_approved_and_audits_operator_as_actor(dashboard_client):
    action_id = _pending_action("inc-approve")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.APPROVED.value
        incident = session.get(Incident, "inc-approve")
        assert incident.status == IncidentStatus.APPROVED.value
        entries = session.query(AuditEntry).filter_by(incident_id="inc-approve").all()
        assert len(entries) == 1
        assert entries[0].actor == "admin"
        assert entries[0].event_type == "risky_action_approved"


def test_reject_action_sets_rejected(dashboard_client):
    action_id = _pending_action("inc-reject")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{action_id}/reject", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.REJECTED.value
        incident = session.get(Incident, "inc-reject")
        assert incident.status == IncidentStatus.REJECTED.value


def test_approve_action_requires_login(dashboard_client):
    action_id = _pending_action()

    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_approve_unknown_action_returns_404(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/actions/does-not-exist/approve")

    assert response.status_code == 404


def _pending_action_with_no_command(incident_id: str = "inc-no-cmd", action_id: str = "investigate_manually") -> str:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code="POOL_APP_NOT_ENABLED",
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
            )
        )
        action = Action(
            incident_id=incident_id,
            action_id=action_id,
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale="no automated fix exists for this",
            proposed_command=None,
            target_nodes='["10.20.1.83"]',
        )
        session.add(action)
        session.commit()
        return action.id


def test_approve_action_with_no_command_closes_out_without_worker_execution(dashboard_client):
    # 2026-07-23 regression: approving investigate_manually (or
    # pg_repair_force — neither has an automated Command) used to flip
    # status=APPROVED, which the Worker would then always fail to execute,
    # marking the Incident FAILED and falsely reporting the whole cluster
    # status as ERR. It must now close out directly, no Worker involvement.
    action_id = _pending_action_with_no_command()
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.EXECUTED.value
        incident = session.get(Incident, "inc-no-cmd")
        assert incident.status == IncidentStatus.RESOLVED.value
        entries = session.query(AuditEntry).filter_by(incident_id="inc-no-cmd").all()
        assert len(entries) == 1
        assert entries[0].event_type == "risky_action_acknowledged_no_command"


def test_approve_action_with_no_command_pg_repair_force_also_closes_out(dashboard_client):
    action_id = _pending_action_with_no_command("inc-pg-repair", "pg_repair_force")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.EXECUTED.value


def test_approve_already_approved_action_is_a_no_op(dashboard_client):
    action_id = _pending_action("inc-double")
    _login(dashboard_client)
    dashboard_client.post(f"/actions/{action_id}/approve")

    # Second click (double-submit) — must not raise or duplicate audit rows.
    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        entries = session.query(AuditEntry).filter_by(incident_id="inc-double").all()
        assert len(entries) == 1  # not 2
