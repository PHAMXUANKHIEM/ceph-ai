import json
from datetime import datetime

import dashboard.routes.convert_cluster as convert_cluster_route
from config.settings import settings
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from watcher.ceph_client import CephQueryError


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure_mgr(monkeypatch):
    # conftest.py's autouse fixture pins TEST_CEPH_MGR_NODES to "" (blank by
    # default, same "opt-in" posture as ceph_rbd_pools) — this feature
    # requires at least 1 configured MGR node, so tests exercising a real
    # propose need to set one explicitly.
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "10.20.1.150")


def _stub_version(monkeypatch, version="18.2.8", is_mixed=False):
    monkeypatch.setattr(
        convert_cluster_route.ceph_client,
        "summarize_cluster_versions",
        lambda: {
            "raw": {},
            "per_type": {},
            "distinct_versions": [version] if not is_mixed else [version, "17.2.8"],
            "is_mixed": is_mixed,
            "current_version": None if is_mixed else version,
        },
    )


def test_unauthenticated_get_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/convert-cluster", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_page_shows_configured_nodes_when_eligible(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _login(dashboard_client)
    response = dashboard_client.get("/convert-cluster")
    assert response.status_code == 200
    # TEST_CEPH_MON_NODES/TEST_CEPH_OSD_NODES from conftest.py's autouse fixture.
    assert "10.20.1.150" in response.text
    assert "Đề xuất chuyển đổi sang cephadm" in response.text


def test_get_page_shows_ineligible_when_already_cephadm(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    _login(dashboard_client)
    response = dashboard_client.get("/convert-cluster")
    assert response.status_code == 200
    assert "đã chạy cephadm rồi" in response.text


def test_get_page_shows_ineligible_when_no_cluster_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    _login(dashboard_client)
    response = dashboard_client.get("/convert-cluster")
    assert response.status_code == 200
    assert "Chưa có node MON/MGR/OSD" in response.text


def test_propose_creates_pending_action(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _configure_mgr(monkeypatch)
    _stub_version(monkeypatch, "18.2.8")
    _login(dashboard_client)

    response = dashboard_client.post("/convert-cluster/propose", json={})

    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.action_id == "convert_cluster_to_cephadm"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == "RISKY"
        params = json.loads(action.action_params)
        assert params["version"] == "18.2.8"
        node_ips = {n["ip"] for n in params["nodes"]}
        assert "10.20.1.150" in node_ips

        incident = session.get(Incident, action.incident_id)
        assert incident.ceph_code == "CONVERT_CLUSTER_TO_CEPHADM"
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value


def test_propose_rejects_when_exec_mode_is_not_none(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "docker")
    _login(dashboard_client)

    response = dashboard_client.post("/convert-cluster/propose", json={})
    assert response.status_code == 400
    assert "systemd" in response.json()["detail"]


def test_propose_rejects_when_no_cluster_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    _login(dashboard_client)

    response = dashboard_client.post("/convert-cluster/propose", json={})
    assert response.status_code == 400


def test_propose_rejects_when_version_is_mixed(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _configure_mgr(monkeypatch)
    _stub_version(monkeypatch, is_mixed=True)
    _login(dashboard_client)

    response = dashboard_client.post("/convert-cluster/propose", json={})
    assert response.status_code == 400
    assert "phiên bản" in response.json()["detail"]


def test_propose_rejects_when_version_query_fails(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _configure_mgr(monkeypatch)

    def broken():
        raise CephQueryError("all MON nodes unreachable")

    monkeypatch.setattr(convert_cluster_route.ceph_client, "summarize_cluster_versions", broken)
    _login(dashboard_client)

    response = dashboard_client.post("/convert-cluster/propose", json={})
    assert response.status_code == 502


def test_propose_rejects_second_proposal_while_one_pending(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _configure_mgr(monkeypatch)
    _stub_version(monkeypatch)
    _login(dashboard_client)

    first = dashboard_client.post("/convert-cluster/propose", json={})
    assert first.status_code == 201

    second = dashboard_client.post("/convert-cluster/propose", json={})
    assert second.status_code == 409


def test_propose_rejects_while_a_deploy_is_pending(dashboard_client, monkeypatch):
    # Broader in-flight check than delete_cluster.py's own — convert also
    # refuses while ANY cluster-lifecycle action (deploy/delete/convert) is
    # already in flight, not just another convert.
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _configure_mgr(monkeypatch)
    _stub_version(monkeypatch)
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code="CLUSTER_DEPLOY",
            status=IncidentStatus.PENDING_APPROVAL.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        session.add(
            Action(
                incident_id=incident.id,
                action_id="deploy_cluster_cephadm",
                classification="RISKY",
                status=ActionStatus.PENDING_APPROVAL.value,
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post("/convert-cluster/propose", json={})
    assert response.status_code == 409


def test_progress_endpoint_returns_null_status_with_no_action(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/convert-cluster/progress")
    assert response.status_code == 200
    assert response.json() == {"status": None, "progress": []}


def test_progress_endpoint_formats_real_timestamps_as_vietnam_local_clock(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    _configure_mgr(monkeypatch)
    _stub_version(monkeypatch)
    _login(dashboard_client)
    propose = dashboard_client.post("/convert-cluster/propose", json={})
    action_pk = propose.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        action.status = ActionStatus.APPROVED.value
        action.execution_progress = json.dumps(
            [
                {
                    "step": "ssh_check",
                    "status": "done",
                    "pct": 5,
                    "started_at": "2026-07-28T03:29:30",
                    "finished_at": "2026-07-28T03:29:45",
                },
                {"step": "health_precheck", "status": "pending", "pct": 10},
            ]
        )
        session.commit()

    first = dashboard_client.get("/convert-cluster/progress").json()
    second = dashboard_client.get("/convert-cluster/progress").json()

    for body in (first, second):
        done_step = body["progress"][0]
        assert done_step["started_at_display"] == "10:29:30"  # 03:29 UTC -> 10:29 ICT
        assert done_step["finished_at_display"] == "10:29:45"
        pending_step = body["progress"][1]
        assert pending_step["started_at_display"] is None
        assert pending_step["finished_at_display"] is None
    assert first["progress"][0]["finished_at_display"] == second["progress"][0]["finished_at_display"]
