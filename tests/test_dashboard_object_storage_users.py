import dashboard.routes.object_storage_users as route
from config.settings import settings
from shared import db
from shared.models import Cluster
from shared.models import ObjectStorageAuditEntry


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
    assert 'href="/openstack/auth-pool"' in response.text
    assert 'href="/deploy-cluster"' in response.text


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
    assert body["generates_access_key"] is None
    assert "--generate-key" not in body["preview"]
    assert "secret" not in response.text.casefold()


def test_admin_page_exposes_two_step_action_form(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(route, "fetch_s3_user_list", lambda host: [])
    _login(dashboard_client)

    response = dashboard_client.get("/object-storage/users")

    assert response.status_code == 200
    assert 'id="s3-user-action-form"' in response.text
    assert 'id="s3-execute"' in response.text
    assert 'id="s3-key-action-form"' in response.text
    assert 'id="s3-one-time-secret"' in response.text
    assert "Access-key lifecycle" in response.text


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
        lambda host, action, uid, params: calls.append((host, action, uid, params)) or None,
    )
    _login(dashboard_client)

    response = dashboard_client.post("/api/object-storage/users/actions/execute", json={
        "action": "modify", "uid": "alice", "display_name": "Alice New",
        "confirmation": "alice",
    })

    assert response.status_code == 200
    assert calls == [("10.20.1.90", "modify", "alice", {"display_name": "Alice New", "email": ""})]
    request_id = response.json()["request_id"]
    with db.SessionLocal() as session:
        audit = session.get(ObjectStorageAuditEntry, request_id)
        assert audit.actor == "admin"
        assert audit.cluster_id == response.json()["cluster_id"]
        assert audit.target_id == "alice"
        assert audit.result == "succeeded"
        assert "secret" not in audit.preview.casefold()


def test_failed_action_is_persisted_in_audit(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        route, "execute_s3_user_action",
        lambda *args: (_ for _ in ()).throw(route.RgwLogError("RGW unavailable")),
    )
    _login(dashboard_client)

    response = dashboard_client.post("/api/object-storage/users/actions/execute", json={
        "action": "suspend", "uid": "alice", "confirmation": "alice",
    })

    assert response.status_code == 502
    with db.SessionLocal() as session:
        audit = session.query(ObjectStorageAuditEntry).filter_by(target_id="alice").one()
        assert audit.result == "failed"
        assert audit.error_message == "RGW unavailable"


