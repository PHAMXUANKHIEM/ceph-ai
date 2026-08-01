"""Story 9.7 UI — dashboard/routes/restore_cluster.py (`restore_cluster_from_backup`).
Same structure/conventions as tests/test_dashboard_deploy_cluster.py and
tests/test_dashboard_convert_cluster.py."""

import json
from datetime import datetime as _dt

from sqlalchemy.exc import OperationalError

from shared import db as db_module
from shared.models import Action, ActionStatus, Incident, IncidentStatus


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _valid_payload(**overrides):
    payload = {
        "version": "18.2.8",
        "nodes": [
            {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
            {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disk": "/dev/vdc"},
            {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disk": "/dev/vdb"},
        ],
        "public_network": "10.20.1.0/24",
        "cluster_network": "10.20.1.0/24",
        "osd_pool_default_size": 3,
        "osd_pool_default_min_size": 2,
    }
    payload.update(overrides)
    return payload


def test_unauthenticated_get_restore_cluster_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/restore-cluster", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_propose_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/restore-cluster/propose", json=_valid_payload(), follow_redirects=False
    )
    assert response.status_code == 303


def test_get_restore_cluster_shows_form(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/restore-cluster")
    assert response.status_code == 200
    assert "restore-form" in response.text
    assert "Đề xuất khôi phục" in response.text


def test_get_restore_cluster_handles_db_error_gracefully(dashboard_client, monkeypatch):
    _login(dashboard_client)

    def _broken_session_local():
        raise OperationalError("SELECT 1", {}, Exception("no such table: actions"))

    monkeypatch.setattr(db_module, "SessionLocal", _broken_session_local)
    response = dashboard_client.get("/restore-cluster")

    assert response.status_code == 503


def test_propose_creates_pending_risky_action(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/restore-cluster/propose", json=_valid_payload())

    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.action_id == "restore_cluster_from_backup"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == "RISKY"
        params = json.loads(action.action_params)
        assert params["version"] == "18.2.8"
        assert params["method"] == "ceph-deploy"
        assert len(params["nodes"]) == 3
        target_nodes = json.loads(action.target_nodes)
        assert target_nodes == ["10.20.1.112", "10.20.1.95", "10.20.1.21"]

        incident = session.get(Incident, action.incident_id)
        assert incident.ceph_code == "RESTORE_CLUSTER_FROM_BACKUP"
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value


def test_propose_rejects_invalid_version(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/restore-cluster/propose", json=_valid_payload(version="not-a-version"))
    assert response.status_code == 400
    assert "Phiên bản" in response.json()["detail"]


def test_propose_rejects_missing_mon_node(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/restore-cluster/propose",
        json=_valid_payload(nodes=[{"ip": "10.20.1.95", "roles": ["mgr", "osd"], "osd_disk": "/dev/vdc"}]),
    )
    assert response.status_code == 400
    assert "MON" in response.json()["detail"]


def test_propose_rejects_duplicate_ip(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/restore-cluster/propose",
        json=_valid_payload(
            nodes=[
                {"ip": "10.20.1.112", "roles": ["mon", "mgr", "osd"], "osd_disk": "/dev/vdc"},
                {"ip": "10.20.1.112", "roles": ["mon"]},
            ]
        ),
    )
    assert response.status_code == 400
    assert "trùng" in response.json()["detail"]


def test_propose_rejects_second_proposal_while_one_pending(dashboard_client):
    _login(dashboard_client)

    first = dashboard_client.post("/restore-cluster/propose", json=_valid_payload())
    assert first.status_code == 201

    second = dashboard_client.post("/restore-cluster/propose", json=_valid_payload())
    assert second.status_code == 409


def test_propose_blocked_while_a_deploy_is_in_flight(dashboard_client):
    """Same mutual-exclusion posture as convert_cluster.py's own propose
    route — restoring an entire cluster must never race with some OTHER
    cluster-lifecycle action (deploy/delete/convert) already in flight."""
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code="CLUSTER_DEPLOY",
            status=IncidentStatus.PENDING_APPROVAL.value,
            detected_at=_dt.utcnow(),
        )
        session.add(incident)
        session.flush()
        session.add(
            Action(
                incident_id=incident.id,
                action_id="deploy_cluster_cephadm",
                classification="RISKY",
                status=ActionStatus.PENDING_APPROVAL.value,
                target_nodes=json.dumps(["10.20.1.112"]),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.post("/restore-cluster/propose", json=_valid_payload())
    assert response.status_code == 409


def test_pending_action_shows_approve_reject_buttons(dashboard_client):
    _login(dashboard_client)
    propose = dashboard_client.post("/restore-cluster/propose", json=_valid_payload())
    action_pk = propose.json()["action_id"]

    response = dashboard_client.get("/restore-cluster")

    assert response.status_code == 200
    assert f"/actions/{action_pk}/approve" in response.text
    assert f"/actions/{action_pk}/reject" in response.text


def test_progress_api_returns_null_when_no_action_exists(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/restore-cluster/progress")
    assert response.json() == {"status": None, "progress": []}


def test_progress_api_returns_latest_action_progress(dashboard_client):
    _login(dashboard_client)
    dashboard_client.post("/restore-cluster/propose", json=_valid_payload())

    with db_module.SessionLocal() as session:
        action = (
            session.query(Action).filter(Action.action_id == "restore_cluster_from_backup").first()
        )
        action.status = ActionStatus.APPROVED.value
        action.execution_progress = json.dumps([{"step": "ssh_check", "status": "running", "pct": 10}])
        session.commit()

    response = dashboard_client.get("/restore-cluster/progress")
    data = response.json()
    assert data["status"] == "APPROVED"
    assert data["progress"][0]["step"] == "ssh_check"
