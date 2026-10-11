"""Add Nodes page (dashboard/routes/add_nodes.py)."""

import json

import pytest

from config.settings import settings
from shared import db as db_module
from shared.models import Action


@pytest.fixture
def admin(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    return dashboard_client


def test_a_proposal_runs_through_an_existing_mon_and_needs_the_typed_ips(admin):
    assert "Thêm node" in admin.get("/add-nodes").text

    response = admin.post("/add-nodes/propose", json={
        "nodes": [{"ip": "10.30.0.9", "osd_disks": ["/dev/vdb"]}, {"ip": "10.30.0.10", "osd_disks": []}],
        "confirmation": "THÊM 10.30.0.9, 10.30.0.10"})

    assert response.status_code == 201, response.text
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        params = json.loads(action.action_params)
        assert (action.action_id, action.classification) == ("add_cluster_nodes", "RISKY")
        assert params["nodes"] == [{"ip": "10.30.0.9", "roles": ["osd"], "osd_disks": ["/dev/vdb"]},
                                   {"ip": "10.30.0.10", "roles": [], "osd_disks": []}]
        assert params["_admin_mon"] and "ceph orch host add" in action.proposed_command


@pytest.mark.parametrize("body", [
    {"nodes": [{"ip": "10.20.1.83", "osd_disks": []}], "confirmation": "THÊM 10.20.1.83"},     # already a node
    {"nodes": [{"ip": "not-an-ip"}], "confirmation": "THÊM not-an-ip"},
    {"nodes": [{"ip": "10.30.0.9", "osd_disks": ["vdb"]}], "confirmation": "THÊM 10.30.0.9"},  # not /dev/...
    {"nodes": [{"ip": "10.30.0.9"}], "confirmation": "THÊM 10.30.0.8"},
    {"nodes": [], "confirmation": "THÊM "},
])
def test_bad_proposals_are_refused(admin, body):
    assert admin.post("/add-nodes/propose", json=body).status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Action).count() == 0
