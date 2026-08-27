import dashboard.routes.openstack as openstack_route
import dashboard.routes.settings as settings_route
from shared import db
from shared.models import Cluster


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_pool_and_auth_payload_normalization():
    assert openstack_route._pool_names([
        {"pool_id": 1, "pool_name": "volumes"},
        {"pool": 2, "poolname": "images"},
    ]) == ["images", "volumes"]
    assert [row["entity"] for row in openstack_route._auth_rows({"auth_dump": [
        {"entity": "client.nova", "caps": {}},
        {"entity": "osd.0", "caps": {}},
        {"entity": "client.cinder", "caps": {}},
    ]})] == ["client.cinder", "client.nova"]


def test_config_dump_normalization_sorts_and_redacts_credentials():
    rows = openstack_route._config_dump_rows([
        {"section": "global", "name": "rgw_crypt_vault_addr", "value": "https://vault.internal:8200"},
        {"section": "global", "name": "rgw_crypt_vault_token_file", "value": "/etc/ceph/vault_token"},
        {"section": "mon", "name": "mon_allow_pool_delete", "value": True, "level": "advanced"},
        {"section": "global", "name": "cluster_secret", "value": "do-not-leak"},
        {"section": "global", "name": "", "value": "ignored"},
    ])
    assert [(row["section"], row["name"]) for row in rows] == [
        ("global", "cluster_secret"),
        ("global", "rgw_crypt_vault_addr"),
        ("global", "rgw_crypt_vault_token_file"),
        ("mon", "mon_allow_pool_delete"),
    ]
    assert rows[0]["value"] == "[REDACTED]"
    assert rows[0]["redacted"] is True
    assert rows[1]["value"] == "https://vault.internal:8200"
    assert rows[1]["redacted"] is False
    assert rows[2]["value"] == "[REDACTED]"
    assert rows[3]["value"] == "True"


def test_auth_config_dump_endpoint_returns_masked_rows(dashboard_client, monkeypatch):
    def fake_query(*args):
        assert args[-1] == "ceph config dump"
        return "mon1", [
            {"section": "global", "name": "rgw_crypt_vault_addr", "value": "https://vault:8200"},
            {"section": "global", "name": "rgw_crypt_vault_token", "value": "super-secret"},
        ]

    monkeypatch.setattr(openstack_route, "run_ceph_json_command_with", fake_query)
    _login(dashboard_client)
    response = dashboard_client.get("/api/openstack/auth-config-dump")
    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == [
        {
            "section": "global",
            "name": "rgw_crypt_vault_addr",
            "value": "https://vault:8200",
            "level": "",
            "can_update_at_runtime": False,
            "redacted": False,
        },
        {
            "section": "global",
            "name": "rgw_crypt_vault_token",
            "value": "[REDACTED]",
            "level": "",
            "can_update_at_runtime": False,
            "redacted": True,
        },
    ]
    assert "super-secret" not in response.text


