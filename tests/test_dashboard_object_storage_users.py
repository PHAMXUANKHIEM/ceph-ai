import dashboard.routes.object_storage_users as route
from config.settings import settings
from shared import db
from shared.models import Cluster


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "10.20.1.90")
    monkeypatch.setattr(settings, "ceph_rgw_container_name", "ceph-rgw-B")


def _raw(uid):
    return {
        "user_id": uid,
        "display_name": f"User {uid}",
        "email": f"{uid}@example.test",
        "suspended": 0,
        "max_buckets": 100,
        "keys": [{"access_key": "AKIA-DO-NOT-LEAK", "secret_key": "SUPER-SECRET"}],
        "subusers": [{"id": f"{uid}:swift"}],
        "caps": [{"type": "users", "perm": "read"}],
        "user_quota": {"enabled": True},
        "bucket_quota": {"enabled": False},
    }


def test_users_api_paginates_and_never_returns_key_material(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(route, "PAGE_SIZE", 1)
    monkeypatch.setattr(route, "fetch_s3_user_list", lambda host: ["alice", "bob"])
    monkeypatch.setattr(route, "fetch_s3_user_info", lambda host, uid: _raw(uid))
    _login(dashboard_client)

    response = dashboard_client.get("/api/object-storage/users?page=2")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["items"][0]["uid"] == "bob"
    assert body["items"][0]["key_count"] == 1
    assert "AKIA-DO-NOT-LEAK" not in response.text
    assert "SUPER-SECRET" not in response.text


def test_user_detail_is_secret_safe_in_api_and_html(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(route, "fetch_s3_user_info", lambda host, uid: _raw(uid))
    _login(dashboard_client)

    api = dashboard_client.get("/api/object-storage/users/alice")
    page = dashboard_client.get("/object-storage/users/alice")

    assert api.status_code == 200
    assert api.json()["caps"] == ["users"]
    assert page.status_code == 200
    assert "User alice" in page.text
    assert "AKIA-DO-NOT-LEAK" not in api.text + page.text
    assert "SUPER-SECRET" not in api.text + page.text


def test_users_page_search_and_empty_state(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(route, "fetch_s3_user_list", lambda host: ["alice", "bob"])
    monkeypatch.setattr(route, "fetch_s3_user_info", lambda host, uid: _raw(uid))
    _login(dashboard_client)

    response = dashboard_client.get("/object-storage/users?query=missing")

    assert response.status_code == 200
    assert "Không có S3 user phù hợp." in response.text


def test_user_detail_rejects_path_like_uid(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/object-storage/users/tenant%2Fuser")

    assert response.status_code == 404


def test_secondary_cluster_uses_scoped_rgw_credentials(dashboard_client, monkeypatch):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = Cluster(
            name="users-secondary", ceph_mon_nodes="10.99.0.10", ceph_rgw_nodes="10.99.0.90",
            ceph_container_name="mon-2", ceph_rgw_container_name="rgw-2", ssh_user="ceph2",
            ssh_key_path="/keys/ceph2", ceph_exec_mode="docker", is_default=False, is_active=True,
        )
        session.add(cluster)
        session.commit()
        cluster_id = cluster.id
    calls = []
    monkeypatch.setattr(route, "fetch_s3_user_list_with", lambda *args: calls.append(args) or ["alice"])
    monkeypatch.setattr(route, "fetch_s3_user_info_with", lambda *args: _raw("alice"))

    response = dashboard_client.get(f"/api/object-storage/users?cluster={cluster_id}")

    assert response.status_code == 200
    assert calls == [("10.99.0.90", "ceph2", "/keys/ceph2", "docker", "rgw-2")]


def test_create_preview_generates_no_access_key(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/api/object-storage/users/actions/preview", json={
        "action": "create", "uid": "alice", "display_name": "Alice Operator",
        "email": "alice@example.test",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["confirmation_required"] == "alice"
    assert body["generates_access_key"] is False
    assert "--generate-key=false" in body["preview"]
    assert "secret" not in response.text.casefold()


def test_admin_page_exposes_two_step_action_form(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(route, "fetch_s3_user_list", lambda host: [])
    _login(dashboard_client)

    response = dashboard_client.get("/object-storage/users")

    assert response.status_code == 200
    assert 'id="s3-user-action-form"' in response.text
    assert 'id="s3-execute"' in response.text
    assert "Tạo user không tự sinh access key" in response.text


def test_execute_requires_admin_and_exact_uid_confirmation(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    _login(dashboard_client)
    payload = {"action": "suspend", "uid": "alice", "confirmation": "wrong"}

    bad_confirmation = dashboard_client.post("/api/object-storage/users/actions/execute", json=payload)
    assert bad_confirmation.status_code == 400

    monkeypatch.setattr(route.auth, "is_admin_user", lambda user: False)
    forbidden = dashboard_client.post(
        "/api/object-storage/users/actions/execute",
        json={"action": "suspend", "uid": "alice", "confirmation": "alice"},
    )
    assert forbidden.status_code == 403


def test_execute_uses_closed_action_adapter(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(
        route, "execute_s3_user_action",
        lambda host, action, uid, params: calls.append((host, action, uid, params)),
    )
    _login(dashboard_client)

    response = dashboard_client.post("/api/object-storage/users/actions/execute", json={
        "action": "modify", "uid": "alice", "display_name": "Alice New",
        "confirmation": "alice",
    })

    assert response.status_code == 200
    assert calls == [("10.20.1.90", "modify", "alice", {"display_name": "Alice New", "email": ""})]
