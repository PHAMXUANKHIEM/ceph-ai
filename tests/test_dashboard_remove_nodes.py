"""Remove Nodes page (dashboard/routes/remove_nodes.py)."""

import json

import pytest

from config.settings import settings
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident


@pytest.fixture
def admin(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    return dashboard_client


def _propose(client, targets, confirmation=None, **extra):
    body = {"targets": targets, "confirmation": confirmation or "GỠ " + ", ".join(targets), **extra}
    return client.post("/remove-nodes/propose", json=body)


def test_the_page_lists_the_selected_clusters_nodes(admin):
    page = admin.get("/remove-nodes").text
    assert "10.20.1.83" in page and "10.20.1.150" in page and "Remove Nodes" in page


def test_a_proposal_is_destructive_and_needs_the_typed_ips_to_approve(admin):
    response = _propose(admin, ["10.20.1.83"], zap_devices=True)

    assert response.status_code == 201, response.text
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        params = json.loads(action.action_params)
        assert action.action_id == "remove_cluster_nodes" and action.classification == "DESTRUCTIVE"
        assert params["targets"] == ["10.20.1.83"] and params["zap_devices"] is True
        assert params["_approval_confirmation"] == "GỠ 10.20.1.83" and params["_node_config_fingerprint"]
        assert session.get(Incident, action.incident_id).ceph_code == "CLUSTER_NODE_REMOVE"
        action_pk = action.id
    assert "GỠ 10.20.1.83" in admin.get("/remove-nodes").text

    wrong = admin.post(f"/actions/{action_pk}/approve", data={"confirm_text": "yes"}, follow_redirects=False)
    assert wrong.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.PENDING_APPROVAL.value


@pytest.mark.parametrize(("targets", "confirmation"), [
    (["10.9.9.9"], None),                                        # not a node of this cluster
    (["10.20.1.150", "10.20.1.249", "10.20.1.253", "10.20.1.83", "10.20.1.78", "10.20.1.1"], None),  # every node
    (["10.20.1.83"], "GỠ 10.20.1.78"),                           # wrong confirmation
    ([], None),
])
def test_bad_proposals_are_refused(admin, targets, confirmation):
    assert _propose(admin, targets, confirmation).status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Action).count() == 0


def test_only_cephadm_clusters_and_only_a_waiting_drain_can_be_finished(admin, monkeypatch):
    assert admin.post("/remove-nodes/finish").status_code == 400
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    assert _propose(admin, ["10.20.1.83"]).status_code == 400


def test_a_drain_left_running_offers_the_finish_step(admin):
    created = _propose(admin, ["10.20.1.83"]).json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, created)
        action.status = ActionStatus.EXECUTED.value
        session.get(Incident, action.incident_id).status = "RESOLVED"  # the Worker closes it after running
        action.execution_progress = json.dumps([{"step": "rm_host", "label": "Gỡ host", "status": "done",
                                                 "hosts": [{"host": "10.20.1.83", "message": "chưa gỡ: chờ drain xong"}]}])
        session.commit()

    assert "Hoàn tất gỡ node" in admin.get("/remove-nodes").text
    finish = admin.post("/remove-nodes/finish")
    assert finish.status_code == 201, finish.text
    with db_module.SessionLocal() as session:
        action = session.get(Action, finish.json()["action_id"])
        assert action.action_id == "finish_remove_cluster_nodes" and json.loads(action.action_params)["targets"] == ["10.20.1.83"]