def test_audit_api_is_admin_only_and_cluster_scoped(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster_id = session.query(Cluster).filter_by(is_default=True).one().id
        session.add(ObjectStorageAuditEntry(
            cluster_id=cluster_id, actor="admin", action="enable", target_type="s3_user",
            target_id="alice", preview="radosgw-admin user enable --uid=alice", result="succeeded",
        ))
        session.commit()

    response = dashboard_client.get("/api/object-storage/audit")
    assert response.status_code == 200
    assert response.json()["entries"][0]["target_id"] == "alice"

    monkeypatch.setattr(route.auth, "is_admin_user", lambda user: False)
    assert dashboard_client.get("/api/object-storage/audit").status_code == 403


def test_create_access_key_returns_secret_once_but_never_persists_it(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        route, "create_s3_access_key",
        lambda host, uid: {"access_key": "NEWACCESS", "secret_key": "ONE-TIME-SECRET"},
    )
    _login(dashboard_client)

    preview = dashboard_client.post("/api/object-storage/users/keys/preview", json={
        "action": "create_key", "uid": "alice",
    })
    response = dashboard_client.post("/api/object-storage/users/keys/execute", json={
        "action": "create_key", "uid": "alice", "confirmation": "alice",
    })

    assert preview.status_code == 200
    assert "ONE-TIME-SECRET" not in preview.text
    assert response.status_code == 200
    assert response.json()["credential"]["secret_key"] == "ONE-TIME-SECRET"
    assert response.json()["secret_shown_once"] is True
    with db.SessionLocal() as session:
        audit = session.get(ObjectStorageAuditEntry, response.json()["request_id"])
        assert "ONE-TIME-SECRET" not in audit.preview
        assert "ONE-TIME-SECRET" not in (audit.error_message or "")


def test_revoke_access_key_requires_exact_key_confirmation(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(route, "revoke_s3_access_key", lambda *args: calls.append(args))
    _login(dashboard_client)
    payload = {"action": "revoke_key", "uid": "alice", "access_key": "OLDKEY"}

    bad = dashboard_client.post(
        "/api/object-storage/users/keys/execute", json={**payload, "confirmation": "alice"}
    )
    good = dashboard_client.post(
        "/api/object-storage/users/keys/execute", json={**payload, "confirmation": "OLDKEY"}
    )

    assert bad.status_code == 400
    assert good.status_code == 200
    assert calls == [("10.20.1.90", "alice", "OLDKEY")]


def test_quota_preview_explains_effect_and_execute_is_audited(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(route, "execute_s3_user_setting", lambda *args: calls.append(args))
    _login(dashboard_client)
    payload = {"action": "quota_set", "uid": "alice", "scope": "user",
               "max_size_bytes": 1024, "max_objects": 10}

    preview = dashboard_client.post("/api/object-storage/users/settings/preview", json=payload)
    executed = dashboard_client.post(
        "/api/object-storage/users/settings/execute", json={**payload, "confirmation": "alice"}
    )

    assert preview.status_code == 200
    assert "enforcement" in preview.json()["effect"]
    assert executed.status_code == 200
    assert calls == [("10.20.1.90", "quota_set", "alice", {
        "scope": "user", "max_size_bytes": 1024, "max_objects": 10,
    })]
    with db.SessionLocal() as session:
        audit = session.get(ObjectStorageAuditEntry, executed.json()["request_id"])
        assert audit.action == "quota_set"
        assert audit.result == "succeeded"


def test_capability_preview_rejects_values_outside_allowlist(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/api/object-storage/users/settings/preview", json={
        "action": "cap_add", "uid": "alice", "cap_type": "zone", "cap_perm": "*",
    })
    assert response.status_code == 400


def test_quota_capability_editor_page_is_admin_only(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/object-storage/user-settings")
    assert response.status_code == 200
    assert 'id="s3-setting-form"' in response.text
    assert "Quota &amp; Capability Editor" in response.text

    original = route.auth.is_admin_user
    route.auth.is_admin_user = lambda user: False
    try:
        assert dashboard_client.get("/object-storage/user-settings").status_code == 403
    finally:
        route.auth.is_admin_user = original


def test_secondary_cluster_executes_setting_with_scoped_credentials(dashboard_client, monkeypatch):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = Cluster(name="setting-secondary", ceph_mon_nodes="10.88.0.10", ceph_rgw_nodes="10.88.0.90", ceph_container_name="mon-x", ceph_rgw_container_name="rgw-x", ssh_user="cephx", ssh_key_path="/keys/x", ceph_exec_mode="docker", is_default=False, is_active=True)
        session.add(cluster); session.commit(); cluster_id = cluster.id
    calls = []
    monkeypatch.setattr(route, "execute_s3_user_setting_with", lambda *args: calls.append(args))
    payload = {"action":"quota_enable", "uid":"alice", "scope":"bucket", "confirmation":"alice"}
    response = dashboard_client.post(f"/api/object-storage/users/settings/execute?cluster={cluster_id}", json=payload)
    assert response.status_code == 200
    assert calls == [("10.88.0.90", "quota_enable", "alice", {"scope":"bucket"}, "cephx", "/keys/x", "docker", "rgw-x")]


def test_non_admin_cannot_call_any_write_preview_or_execute_api(dashboard_client, monkeypatch):
    _login(dashboard_client)
    monkeypatch.setattr(route.auth, "is_admin_user", lambda user: False)
    requests = [
        ("/api/object-storage/users/actions/preview", {"action":"suspend", "uid":"alice"}),
        ("/api/object-storage/users/actions/execute", {"action":"suspend", "uid":"alice", "confirmation":"alice"}),
        ("/api/object-storage/users/keys/preview", {"action":"create_key", "uid":"alice"}),
        ("/api/object-storage/users/keys/execute", {"action":"create_key", "uid":"alice", "confirmation":"alice"}),
        ("/api/object-storage/users/settings/preview", {"action":"quota_enable", "uid":"alice", "scope":"user"}),
        ("/api/object-storage/users/settings/execute", {"action":"quota_enable", "uid":"alice", "scope":"user", "confirmation":"alice"}),
    ]
    assert [dashboard_client.post(path, json=body).status_code for path, body in requests] == [403] * 6


def test_failed_key_action_redacts_secret_from_http_and_audit(dashboard_client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(route, "create_s3_access_key", lambda *args: (_ for _ in ()).throw(
        route.RgwLogError("secret_access_key=LEAK-ME")
    ))
    _login(dashboard_client)
    response = dashboard_client.post("/api/object-storage/users/keys/execute", json={
        "action":"create_key", "uid":"alice", "confirmation":"alice",
    })
    assert response.status_code == 502
    assert "LEAK-ME" not in response.text
    assert "[REDACTED]" in response.text
    with db.SessionLocal() as session:
        audit = session.query(ObjectStorageAuditEntry).filter_by(action="create_key").one()
        assert audit.error_message == "secret_access_key=[REDACTED]"


def test_secondary_cluster_creates_key_with_scoped_credentials(dashboard_client, monkeypatch):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = Cluster(name="key-secondary", ceph_mon_nodes="10.77.0.10", ceph_rgw_nodes="10.77.0.90", ceph_container_name="mon-k", ceph_rgw_container_name="rgw-k", ssh_user="cephk", ssh_key_path="/keys/k", ceph_exec_mode="podman", is_default=False, is_active=True)
        session.add(cluster); session.commit(); cluster_id = cluster.id
    calls = []
    monkeypatch.setattr(route, "create_s3_access_key_with", lambda *args: calls.append(args) or {"access_key":"NEW", "secret_key":"SECRET"})
    response = dashboard_client.post(f"/api/object-storage/users/keys/execute?cluster={cluster_id}", json={"action":"create_key", "uid":"alice", "confirmation":"alice"})
    assert response.status_code == 200
    assert calls == [("10.77.0.90", "alice", "cephk", "/keys/k", "podman", "rgw-k")]