def test_config_dump_set_restarts_every_rgw(dashboard_client, monkeypatch):
    executed = []

    def fake_query(*args):
        assert args[-1] == "ceph orch ps --daemon_type rgw"
        return "mon1", [
            {"daemon_name": "rgw.sse.ceph1"},
            {"daemon_name": "rgw.sse.ceph2"},
        ]

    monkeypatch.setattr(openstack_route, "run_ceph_json_command_with", fake_query)
    monkeypatch.setattr(
        openstack_route,
        "execute_command",
        lambda host, command, **kwargs: executed.append((host, command, kwargs)) or "ok",
    )
    _login(dashboard_client)
    response = dashboard_client.post(
        "/openstack/config-dump",
        data={"action": "set", "section": "global", "name": "test_option", "value": "value with spaces"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "updated=1" in response.headers["location"]
    assert len(executed) == 3
    assert "ceph config set global test_option 'value with spaces'" in executed[0][1]
    assert all("ceph orch daemon restart rgw.sse.ceph" in command for _, command, _ in executed[1:])


def test_config_dump_rejects_unsafe_option_without_execution(dashboard_client, monkeypatch):
    executed = []
    monkeypatch.setattr(openstack_route, "execute_command", lambda *args, **kwargs: executed.append(args))
    _login(dashboard_client)
    response = dashboard_client.post(
        "/openstack/config-dump",
        data={"action": "set", "section": "global;id", "name": "bad", "value": "x"},
    )
    assert response.status_code == 400
    assert executed == []


def test_caps_command_preserves_existing_caps_and_adds_pool():
    command = openstack_route._caps_command(
        "client.cinder", "volumes", "write",
        {"mon": "profile rbd", "osd": "profile rbd pool=images", "mgr": "allow r"},
    )
    assert command == (
        "ceph auth caps client.cinder mon 'profile rbd' mgr 'allow r' "
        "osd 'profile rbd pool=images, profile rbd pool=volumes'"
    )


def test_caps_command_supports_read_only_and_rejects_injection():
    assert "profile rbd-read-only pool=images" in openstack_route._caps_command(
        "client.glance", "images", "read", {}
    )
    try:
        openstack_route._caps_command("client.nova;id", "volumes", "write", {})
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe entity must be rejected")


def test_create_auth_command_builds_executable_rbd_caps():
    assert openstack_route._create_auth_command("client.cinder", "volumes", "write") == (
        "ceph auth get-or-create client.cinder mon 'profile rbd' osd 'profile rbd pool=volumes'"
    )
    assert "profile rbd-read-only pool=images" in openstack_route._create_auth_command(
        "client.glance", "images", "read"
    )


def test_auth_pool_page_loads_users_and_pools(dashboard_client, monkeypatch):
    def fake_query(*args):
        if args[-1] == "ceph osd pool ls detail":
            return "mon1", [{"pool_id": 1, "pool_name": "volumes"}]
        return "mon1", {"auth_dump": [{
            "entity": "client.cinder",
            "caps": {"mon": "profile rbd", "osd": "profile rbd pool=volumes"},
        }]}

    monkeypatch.setattr(openstack_route, "run_ceph_json_command_with", fake_query)
    _login(dashboard_client)
    response = dashboard_client.get("/openstack/auth-pool")
    assert response.status_code == 200
    assert "client.cinder" in response.text
    assert "volumes" in response.text
    assert "Controller" not in response.text
    assert 'class="tabbed-sidebar"' not in response.text


def test_main_sidebar_defines_ceph_auth_group():
    app_js = (openstack_route.templates.env.loader.searchpath[0] + "/../static/app.js")
    with open(app_js, encoding="utf-8") as source:
        content = source.read()
    assert (
        '{ label: "ceph-auth", paths: ["/openstack/auth-pool", '
        '"/openstack/config-dump", "/openstack/auth-user/create"] }'
    ) in content


def test_config_dump_page_is_separate_from_auth_pool(dashboard_client, monkeypatch):
    def fail_if_ceph_is_called(*args):
        raise AssertionError("config dump page should load data lazily")

    monkeypatch.setattr(openstack_route, "run_ceph_json_command_with", fail_if_ceph_is_called)
    _login(dashboard_client)
    response = dashboard_client.get("/openstack/config-dump")
    assert response.status_code == 200
    assert "Ceph Config Dump" in response.text
    assert "Tải ceph config dump" in response.text
    assert "<h2>OpenStack Auth-Pool" not in response.text


def test_create_auth_user_page_has_create_form(dashboard_client, monkeypatch):
    def fake_query(*args):
        if args[-1] == "ceph osd pool ls detail":
            return "mon1", [{"pool_id": 1, "pool_name": "volumes"}]
        return "mon1", {"auth_dump": []}

    monkeypatch.setattr(openstack_route, "run_ceph_json_command_with", fake_query)
    _login(dashboard_client)
    response = dashboard_client.get("/openstack/auth-user/create")
    assert response.status_code == 200
    assert "Tạo Ceph Auth User" in response.text
    assert 'action="/openstack/auth-user/create?' in response.text
    assert 'href="/volumes"' in response.text
    assert 'href="/pgs"' not in response.text  # app.js adds PGs without deleting Pool/Volumes


def test_create_auth_user_executes_on_selected_cluster(dashboard_client, monkeypatch):
    def fake_query(*args):
        if args[-1] == "ceph osd pool ls detail":
            return "mon1", [{"pool_id": 1, "pool_name": "volumes"}]
        return "mon1", {"auth_dump": []}

    executed = []
    monkeypatch.setattr(openstack_route, "run_ceph_json_command_with", fake_query)
    monkeypatch.setattr(
        openstack_route,
        "execute_command",
        lambda host, command, **kwargs: executed.append((host, command, kwargs)) or "created",
    )
    _login(dashboard_client)
    response = dashboard_client.post(
        "/openstack/auth-user/create",
        data={"entity_name": "cinder", "pool": "volumes", "access": "write"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "created=1" in response.headers["location"]
    assert len(executed) == 1
    assert "ceph auth get-or-create client.cinder" in executed[0][1]
    assert "profile rbd pool=volumes" in executed[0][1]


def test_settings_saves_openstack_nodes(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/settings/openstack", data={
        "controller_nodes": "10.0.0.10, 10.0.0.11",
        "compute_nodes": "10.0.0.20, 10.0.0.21",
        "ceph_config_path": "/etc/ceph/openstack",
        "openrc_path": "/root/admin-openrc",
    })
    assert response.status_code == 200
    assert "Đã lưu cấu hình OpenStack" in response.text
    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).one()
        assert cluster.openstack_controller_nodes == "10.0.0.10,10.0.0.11"
        assert cluster.openstack_compute_nodes == "10.0.0.20,10.0.0.21"
        assert cluster.openstack_ceph_config_path == "/etc/ceph/openstack"
        assert cluster.openstack_openrc_path == "/root/admin-openrc"


def test_settings_tests_openstack_nodes_without_saving(dashboard_client, monkeypatch):
    _login(dashboard_client)
    calls = []

    def fake_execute(host, command, user=None, key_path=None):
        calls.append((host, command, user, key_path))
        return "CEPH_AIOPS_OPENSTACK_OK"

    monkeypatch.setattr(settings_route, "execute_command", fake_execute)
    response = dashboard_client.post("/settings/openstack/test", data={
        "controller_nodes": "controller1, controller2",
        "compute_nodes": "compute1,controller1",
    })
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert "3/3" in response.json()["message"]
    assert [call[0] for call in calls] == ["controller1", "controller2", "compute1"]
    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).one()
        assert cluster.openstack_controller_nodes != "controller1,controller2"


def test_settings_openstack_test_reports_failed_node(dashboard_client, monkeypatch):
    _login(dashboard_client)

    def fake_execute(host, command, user=None, key_path=None):
        if host == "controller2":
            raise settings_route.ExecutorError("SSH timeout")
        return "CEPH_AIOPS_OPENSTACK_OK"

    monkeypatch.setattr(settings_route, "execute_command", fake_execute)
    response = dashboard_client.post("/settings/openstack/test", data={
        "controller_nodes": "controller1,controller2",
        "compute_nodes": "",
    })
    assert response.json()["valid"] is False
    assert "controller2" in response.json()["message"]
    assert "SSH timeout" in response.json()["message"]


def test_settings_tests_vm_ssh_through_controller(dashboard_client, monkeypatch):
    _login(dashboard_client)
    calls = []

    def fake_execute(host, command, user=None, key_path=None):
        calls.append((host, command, user, key_path))
        return "CEPH_AIOPS_VM_SSH_OK"

    monkeypatch.setattr(settings_route, "execute_command", fake_execute)
    response = dashboard_client.post("/settings/openstack/vm/test", data={
        "controller_nodes": "controller1,controller2",
        "vm_ip": "10.20.1.50",
        "vm_ssh_user": "ubuntu",
        "vm_ssh_key_path": "/root/.ssh/vm-key",
    })
    assert response.json()["valid"] is True
    assert "controller1" in response.json()["message"]
    assert calls[0][0] == "controller1"
    assert "ubuntu@10.20.1.50" in calls[0][1]
    assert "/root/.ssh/vm-key" in calls[0][1]


def test_settings_vm_ssh_tries_next_controller(dashboard_client, monkeypatch):
    _login(dashboard_client)
    attempted = []

    def fake_execute(host, command, user=None, key_path=None):
        attempted.append(host)
        if host == "controller1":
            raise settings_route.ExecutorError("controller unavailable")
        return "CEPH_AIOPS_VM_SSH_OK"

    monkeypatch.setattr(settings_route, "execute_command", fake_execute)
    response = dashboard_client.post("/settings/openstack/vm/test", data={
        "controller_nodes": "controller1,controller2",
        "vm_ip": "10.20.1.50",
        "vm_ssh_user": "root",
        "vm_ssh_key_path": "/root/.ssh/vm-key",
    })
    assert response.json()["valid"] is True
    assert attempted == ["controller1", "controller2"]


def test_create_auth_user_exports_and_copies_ceph_files(dashboard_client, monkeypatch):
    def fake_query(*args):
        if args[-1] == "ceph osd pool ls detail":
            return "mon1", [{"pool_id": 1, "pool_name": "volumes"}]
        return "mon1", {"auth_dump": []}

    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).one()
        cluster.openstack_controller_nodes = "controller1"
        cluster.openstack_compute_nodes = "compute1"
        cluster.openstack_ceph_config_path = "/etc/ceph"
        session.commit()

    executed = []

    def fake_execute(host, command, **kwargs):
        executed.append((host, command, kwargs))
        if "client.admin" in command:
            return "[client.admin]\n key = admin-secret\n"
        if "auth get client.cinder" in command:
            return "[client.cinder]\n key = user-secret\n"
        if "cat /etc/ceph/ceph.conf" in command:
            return "[global]\n fsid = test\n"
        return "created"

    monkeypatch.setattr(openstack_route, "run_ceph_json_command_with", fake_query)
    monkeypatch.setattr(openstack_route, "execute_command", fake_execute)
    _login(dashboard_client)
    response = dashboard_client.post(
        "/openstack/auth-user/create",
        data={"entity_name": "cinder", "pool": "volumes", "access": "write"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert any("-o /tmp/ceph.client.cinder.keyring" in command for _, command, _ in executed)
    for host in ("controller1", "compute1"):
        target_command = next(command for called_host, command, _ in executed if called_host == host)
        assert "/etc/ceph/ceph.client.cinder.keyring" in target_command
        assert "/etc/ceph/ceph.client.admin.keyring" in target_command
        assert "/etc/ceph/ceph.conf" in target_command
