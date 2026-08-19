import json

from config.settings import settings
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident, IncidentStatus


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_unauthenticated_get_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/delete-cluster", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_page_shows_configured_nodes(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/delete-cluster")
    assert response.status_code == 200
    # TEST_CEPH_MON_NODES/TEST_CEPH_OSD_NODES from conftest.py's autouse fixture.
    assert "10.20.1.150" in response.text
    assert "10.20.1.83" in response.text


def test_get_page_with_no_configured_cluster_shows_empty_state(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    _login(dashboard_client)
    response = dashboard_client.get("/delete-cluster")
    assert response.status_code == 200
    assert "Chưa có cụm nào được cấu hình" in response.text


def test_propose_creates_pending_action_for_manual_exec_mode(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _login(dashboard_client)

    response = dashboard_client.post("/delete-cluster/propose", json={"wipe_osd_disks": False})

    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.action_id == "delete_cluster_manual"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        # AI roadmap Pha 0.4 (2026-08-18): moved risky: -> destructive: —
        # irreversibly tears down a real cluster. Always required explicit
        # approval either way (unchanged above); stricter label only.
        assert action.classification == "DESTRUCTIVE"
        params = json.loads(action.action_params)
        assert params["wipe_osd_disks"] is False
        node_ips = {n["ip"] for n in params["nodes"]}
        assert "10.20.1.150" in node_ips
        assert "10.20.1.83" in node_ips

        incident = session.get(Incident, action.incident_id)
        assert incident.ceph_code == "CLUSTER_DELETE"
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value

        # KHÔNG xoá dữ liệu đĩa OSD must be stated plainly when not requested.
        assert "KHÔNG xoá" in action.rationale


def test_propose_creates_pending_action_for_cephadm_exec_mode(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    _login(dashboard_client)

    response = dashboard_client.post("/delete-cluster/propose", json={"wipe_osd_disks": False})

    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.action_id == "delete_cluster_cephadm"
        # AI roadmap Pha 0.4 (2026-08-18): see the manual-exec-mode test
        # above for why this is DESTRUCTIVE now, not RISKY.
        assert action.classification == "DESTRUCTIVE"


def test_propose_with_wipe_requires_osd_disk_per_node(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/delete-cluster/propose", json={"wipe_osd_disks": True, "osd_disks": {}}
    )
    assert response.status_code == 400
    assert "đĩa OSD" in response.json()["detail"]


def test_propose_with_wipe_and_valid_disks_succeeds(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _login(dashboard_client)

    osd_disks = {ip: ["/dev/vdc"] for ip in settings.ceph_osd_nodes.split(",")}
    response = dashboard_client.post(
        "/delete-cluster/propose", json={"wipe_osd_disks": True, "osd_disks": osd_disks}
    )
    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        params = json.loads(action.action_params)
        assert params["wipe_osd_disks"] is True
        disks = {n["ip"]: n["osd_disks"] for n in params["nodes"] if "osd" in n["roles"]}
        assert all(v == ["/dev/vdc"] for v in disks.values())
        assert "CÓ xoá" in action.rationale
        assert "KHÔNG THỂ HOÀN TÁC" in action.rationale


def test_propose_with_wipe_allows_multiple_disks_on_same_node(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _login(dashboard_client)

    osd_nodes = settings.ceph_osd_nodes.split(",")
    osd_disks = {ip: ["/dev/vdc", "/dev/vdd"] for ip in osd_nodes}
    response = dashboard_client.post(
        "/delete-cluster/propose", json={"wipe_osd_disks": True, "osd_disks": osd_disks}
    )
    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        params = json.loads(action.action_params)
        disks = {n["ip"]: n["osd_disks"] for n in params["nodes"] if "osd" in n["roles"]}
        assert all(v == ["/dev/vdc", "/dev/vdd"] for v in disks.values())


def test_propose_with_wipe_rejects_duplicate_disk_on_same_node(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _login(dashboard_client)

    osd_nodes = settings.ceph_osd_nodes.split(",")
    osd_disks = {ip: ["/dev/vdc", "/dev/vdc"] for ip in osd_nodes}
    response = dashboard_client.post(
        "/delete-cluster/propose", json={"wipe_osd_disks": True, "osd_disks": osd_disks}
    )
    assert response.status_code == 400
    assert "trùng lặp" in response.json()["detail"]


def test_propose_rejects_when_no_cluster_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    _login(dashboard_client)

    response = dashboard_client.post("/delete-cluster/propose", json={"wipe_osd_disks": False})
    assert response.status_code == 400


def test_propose_rejects_second_proposal_while_one_pending(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _login(dashboard_client)

    first = dashboard_client.post("/delete-cluster/propose", json={"wipe_osd_disks": False})
    assert first.status_code == 201

    second = dashboard_client.post("/delete-cluster/propose", json={"wipe_osd_disks": False})
    assert second.status_code == 409


def test_progress_endpoint_returns_null_status_with_no_action(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/delete-cluster/progress")
    assert response.status_code == 200
    assert response.json() == {"status": None, "progress": []}


def test_progress_endpoint_formats_real_timestamps_as_vietnam_local_clock(dashboard_client, monkeypatch):
    # 2026-07-28 regression test — same "time keeps changing after a step
    # finished" fix as Deploy Cluster's identical bug (delete_cluster.js
    # shared the same JS, and delete_cluster.py's progress comes from the
    # same worker/executor/cluster_deploy.py::run()). A step with a real
    # frozen finished_at must come back as a fixed HH:MM:SS on every poll.
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _login(dashboard_client)
    propose = dashboard_client.post("/delete-cluster/propose", json={"wipe_osd_disks": False})
    action_pk = propose.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        action.status = ActionStatus.APPROVED.value
        action.execution_progress = json.dumps(
            [
                {
                    "step": "ssh_check",
                    "status": "done",
                    "pct": 10,
                    "started_at": "2026-07-28T03:29:30",
                    "finished_at": "2026-07-28T03:29:45",
                },
                {"step": "stop_daemons", "status": "pending", "pct": 35},
            ]
        )
        session.commit()

    first = dashboard_client.get("/delete-cluster/progress").json()
    second = dashboard_client.get("/delete-cluster/progress").json()

    for body in (first, second):
        done_step = body["progress"][0]
        assert done_step["started_at_display"] == "10:29:30"  # 03:29 UTC -> 10:29 ICT
        assert done_step["finished_at_display"] == "10:29:45"
        pending_step = body["progress"][1]
        assert pending_step["started_at_display"] is None
        assert pending_step["finished_at_display"] is None
    assert first["progress"][0]["finished_at_display"] == second["progress"][0]["finished_at_display"]
