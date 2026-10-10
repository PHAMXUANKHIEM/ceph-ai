"""Replace OSD page (dashboard/routes/replace_osd.py)."""

import json

import pytest

from config.settings import settings
from dashboard.routes import replace_osd
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident

CRUSH = {"crush": {"roots": [{"type": "root", "name": "default", "children": [
    {"type": "host", "name": "ceph1", "children": [{"type": "osd", "id": 0}]},
    {"type": "host", "name": "ceph2", "children": [{"type": "osd", "id": 1}]},
]}]}}


@pytest.fixture
def admin(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(replace_osd.cluster_snapshot, "read_section_snapshot",
                        lambda cluster_id, section: CRUSH if section == "crush" else None)
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    return dashboard_client


def test_the_page_lists_the_clusters_osds_from_the_crush_snapshot(admin):
    page = admin.get("/replace-osd").text
    assert "osd.0 (ceph1)" in page and "osd.1 (ceph2)" in page and "Thay OSD hỏng" in page


def test_a_proposal_is_destructive_and_needs_the_typed_confirmation(admin):
    response = admin.post("/replace-osd/propose", json={"osd_id": 1, "confirmation": "THAY osd.1"})

    assert response.status_code == 201, response.text
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        params = json.loads(action.action_params)
        assert (action.action_id, action.classification) == ("replace_failed_osd", "DESTRUCTIVE")
        assert params["osd_id"] == 1 and params["_approval_confirmation"] == "THAY osd.1"
        assert session.get(Incident, action.incident_id).ceph_code == "OSD_REPLACE"
        assert "ceph orch osd rm 1 --replace" in action.proposed_command
        action_pk = action.id
    wrong = admin.post(f"/actions/{action_pk}/approve", data={"confirm_text": "yes"}, follow_redirects=False)
    assert wrong.status_code == 400


@pytest.mark.parametrize("body", [
    {"osd_id": 7, "confirmation": "THAY osd.7"},      # not in this cluster's CRUSH tree
    {"osd_id": 1, "confirmation": "THAY osd.0"},      # wrong confirmation
    {"confirmation": "THAY osd."},
])
def test_bad_proposals_are_refused(admin, body):
    assert admin.post("/replace-osd/propose", json=body).status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Action).count() == 0


def test_finishing_uses_the_host_and_disk_the_first_step_recorded(admin):
    created = admin.post("/replace-osd/propose", json={"osd_id": 1, "confirmation": "THAY osd.1"}).json()
    with db_module.SessionLocal() as session:
        action = session.get(Action, created["action_id"])
        action.status = ActionStatus.EXECUTED.value
        session.get(Incident, action.incident_id).status = "RESOLVED"  # the Worker closes it after running
        action.execution_progress = json.dumps([{"step": "osd_preflight", "status": "done", "hosts": [
            {"host": "osd.1", "status": "done", "message": "ok", "osd_host": "ceph2", "osd_device": "/dev/vdb"}]}])
        session.commit()
    assert "chờ ổ mới" in admin.get("/replace-osd").text

    response = admin.post("/replace-osd/finish", json={"device": "/dev/vdc", "confirmation": "THAY osd.1"})

    assert response.status_code == 201, response.text
    with db_module.SessionLocal() as session:
        finish = session.get(Action, response.json()["action_id"])
        params = json.loads(finish.action_params)
        assert finish.action_id == "finish_replace_osd" and finish.classification == "DESTRUCTIVE"
        assert (params["_hostname"], params["_device"], params["device"]) == ("ceph2", "/dev/vdb", "/dev/vdc")
        assert "ceph orch device zap ceph2 /dev/vdc --force" in finish.proposed_command


def test_finishing_needs_a_previous_replacement(admin):
    response = admin.post("/replace-osd/finish", json={"confirmation": "THAY osd.1"})
    assert response.status_code == 400
