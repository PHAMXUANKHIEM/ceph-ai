import dashboard.routes.openstack as openstack_route
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
        '"/openstack/auth-user/create"] }'
    ) in content


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
    })
    assert response.status_code == 200
    assert "Đã lưu cấu hình OpenStack" in response.text
    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).one()
        assert cluster.openstack_controller_nodes == "10.0.0.10,10.0.0.11"
        assert cluster.openstack_compute_nodes == "10.0.0.20,10.0.0.21"
        assert cluster.openstack_ceph_config_path == "/etc/ceph/openstack"


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
