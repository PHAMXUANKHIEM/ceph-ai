"""Node Maintenance page (dashboard/routes/node_maintenance.py)."""

import json

import pytest

from config.settings import settings
from shared import db as db_module
from shared.models import Action, Incident


@pytest.fixture
def admin(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    return dashboard_client


def test_a_proposal_keeps_the_chosen_order_and_needs_the_typed_ips(admin):
    assert "Bảo trì node" in admin.get("/node-maintenance").text

    response = admin.post("/node-maintenance/propose", json={
        "targets": ["10.20.1.83", "10.20.1.150"], "confirmation": "BẢO TRÌ 10.20.1.83, 10.20.1.150"})

    assert response.status_code == 201, response.text
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        params = json.loads(action.action_params)
        assert (action.action_id, action.classification) == ("rolling_node_maintenance", "DESTRUCTIVE")
        assert params["targets"] == ["10.20.1.83", "10.20.1.150"]
        assert session.get(Incident, action.incident_id).ceph_code == "NODE_MAINTENANCE"
        assert "maintenance enter" in action.proposed_command
        action_pk = action.id
    assert admin.post(f"/actions/{action_pk}/approve", data={"confirm_text": "ok"},
                      follow_redirects=False).status_code == 400


@pytest.mark.parametrize("body", [
    {"targets": ["10.9.9.9"], "confirmation": "BẢO TRÌ 10.9.9.9"},
    {"targets": ["10.20.1.83", "10.20.1.83"], "confirmation": "BẢO TRÌ 10.20.1.83, 10.20.1.83"},
    {"targets": ["10.20.1.83"], "confirmation": "BẢO TRÌ 10.20.1.150"},
    {"targets": [], "confirmation": "BẢO TRÌ "},
])
def test_bad_proposals_are_refused(admin, body):
    assert admin.post("/node-maintenance/propose", json=body).status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Action).count() == 0
