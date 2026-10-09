import json
from pathlib import Path

import bcrypt
from sqlalchemy.exc import OperationalError

import dashboard.routes.deploy_cluster as deploy_cluster_route
from dashboard.routes import incidents as incidents_route
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident, IncidentStatus, User


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _create_user(username, password, *, is_admin=False):
    with db_module.SessionLocal() as session:
        session.add(
            User(
                username=username,
                password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                is_admin=is_admin,
                is_active=True,
                created_by="admin",
            )
        )
        session.commit()


def _login_as(client, username, password):
    client.post("/login", data={"username": username, "password": password})


def _valid_payload(**overrides):
    payload = {
        "version": "18.2.8",
        "method": "cephadm",
        "nodes": [
            {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
            # Different disk names per node (node1 /dev/vdc, node2 /dev/vdb)
            # — osd_disks is per node, not one cluster-wide value.
            {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disks": ["/dev/vdc"]},
            {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disks": ["/dev/vdb"]},
        ],
        "public_network": "10.20.1.0/24",
        "cluster_network": "10.20.1.0/24",
        "osd_pool_default_size": 3,
        "osd_pool_default_min_size": 2,
        "osd_encryption": False,
    }
    payload.update(overrides)
    return payload


def test_unauthenticated_get_deploy_cluster_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/deploy-cluster", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_propose_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/deploy-cluster/propose", json=_valid_payload(), follow_redirects=False
    )
    assert response.status_code == 303


def test_get_deploy_cluster_shows_form(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/deploy-cluster")
    assert response.status_code == 200
    assert "deploy-form" in response.text
    assert "Bắt đầu cài đặt" in response.text
    assert response.text.count('id="df-codename"') == 1
    assert response.text.count('id="df-version-select"') == 1
    assert 'id="deploy-confirm-dialog"' in response.text
    assert 'id="df-log-search"' in response.text
    assert 'data-log-filter="warning"' in response.text
    assert 'id="df-ssh-key-path"' not in response.text


def test_get_deploy_cluster_handles_db_error_gracefully(dashboard_client, monkeypatch):
    _login(dashboard_client)

    def _broken_session_local():
        raise OperationalError("SELECT 1", {}, Exception("no such table: actions"))

    monkeypatch.setattr(db_module, "SessionLocal", _broken_session_local)
    response = dashboard_client.get("/deploy-cluster")

    assert response.status_code == 503


def test_propose_creates_pending_action_for_cephadm(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())

    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.action_id == "deploy_cluster_cephadm"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == "RISKY"
        params = json.loads(action.action_params)
        assert params["version"] == "18.2.8"
        assert params["osd_encryption"] is False
        assert "dm-crypt/LUKS: TẮT" in action.rationale
        assert len(params["nodes"]) == 3
        target_nodes = json.loads(action.target_nodes)
        assert target_nodes == ["10.20.1.112", "10.20.1.95", "10.20.1.21"]

        incident = session.get(Incident, action.incident_id)
        assert incident.ceph_code == "CLUSTER_DEPLOY"
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value


def test_propose_persists_osd_encryption_for_operator_approval(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose", json=_valid_payload(osd_encryption=True)
    )
    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        assert json.loads(action.action_params)["osd_encryption"] is True
        assert "dm-crypt/LUKS: BẬT" in action.rationale


def test_propose_rejects_non_boolean_osd_encryption(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose", json=_valid_payload(osd_encryption="yes")
    )
    assert response.status_code == 400


def test_propose_rejects_invalid_version(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(version="not-a-version"))
    assert response.status_code == 400
    assert "Phiên bản" in response.json()["detail"]


def test_propose_creates_pending_action_for_ceph_deploy(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(method="ceph-deploy"))

    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.action_id == "deploy_cluster_ceph_deploy"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == "RISKY"
        # AC #4/#6: the plan text must document the stop-on-first-failure
        # posture (contrasted against the continue-on-failure package
        # upgrade path) and the real disk write OSD creation performs.
        assert "DỪNG LẠI NGAY" in action.rationale
        assert "ceph-volume lvm create" in action.rationale
        assert "GHI/ĐỊNH DẠNG THẬT" in action.rationale


def test_propose_creates_pending_action_for_rpm_local(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post(
        "/deploy-cluster/propose", json=_valid_payload(method="rpm-local", rpm_path="/opt/ceph-rpms")
    )

    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.action_id == "deploy_cluster_rpm_local"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == "RISKY"
        params = json.loads(action.action_params)
        assert params["rpm_path"] == "/opt/ceph-rpms"
        # AC #5: the plan text must state plainly that nothing is downloaded,
        # and must name createrepo/dpkg-scanpackages instead of the
        # download.ceph.com repo the other two methods use.
        assert "KHÔNG có gì được tải từ Internet" in action.rationale
        assert "/opt/ceph-rpms" in action.rationale
        assert "KHÔNG thêm repo download.ceph.com" in action.rationale


def test_propose_rejects_rpm_local_missing_rpm_path(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose", json=_valid_payload(method="rpm-local", rpm_path="")
    )
    assert response.status_code == 400
    assert "RPM" in response.json()["detail"]


def test_propose_rejects_rpm_local_relative_rpm_path(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose", json=_valid_payload(method="rpm-local", rpm_path="opt/ceph-rpms")
    )
    assert response.status_code == 400
    assert "RPM" in response.json()["detail"]


def test_propose_rejects_unknown_method(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(method="docker-compose"))
    assert response.status_code == 400


def test_propose_rejects_missing_mon(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose",
        json=_valid_payload(nodes=[{"ip": "10.20.1.112", "roles": ["mgr", "osd"]}]),
    )
    assert response.status_code == 400
    assert "MON" in response.json()["detail"]


def test_propose_rejects_missing_mgr(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose",
        json=_valid_payload(nodes=[{"ip": "10.20.1.112", "roles": ["mon", "osd"]}]),
    )
    assert response.status_code == 400
    assert "MGR" in response.json()["detail"]


def test_propose_rejects_missing_osd(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose",
        json=_valid_payload(nodes=[{"ip": "10.20.1.112", "roles": ["mon", "mgr"]}]),
    )
    assert response.status_code == 400
    assert "OSD" in response.json()["detail"]


def test_propose_rejects_duplicate_ip(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose",
        json=_valid_payload(
            nodes=[
                {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
                {"ip": "10.20.1.112", "roles": ["osd"]},
            ]
        ),
    )
    assert response.status_code == 400
    assert "trùng" in response.json()["detail"]


def test_propose_rejects_invalid_ip(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose",
        json=_valid_payload(nodes=[{"ip": "999.1.1.1", "roles": ["mon", "mgr", "osd"]}]),
    )
    assert response.status_code == 400
    assert "IP không hợp lệ" in response.json()["detail"]


def test_propose_rejects_empty_node_list(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(nodes=[]))
    assert response.status_code == 400


def test_propose_rejects_invalid_osd_disk(dashboard_client):
    _login(dashboard_client)
    bad_nodes = [
        {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
        {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disks": ["vdc"]},
        {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disks": ["/dev/vdb"]},
    ]
    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(nodes=bad_nodes))
    assert response.status_code == 400
    assert "Đĩa OSD" in response.json()["detail"]


def test_propose_rejects_missing_osd_disk_on_osd_node(dashboard_client):
    _login(dashboard_client)
    bad_nodes = [
        {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
        {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"]},
        {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disks": ["/dev/vdb"]},
    ]
    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(nodes=bad_nodes))
    assert response.status_code == 400
    assert "đĩa OSD" in response.json()["detail"]
    assert "10.20.1.95" in response.json()["detail"]


def test_propose_rejects_empty_osd_disk_list_on_osd_node(dashboard_client):
    _login(dashboard_client)
    bad_nodes = [
        {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
        {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disks": []},
        {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disks": ["/dev/vdb"]},
    ]
    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(nodes=bad_nodes))
    assert response.status_code == 400
    assert "đĩa OSD" in response.json()["detail"]


def test_propose_rejects_duplicate_osd_disk_on_same_node(dashboard_client):
    _login(dashboard_client)
    bad_nodes = [
        {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
        {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disks": ["/dev/vdc", "/dev/vdc"]},
        {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disks": ["/dev/vdb"]},
    ]
    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(nodes=bad_nodes))
    assert response.status_code == 400
    assert "trùng lặp" in response.json()["detail"]


def test_propose_allows_different_osd_disk_names_per_node(dashboard_client):
    """The whole point of this feature: node1 can use /dev/vdc while node2
    uses /dev/vdb."""
    _login(dashboard_client)
    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())
    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        params = json.loads(action.action_params)
        disks_by_ip = {n["ip"]: n["osd_disks"] for n in params["nodes"] if "osd" in n["roles"]}
        assert disks_by_ip == {"10.20.1.95": ["/dev/vdc"], "10.20.1.21": ["/dev/vdb"]}
        # The plan text must name EACH node's own disk, not one shared value.
        assert "10.20.1.95 (/dev/vdc)" in action.rationale
        assert "10.20.1.21 (/dev/vdb)" in action.rationale


def test_propose_allows_multiple_osd_disks_on_the_same_node(dashboard_client):
    """A single node can carry more than one OSD disk (e.g. /dev/vdc AND
    /dev/vdd on the same node, each becoming its own OSD)."""
    _login(dashboard_client)
    multi_disk_nodes = [
        {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
        {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disks": ["/dev/vdc", "/dev/vdd"]},
        {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disks": ["/dev/vdb"]},
    ]
    response = dashboard_client.post(
        "/deploy-cluster/propose", json=_valid_payload(nodes=multi_disk_nodes)
    )
    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        params = json.loads(action.action_params)
        disks_by_ip = {n["ip"]: n["osd_disks"] for n in params["nodes"] if "osd" in n["roles"]}
        assert disks_by_ip == {"10.20.1.95": ["/dev/vdc", "/dev/vdd"], "10.20.1.21": ["/dev/vdb"]}
        assert "10.20.1.95 (/dev/vdc, /dev/vdd)" in action.rationale


def test_propose_rejects_second_deploy_while_one_in_flight(dashboard_client):
    _login(dashboard_client)
    first = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())
    assert first.status_code == 201

    second = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())
    assert second.status_code == 409


def test_propose_allows_new_deploy_after_previous_one_resolved(dashboard_client):
    _login(dashboard_client)
    first = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())
    action_pk = first.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        action.status = ActionStatus.EXECUTED.value
        session.commit()

    second = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())
    assert second.status_code == 201


def test_progress_endpoint_no_action_returns_null_status(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/deploy-cluster/progress")
    assert response.status_code == 200
    assert response.json() == {"status": None, "progress": []}


def test_progress_endpoint_returns_latest_action_status_and_progress(dashboard_client):
    _login(dashboard_client)
    propose = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())
    action_pk = propose.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        action.status = ActionStatus.APPROVED.value
        action.execution_progress = json.dumps([{"step": "ssh_check", "status": "running", "pct": 10}])
        session.commit()

    response = dashboard_client.get("/deploy-cluster/progress")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "APPROVED"
    assert body["action_id"] == action_pk
    # 2026-07-28: the route now also annotates each step with
    # started_at_display/finished_at_display (deploy_cluster.py::
    # _with_step_display_times) — None here since this step dict has no
    # real started_at/finished_at (it was written directly by the test,
    # not by cluster_deploy.py's run()).
    assert body["progress"] == [
        {
            "step": "ssh_check",
            "status": "running",
            "pct": 10,
            "started_at_display": None,
            "finished_at_display": None,
        }
    ]


def test_failed_deploy_without_saved_progress_shows_target_safety_reason(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        deploy_cluster_route.env_config,
        "current_cluster_config_fingerprint",
        lambda: "stable-test-config",
    )
    monkeypatch.setattr(
        deploy_cluster_route,
        "configured_nodes",
        lambda: [{"host": "10.20.1.10", "roles": ["MON"]}],
    )
    _login(dashboard_client)
    response = dashboard_client.post(
        "/deploy-cluster/propose",
        json=_valid_payload(method="ceph-deploy"),
    )
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        params = json.loads(action.action_params)
        params["_cluster_config_fingerprint"] = "stable-test-config"
        action.action_params = json.dumps(params)
        action.status = ActionStatus.FAILED.value
        action.execution_progress = None
        session.commit()

    progress_response = dashboard_client.get("/deploy-cluster/progress")
    body = progress_response.json()
    assert body["status"] == "FAILED"
    assert body["progress"][0]["step"] == "preflight"
    assert "10.20.1.112, 10.20.1.95, 10.20.1.21" in body["progress"][0]["message"]
    assert "không thuộc cấu hình cluster hiện tại" in body["progress"][0]["message"]

    page = dashboard_client.get("/deploy-cluster")
    state_json = page.text.split(
        '<script type="application/json" id="deploy-initial-state">', 1
    )[1].split("</script>", 1)[0].strip()
    initial_state = json.loads(state_json)
    assert initial_state["progress"][0]["label"] == "Kiểm tra an toàn trước triển khai"
    assert "không thuộc cấu hình cluster hiện tại" in initial_state["progress"][0]["message"]


def test_progress_endpoint_formats_real_timestamps_as_vietnam_local_clock(dashboard_client):
    # 2026-07-28 regression test for the "time keeps changing after a step
    # finished" bug — a step with a REAL frozen finished_at (as
    # worker/executor/cluster_deploy.py's run() now writes) must come back
    # as a fixed HH:MM:SS, not recomputed from "now" on every poll.
    _login(dashboard_client)
    propose = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())
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
                {"step": "dependencies", "status": "pending", "pct": 20},
            ]
        )
        session.commit()

    first = dashboard_client.get("/deploy-cluster/progress").json()
    second = dashboard_client.get("/deploy-cluster/progress").json()

    for body in (first, second):
        done_step = body["progress"][0]
        assert done_step["started_at_display"] == "10:29:30"  # 03:29 UTC -> 10:29 ICT
        assert done_step["finished_at_display"] == "10:29:45"
        pending_step = body["progress"][1]
        assert pending_step["started_at_display"] is None
        assert pending_step["finished_at_display"] is None
    # Never recomputed from "now" between polls — same finished_at_display
    # both times, regardless of how much wall-clock time passed in between.
    assert first["progress"][0]["finished_at_display"] == second["progress"][0]["finished_at_display"]


def test_get_deploy_cluster_shows_pending_approval_plan(dashboard_client):
    _login(dashboard_client)
    dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())

    response = dashboard_client.get("/deploy-cluster")

    assert response.status_code == 200
    assert "Duyệt" in response.text
    assert "cephadm bootstrap" in response.text


def test_get_deploy_cluster_shows_executed_summary(dashboard_client):
    _login(dashboard_client)
    propose = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())
    action_pk = propose.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        action.status = ActionStatus.EXECUTED.value
        session.commit()

    response = dashboard_client.get("/deploy-cluster")

    assert response.status_code == 200
    assert "đã dựng thành công" in response.text
    assert "Xem Dashboard" in response.text
    assert "Khởi động lại ▾" in response.text
    assert 'id="deploy-restart-dialog"' in response.text
    # Deploying never rewrites the monitored cluster's .env since 06/10/2026.
    assert "đã được ghi vào .env" not in response.text
    assert "Cụm mới không được đăng ký giám sát" in response.text


def test_executed_summary_names_the_registered_monitoring_cluster(dashboard_client):
    _login(dashboard_client)
    payload = dict(_valid_payload(), register_monitoring=True, monitor_cluster_name="CS-NEW")
    action_pk = dashboard_client.post("/deploy-cluster/propose", json=payload).json()["action_id"]
    with db_module.SessionLocal() as session:
        session.get(Action, action_pk).status = ActionStatus.EXECUTED.value
        session.commit()

    page = dashboard_client.get("/deploy-cluster").text

    assert "cụm giám sát <strong>CS-NEW</strong>" in page and "không cần khởi động lại dịch vụ" in page


def test_get_deploy_cluster_shows_failed_summary(dashboard_client):
    _login(dashboard_client)
    propose = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())
    action_pk = propose.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        action.status = ActionStatus.FAILED.value
        session.commit()

    response = dashboard_client.get("/deploy-cluster")

    assert response.status_code == 200
    assert "thất bại" in response.text


def test_deploy_cluster_nav_link_present_on_other_pages(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/nodes")
    assert response.status_code == 200
    assert 'href="/deploy-cluster"' in response.text


# -- POST /deploy-cluster/provision-host-key -------------------------------

def test_provision_host_key_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post(
        "/deploy-cluster/provision-host-key",
        json={"host": "10.3.55.98", "host_key": "ssh-ed25519 AAAAverified"},
    )

    assert response.status_code == 403


def test_provision_host_key_route_stores_only_operator_supplied_verified_key(dashboard_client, monkeypatch):
    _login(dashboard_client)
    calls = []
    monkeypatch.setattr(
        deploy_cluster_route,
        "provision_host_key",
        lambda host, host_key, note="", added_by="": calls.append((host, host_key, note, added_by)) or "ssh-ed25519",
    )

    response = dashboard_client.post(
        "/deploy-cluster/provision-host-key",
        json={"host": "10.3.55.98", "host_key": "ssh-ed25519 AAAAverified"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert calls == [("10.3.55.98", "ssh-ed25519 AAAAverified", "", "admin")]


def test_provision_host_key_route_rejects_invalid_key(dashboard_client, monkeypatch):
    _login(dashboard_client)
    monkeypatch.setattr(
        deploy_cluster_route,
        "provision_host_key",
        lambda _host, _key, _note="", _added_by="": (_ for _ in ()).throw(deploy_cluster_route.HostKeyProvisionError("key sai")),
    )

    response = dashboard_client.post(
        "/deploy-cluster/provision-host-key",
        json={"host": "10.3.55.98", "host_key": "not-a-key"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "key sai"


# -- POST /deploy-cluster/forget-host-key ------------------------------------
# "Xoá SSH host key cũ" moved here from the always-visible Settings-page
# form — it's now hidden until deploy_cluster.js's HOST_KEY_MISMATCH_RE
# detects the exact paramiko BadHostKeyException wording in a failed
# _phase_ssh_check step and renders the button inline, right where an
# operator is already looking at the failure.


def test_forget_host_key_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/deploy-cluster/forget-host-key", json={"host": "10.3.55.98"})

    assert response.status_code == 403


def test_forget_host_key_route_blank_host_returns_400(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/deploy-cluster/forget-host-key", json={"host": "  "})

    assert response.status_code == 400


def test_forget_host_key_route_success(dashboard_client, monkeypatch):
    _login(dashboard_client)
    monkeypatch.setattr(deploy_cluster_route, "forget_host_key", lambda host: True)

    response = dashboard_client.post("/deploy-cluster/forget-host-key", json={"host": "10.3.55.98"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "10.3.55.98" in body["message"]


def test_forget_host_key_route_no_stored_entry(dashboard_client, monkeypatch):
    _login(dashboard_client)
    monkeypatch.setattr(deploy_cluster_route, "forget_host_key", lambda host: False)

    response = dashboard_client.post("/deploy-cluster/forget-host-key", json={"host": "10.3.55.98"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "Không tìm thấy" in body["message"]


def test_deploy_cluster_page_initial_state_includes_is_admin(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/deploy-cluster")

    assert response.status_code == 200
    assert '"is_admin": true' in response.text


def test_deploy_cluster_page_shows_worker_public_key_only(dashboard_client, tmp_path, monkeypatch):
    _login(dashboard_client)
    public_key = "ssh-ed25519 AAA= ceph-ai-deploy"
    key_file = tmp_path / "worker-deploy.pub"
    key_file.write_text(public_key)
    monkeypatch.setattr(deploy_cluster_route.settings, "deploy_ssh_public_key_path", str(key_file))

    response = dashboard_client.get("/deploy-cluster")

    assert response.status_code == 200
    assert public_key in response.text
    assert "Copy public key" in response.text
    assert "Private key được giữ trong Worker" in response.text


def test_deploy_cluster_frontend_does_not_reload_for_terminal_history():
    source = (
        Path(__file__).resolve().parents[1] / "dashboard/static/deploy_cluster.js"
    ).read_text(encoding="utf-8")

    assert "var followAction = FOLLOW_STATUSES.indexOf(initialState.status) !== -1;" in source
    assert "if (followAction && data.status && TERMINAL_STATUSES.indexOf(data.status) !== -1)" in source
    assert "var activeActionId = followAction && initialState.action_id" in source


def test_cluster_deploy_incident_excluded_from_cluster_status():
    incident = Incident(
        id="i1",
        ceph_code="CLUSTER_DEPLOY",
        status=IncidentStatus.FAILED.value,
        detected_at=__import__("datetime").datetime.utcnow(),
    )
    # A failed cluster-DEPLOY attempt must never flip the (unrelated,
    # already-monitored) cluster's own health badge to ERR — it's excluded
    # from real_incidents entirely, so with no other incidents this is "OK".
    assert incidents_route.compute_cluster_status([incident], heartbeat_stale=False) == "OK"


def test_deploy_does_not_register_monitoring_unless_asked(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload())

    with db_module.SessionLocal() as session:
        params = json.loads(session.get(Action, response.json()["action_id"]).action_params)
    assert params["register_monitoring"] is False and "monitor_cluster_name" not in params


def test_deploy_can_register_the_new_cluster_as_a_second_monitored_cluster(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(
        register_monitoring=True, monitor_cluster_name="CS-NEW"))

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        params = json.loads(session.get(Action, response.json()["action_id"]).action_params)
    assert params["register_monitoring"] is True and params["monitor_cluster_name"] == "CS-NEW"


def test_monitoring_name_must_be_valid_and_unused(dashboard_client):
    from shared.models import Cluster

    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        session.add(Cluster(name="Taken", is_default=False, is_active=True, ceph_mon_nodes="10.0.0.1",
                            ssh_user="root", ssh_key_path="/tmp/k"))
        session.commit()

    for name, message in (("", "Tên cụm giám sát"), ("bad/name", "Tên cụm giám sát"), ("Taken", "Đã có cụm")):
        response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(
            register_monitoring=True, monitor_cluster_name=name))
        assert response.status_code == 400 and message in response.json()["detail"], name


def test_deploy_page_offers_the_monitoring_option_off_by_default(dashboard_client):
    _login(dashboard_client)

    page = dashboard_client.get("/deploy-cluster").text

    assert 'id="df-register-monitoring"' in page and "checked" not in page.split('id="df-register-monitoring"')[1][:40]
    assert "Đăng ký cụm mới làm cụm giám sát thứ hai" in page


def test_deploy_can_mark_the_new_cluster_as_failure_lab_staging(dashboard_client):
    _login(dashboard_client)
    assert "Dùng cụm này làm cụm Staging" in dashboard_client.get("/deploy-cluster").text

    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(
        register_monitoring=True, monitor_cluster_name="CS-STAGING", failure_lab_staging=True))

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        params = json.loads(session.get(Action, response.json()["action_id"]).action_params)
    assert params["failure_lab_staging"] is True


def test_staging_is_refused_when_one_is_already_set(dashboard_client):
    from shared import failure_lab_config
    from shared.models import Cluster

    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        lab = Cluster(name="CS-STG", is_default=False, is_active=True, ceph_mon_nodes="10.0.0.9",
                      ssh_user="root", ssh_key_path="/tmp/k")
        session.add(lab)
        session.commit()
        failure_lab_config.save("admin", cluster_id=lab.id)

    assert "Đã có cụm Staging: CS-STG" in dashboard_client.get("/deploy-cluster").text
    response = dashboard_client.post("/deploy-cluster/propose", json=_valid_payload(
        register_monitoring=True, monitor_cluster_name="CS-NEW", failure_lab_staging=True))

    assert response.status_code == 400 and "CS-STG" in response.json()["detail"]
