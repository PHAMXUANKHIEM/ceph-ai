import dashboard.routes.object_storage as object_storage_route
from datetime import datetime, timezone
import json
import bcrypt
from config.settings import settings
from shared import db
from shared.models import Cluster, ObjectStorageAuditEntry, User
from watcher.rgw_access_log import RgwLogError


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _login_operator(client):
    with db.SessionLocal() as session:
        session.add(User(
            username="bucket-operator",
            password_hash=bcrypt.hashpw(b"operator-pass", bcrypt.gensalt()).decode(),
            is_admin=False,
            is_active=True,
            created_by="admin",
        ))
        session.commit()
    client.post("/login", data={"username": "bucket-operator", "password": "operator-pass"})


def _configure_nodes(monkeypatch):
    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.150")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "10.20.1.90")
    monkeypatch.setattr(settings, "ceph_rgw_container_name", "ceph-rgw-B")


def _stats(owner="operator", size=1024, objects=2):
    return {
        "owner": owner,
        "creation_time": "2026-08-16 01:02:03.000000",
        "usage": {"rgw.main": {"num_objects": objects, "size_utilized": size}},
        "bucket_quota": {"enabled": True, "max_size": 10_000, "max_objects": 50},
    }


def test_unauthenticated_inventory_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/object-storage/buckets", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_inventory_page_shows_empty_state_without_sample_buckets(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: [])
    _login(dashboard_client)

    response = dashboard_client.get("/object-storage/buckets")

    assert response.status_code == 200
    assert "Chưa có bucket trên cụm đang chọn." in response.text
    assert ".mgr" not in response.text
    assert '<a href="/object-storage/buckets" class="nav-link active">Buckets</a>' in response.text
    assert '>Object Storage</a>' not in response.text


def test_capability_api_detects_live_reef_version_and_requires_s3_for_bucket_create(dashboard_client, monkeypatch):
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "18.2.4", "is_mixed": False,
    })
    _login(dashboard_client)
    response = dashboard_client.get("/api/object-storage/capabilities")
    assert response.status_code == 200
    capability = response.json()
    assert capability["ceph_release"] == "reef"
    assert capability["bucket_create"]["method"] == "s3_api"
    assert capability["bucket_create"]["radosgw_admin_supported"] is False
    assert "/reef/" in capability["bucket_create"]["documentation"]


def test_capability_api_fails_closed_for_mixed_cluster(dashboard_client, monkeypatch):
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": None, "is_mixed": True,
    })
    _login(dashboard_client)
    response = dashboard_client.get("/api/object-storage/capabilities")
    assert response.status_code == 502
    assert "lẫn phiên bản" in response.json()["detail"]


def test_capability_api_explains_features_unavailable_on_mimic(dashboard_client, monkeypatch):
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "13.2.10", "is_mixed": False,
    })
    _login(dashboard_client)
    response = dashboard_client.get("/api/object-storage/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["ceph_release"] == "mimic"
    assert body["bucket_governance"]["object_lock_at_create"] is False
    assert "Octopus 15" in body["bucket_governance"]["object_lock_unavailable_reason"]
    assert body["lifecycle"]["supported"] is True
    assert body["lifecycle"]["transition_supported"] is False
    assert "Nautilus 14" in body["lifecycle"]["transition_unavailable_reason"]


def test_old_ceph_rejects_object_lock_and_lifecycle_transition_server_side(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "13.2.10", "is_mixed": False,
    })
    monkeypatch.setattr(object_storage_route, "fetch_s3_user_info", lambda host, uid: {"user_id": uid})
    _login(dashboard_client)
    object_lock = dashboard_client.post("/api/object-storage/buckets/actions/preview", json={
        "name": "team-archive", "owner": "alice", "endpoint": "https://rgw.example.test",
        "object_lock": True,
    })
    transition = dashboard_client.post("/api/object-storage/buckets/lifecycle/preview", json={
        "action": "lifecycle_put", "bucket": "team-archive", "owner": "alice",
        "endpoint": "https://rgw.example.test", "rules": [{
            "id": "archive", "prefix": "logs/", "transition_days": 30,
            "storage_class": "STANDARD_IA",
        }],
    })
    new_policy_action = dashboard_client.post("/api/object-storage/buckets/policy-acl/preview", json={
        "action": "policy_put", "bucket": "team-archive", "owner": "alice",
        "endpoint": "https://rgw.example.test", "policy": {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"AWS": "alice"},
            "Action": "s3:PutBucketPublicAccessBlock", "Resource": "arn:aws:s3:::team-archive",
        }]},
    })
    assert object_lock.status_code == 409
    assert "Octopus 15" in object_lock.json()["detail"]
    assert transition.status_code == 409
    assert "Nautilus 14" in transition.json()["detail"]
    assert new_policy_action.status_code == 409
    assert "Octopus 15" in new_policy_action.json()["detail"]


def test_create_bucket_uses_s3_api_temporary_owner_key_and_audit(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "18.2.4", "is_mixed": False,
    })
    monkeypatch.setattr(object_storage_route, "fetch_s3_user_info", lambda host, uid: {
        "user_id": uid, "suspended": 0,
    })
    monkeypatch.setattr(object_storage_route, "create_s3_access_key", lambda host, uid: {
        "access_key": "TEMPACCESS", "secret_key": "temp-secret",
    })
    revoked = []
    monkeypatch.setattr(object_storage_route, "revoke_s3_access_key", lambda host, uid, key: revoked.append((host, uid, key)))
    created = []

    class FakeS3:
        def create_bucket(self, **kwargs):
            created.append(kwargs)

    client_args = []
    monkeypatch.setattr(object_storage_route.boto3, "client", lambda *args, **kwargs: client_args.append((args, kwargs)) or FakeS3())
    _login(dashboard_client)
    body = {"name": "team-archive", "owner": "alice", "endpoint": "https://rgw.example.test",
            "api_name": "default", "placement": "archive", "object_lock": True}

    preview = dashboard_client.post("/api/object-storage/buckets/actions/preview", json=body)
    executed = dashboard_client.post("/api/object-storage/buckets/actions/execute", json={**body, "confirmation": "team-archive"})

    assert preview.status_code == 200
    assert preview.json()["ceph_release"] == "reef"
    assert executed.status_code == 200
    assert created == [{"Bucket": "team-archive", "ObjectLockEnabledForBucket": True,
                        "CreateBucketConfiguration": {"LocationConstraint": "default:archive"}}]
    assert revoked == [("10.20.1.90", "alice", "TEMPACCESS")]
    assert client_args[0][1]["aws_secret_access_key"] == "temp-secret"
    with db.SessionLocal() as session:
        audit = session.get(ObjectStorageAuditEntry, executed.json()["request_id"])
        assert audit.result == "succeeded"
        assert audit.action == "create_bucket"
        assert "temp-secret" not in audit.preview


def test_create_bucket_rejects_operator_and_invalid_payload(dashboard_client, monkeypatch):
    _login_operator(dashboard_client)
    body = {"name": "Bad_Name", "owner": "alice", "endpoint": "https://rgw.example.test"}
    assert dashboard_client.post("/api/object-storage/buckets/actions/preview", json=body).status_code == 403
    _login(dashboard_client)
    assert dashboard_client.post("/api/object-storage/buckets/actions/preview", json=body).status_code == 400


def test_create_bucket_fails_closed_before_audit_on_mixed_ceph(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": None, "is_mixed": True,
    })
    _login(dashboard_client)
    body = {"name": "team-archive", "owner": "alice", "endpoint": "https://rgw.example.test",
            "confirmation": "team-archive"}
    response = dashboard_client.post("/api/object-storage/buckets/actions/execute", json=body)
    assert response.status_code == 502
    with db.SessionLocal() as session:
        assert session.query(ObjectStorageAuditEntry).filter_by(action="create_bucket").count() == 0


def test_bucket_quota_governance_uses_closed_command_and_audit(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "18.2.4", "is_mixed": False,
    })
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["team-archive"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: {
        **_stats(owner="alice"), "object_lock_enabled": True,
    })
    calls = []
    monkeypatch.setattr(object_storage_route, "execute_bucket_quota", lambda *args: calls.append(args))
    _login(dashboard_client)
    body = {"action": "quota_set", "bucket": "team-archive", "max_size_bytes": 2048,
            "max_objects": 100}

    preview = dashboard_client.post("/api/object-storage/buckets/governance/preview", json=body)
    executed = dashboard_client.post("/api/object-storage/buckets/governance/execute",
                                     json={**body, "confirmation": "team-archive"})

    assert preview.status_code == 200
    assert "cần enable" in preview.json()["preview"]
    assert executed.status_code == 200
    assert calls == [("10.20.1.90", "set", "team-archive", 2048, 100)]
    with db.SessionLocal() as session:
        audit = session.get(ObjectStorageAuditEntry, executed.json()["request_id"])
        assert audit.result == "succeeded"
        assert audit.action == "quota_set"


def test_bucket_versioning_uses_s3_owner_key_and_always_revokes(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "18.2.4", "is_mixed": False,
    })
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["team-archive"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: {
        **_stats(owner="alice"), "object_lock_enabled": True,
    })
    monkeypatch.setattr(object_storage_route, "fetch_s3_user_info", lambda host, uid: {"user_id": uid})
    monkeypatch.setattr(object_storage_route, "create_s3_access_key", lambda host, uid: {
        "access_key": "TEMPACCESS", "secret_key": "temp-secret",
    })
    revoked = []
    monkeypatch.setattr(object_storage_route, "revoke_s3_access_key", lambda *args: revoked.append(args))
    calls = []

    class FakeS3:
        def put_bucket_versioning(self, **kwargs):
            calls.append(("versioning", kwargs))

        def put_object_lock_configuration(self, **kwargs):
            calls.append(("retention", kwargs))

    monkeypatch.setattr(object_storage_route.boto3, "client", lambda *args, **kwargs: FakeS3())
    _login(dashboard_client)
    body = {"action": "versioning_enable", "bucket": "team-archive", "owner": "alice",
            "endpoint": "https://rgw.example.test", "confirmation": "team-archive"}

    response = dashboard_client.post("/api/object-storage/buckets/governance/execute", json=body)
    retention = dashboard_client.post("/api/object-storage/buckets/governance/execute", json={
        "action": "retention_set", "bucket": "team-archive", "owner": "alice",
        "endpoint": "https://rgw.example.test", "mode": "COMPLIANCE", "days": 30,
        "confirmation": "team-archive",
    })

    assert response.status_code == 200
    assert retention.status_code == 200
    assert calls == [
        ("versioning", {"Bucket": "team-archive", "VersioningConfiguration": {"Status": "Enabled"}}),
        ("retention", {"Bucket": "team-archive", "ObjectLockConfiguration": {
            "ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {
                "Mode": "COMPLIANCE", "Days": 30}}}}),
    ]
    assert revoked == [("10.20.1.90", "alice", "TEMPACCESS")] * 2


def test_lifecycle_preview_dry_run_execute_and_audit(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "18.2.4", "is_mixed": False,
    })
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["team-archive"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: _stats(owner="alice"))
    monkeypatch.setattr(object_storage_route, "fetch_s3_user_info", lambda host, uid: {"user_id": uid})
    monkeypatch.setattr(object_storage_route, "create_s3_access_key", lambda host, uid: {
        "access_key": "TEMPACCESS", "secret_key": "temp-secret",
    })
    revoked = []
    monkeypatch.setattr(object_storage_route, "revoke_s3_access_key", lambda *args: revoked.append(args))
    applied = []

    class FakeS3:
        def get_bucket_lifecycle_configuration(self, **kwargs):
            return {"Rules": [{"ID": "old-rule", "Status": "Enabled", "Filter": {"Prefix": "old/"}}]}

        def list_objects_v2(self, **kwargs):
            return {"IsTruncated": False, "Contents": [
                {"Key": "logs/old.log", "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc)},
                {"Key": "images/new.jpg", "LastModified": datetime(2026, 8, 17, tzinfo=timezone.utc)},
            ]}

        def put_bucket_lifecycle_configuration(self, **kwargs):
            applied.append(kwargs)

    monkeypatch.setattr(object_storage_route.boto3, "client", lambda *args, **kwargs: FakeS3())
    _login(dashboard_client)
    body = {"action": "lifecycle_put", "bucket": "team-archive", "owner": "alice",
            "endpoint": "https://rgw.example.test", "rules": [{
                "id": "expire-logs", "prefix": "logs/", "status": "Enabled", "expiration_days": 90,
            }]}

    preview = dashboard_client.post("/api/object-storage/buckets/lifecycle/preview", json=body)
    executed = dashboard_client.post("/api/object-storage/buckets/lifecycle/execute",
                                     json={**body, "confirmation": "team-archive"})

    assert preview.status_code == 200
    assert preview.json()["dry_run"]["scanned_objects"] == 2
    assert preview.json()["dry_run"]["estimated_current_objects_affected"] == 1
    assert executed.status_code == 200
    assert applied == [{"Bucket": "team-archive", "LifecycleConfiguration": {"Rules": [{
        "ID": "expire-logs", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
        "Expiration": {"Days": 90},
    }]}}]
    assert revoked == [("10.20.1.90", "alice", "TEMPACCESS")] * 2
    with db.SessionLocal() as session:
        audit = session.get(ObjectStorageAuditEntry, executed.json()["request_id"])
        assert audit.result == "succeeded"
        assert "temp-secret" not in audit.preview


def test_lifecycle_schema_rejects_duplicate_ids_and_unknown_actionless_rules(dashboard_client):
    _login(dashboard_client)
    base = {"action": "lifecycle_put", "bucket": "team-archive", "owner": "alice",
            "endpoint": "https://rgw.example.test"}
    duplicate = dashboard_client.post("/api/object-storage/buckets/lifecycle/preview", json={**base, "rules": [
        {"id": "same", "prefix": "a/", "expiration_days": 1},
        {"id": "same", "prefix": "b/", "expiration_days": 2},
    ]})
    actionless = dashboard_client.post("/api/object-storage/buckets/lifecycle/preview", json={
        **base, "rules": [{"id": "no-action", "prefix": "logs/"}],
    })
    assert duplicate.status_code == 400
    assert actionless.status_code == 400


def test_bucket_policy_preview_diff_execute_and_public_confirmation(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "18.2.4", "is_mixed": False,
    })
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["team-archive"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: _stats(owner="alice"))
    monkeypatch.setattr(object_storage_route, "fetch_s3_user_info", lambda host, uid: {"user_id": uid})
    monkeypatch.setattr(object_storage_route, "create_s3_access_key", lambda host, uid: {
        "access_key": "TEMPACCESS", "secret_key": "temp-secret",
    })
    revoked = []
    monkeypatch.setattr(object_storage_route, "revoke_s3_access_key", lambda *args: revoked.append(args))
    applied = []

    class FakeS3:
        def get_bucket_policy(self, **kwargs):
            return {"Policy": json.dumps({"Version": "2012-10-17", "Statement": [{
                "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject",
                "Resource": "arn:aws:s3:::team-archive/*",
            }]})}

        def get_bucket_acl(self, **kwargs):
            return {"Owner": {"ID": "alice"}, "Grants": []}

        def put_bucket_policy(self, **kwargs):
            applied.append(kwargs)

    monkeypatch.setattr(object_storage_route.boto3, "client", lambda *args, **kwargs: FakeS3())
    _login(dashboard_client)
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": "*", "Action": ["s3:GetObject"],
        "Resource": ["arn:aws:s3:::team-archive/*"],
    }]}
    body = {"action": "policy_put", "bucket": "team-archive", "owner": "alice",
            "endpoint": "https://rgw.example.test", "policy": policy}

    preview = dashboard_client.post("/api/object-storage/buckets/policy-acl/preview", json=body)
    weak = dashboard_client.post("/api/object-storage/buckets/policy-acl/execute",
                                 json={**body, "confirmation": "team-archive"})
    executed = dashboard_client.post("/api/object-storage/buckets/policy-acl/execute",
                                     json={**body, "confirmation": "PUBLIC:team-archive"})

    assert preview.status_code == 200
    assert preview.json()["public_access"] is True
    assert preview.json()["confirmation_required"] == "PUBLIC:team-archive"
    assert preview.json()["diff"]["before_policy"]["Statement"][0]["Effect"] == "Deny"
    assert weak.status_code == 400
    assert executed.status_code == 200
    assert json.loads(applied[0]["Policy"]) == policy
    assert revoked == [("10.20.1.90", "alice", "TEMPACCESS")] * 2


def test_bucket_policy_rejects_unknown_reef_action_and_cross_bucket_resource(dashboard_client):
    _login(dashboard_client)
    base = {"action": "policy_put", "bucket": "team-archive", "owner": "alice",
            "endpoint": "https://rgw.example.test"}
    unknown = dashboard_client.post("/api/object-storage/buckets/policy-acl/preview", json={**base, "policy": {
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*",
            "Action": "s3:DefinitelyNotReal", "Resource": "arn:aws:s3:::team-archive/*"}],
    }})
    cross_bucket = dashboard_client.post("/api/object-storage/buckets/policy-acl/preview", json={**base, "policy": {
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*",
            "Action": "s3:GetObject", "Resource": "arn:aws:s3:::other-bucket/*"}],
    }})
    assert unknown.status_code == 400
    assert cross_bucket.status_code == 400


def test_inventory_page_keeps_auth_and_cluster_lifecycle_navigation(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: [])
    _login(dashboard_client)

    response = dashboard_client.get("/object-storage/buckets")

    assert response.status_code == 200
    assert 'href="/openstack/auth-pool"' in response.text
    assert 'href="/deploy-cluster"' in response.text
    assert 'href="/delete-cluster"' in response.text
    assert 'href="/upgrade"' in response.text
    assert 'href="/patch"' in response.text
    assert 'href="/convert-cluster"' in response.text


def test_inventory_api_searches_paginates_and_returns_bucket_stats(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "PAGE_SIZE", 2)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["archive", "images", "volumes"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: _stats(owner=f"owner-{name}"))
    _login(dashboard_client)

    page_two = dashboard_client.get("/api/object-storage/buckets?page=2")
    assert page_two.status_code == 200
    body = page_two.json()
    assert body["total"] == 3
    assert body["page"] == 2
    assert [row["name"] for row in body["items"]] == ["volumes"]
    assert body["items"][0]["owner"] == "owner-volumes"
    assert body["items"][0]["size"] == "1.0 KiB"

    filtered = dashboard_client.get("/api/object-storage/buckets?query=ima")
    assert filtered.status_code == 200
    assert [row["name"] for row in filtered.json()["items"]] == ["images"]


def test_inventory_keeps_other_rows_when_one_bucket_stats_query_fails(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["good", "gone"])

    def stats(host, name):
        if name == "gone":
            raise RgwLogError("bucket disappeared")
        return _stats()

    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", stats)
    _login(dashboard_client)

    response = dashboard_client.get("/api/object-storage/buckets")

    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()["items"]}
    assert rows["good"]["stats_available"] is True
    assert rows["gone"] == {"name": "gone", "stats_available": False, "stats_error": "bucket disappeared"}


def test_non_admin_operator_can_use_read_only_inventory(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["operator-visible"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: _stats())
    _login_operator(dashboard_client)

    api = dashboard_client.get("/api/object-storage/buckets")
    page = dashboard_client.get("/object-storage/buckets")

    assert api.status_code == 200
    assert api.json()["items"][0]["name"] == "operator-visible"
    assert page.status_code == 200
    assert "operator-visible" in page.text


def test_metadata_filter_limit_fails_before_stats_fanout(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "MAX_METADATA_SCAN", 2)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["a", "b", "c"])
    stats_calls = []
    monkeypatch.setattr(
        object_storage_route, "fetch_bucket_stats", lambda host, name: stats_calls.append(name) or _stats()
    )
    _login(dashboard_client)

    api = dashboard_client.get("/api/object-storage/buckets?owner=team-a")
    page = dashboard_client.get("/object-storage/buckets?owner=team-a")

    assert api.status_code == 502
    assert "tối đa 2 bucket" in api.json()["detail"]
    assert page.status_code == 200
    assert "tối đa 2 bucket" in page.text
    assert stats_calls == []


def test_bucket_stats_error_redacts_s3_credentials(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["broken"])

    def fail_stats(host, name):
        raise RgwLogError("access_key=AKIA_TEST secret_access_key:super-secret session_token token-value")

    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", fail_stats)
    _login(dashboard_client)

    response = dashboard_client.get("/api/object-storage/buckets")

    assert response.status_code == 200
    error = response.json()["items"][0]["stats_error"]
    assert error.count("[REDACTED]") == 3
    assert "AKIA_TEST" not in error
    assert "super-secret" not in error
    assert "token-value" not in error


def test_bucket_detail_is_read_only_and_returns_404_for_unknown_bucket(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["images"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: _stats() if name == "images" else None)
    _login(dashboard_client)

    response = dashboard_client.get("/api/object-storage/buckets/images")
    assert response.status_code == 200
    assert response.json()["name"] == "images"
    assert response.json()["owner"] == "operator"

    missing = dashboard_client.get("/api/object-storage/buckets/no-such-bucket")
    assert missing.status_code == 404


def test_secondary_cluster_uses_its_own_rgw_connection(dashboard_client, monkeypatch):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = Cluster(
            name="cluster-rgw-2", ceph_mon_nodes="10.99.0.10", ceph_rgw_nodes="10.99.0.90",
            ceph_container_name="mon-2", ceph_rgw_container_name="rgw-2",
            ssh_user="ceph2", ssh_key_path="/keys/ceph2", ceph_exec_mode="docker",
            is_default=False, is_active=True,
        )
        session.add(cluster)
        session.commit()
        cluster_id = cluster.id
    calls = []
    monkeypatch.setattr(
        object_storage_route, "fetch_bucket_list_with",
        lambda *args: calls.append(args) or ["secondary-bucket"],
    )
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats_with", lambda *args: _stats())

    response = dashboard_client.get(f"/api/object-storage/buckets?cluster={cluster_id}")

    assert response.status_code == 200
    assert calls == [("10.99.0.90", "ceph2", "/keys/ceph2", "docker", "rgw-2")]
    assert response.json()["items"][0]["name"] == "secondary-bucket"


def test_inventory_filters_metadata_and_sorts_before_pagination(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "PAGE_SIZE", 1)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["empty", "small", "large"])
    stats = {
        "empty": _stats(owner="other", size=0, objects=0),
        "small": _stats(owner="team-a", size=10, objects=1),
        "large": _stats(owner="team-a", size=100, objects=8),
    }
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: stats[name])
    _login(dashboard_client)

    response = dashboard_client.get(
        "/api/object-storage/buckets?owner=TEAM-A&usage=nonempty&sort=size&order=desc&page=2"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page_count"] == 2
    assert [row["name"] for row in body["items"]] == ["small"]


def test_bucket_detail_links_to_prefilled_access_log(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["team bucket"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: _stats())
    monkeypatch.setattr(object_storage_route, "fetch_bucket_access_log", lambda host, name: [])
    _login(dashboard_client)

    detail = dashboard_client.get("/object-storage/buckets/team%20bucket")
    assert detail.status_code == 200
    assert "&amp;bucket=team%20bucket" in detail.text

    access_log = dashboard_client.get("/bucket-access-log?bucket=team%20bucket")
    assert access_log.status_code == 200
    assert 'id="bal-bucket" value="team bucket"' in access_log.text


def test_bucket_detail_shows_unknown_optional_capabilities_truthfully(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["archive"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: _stats())
    monkeypatch.setattr(object_storage_route, "fetch_bucket_access_log", lambda host, name: [])
    _login(dashboard_client)

    response = dashboard_client.get("/object-storage/buckets/archive")

    assert response.status_code == 200
    assert response.text.count("RGW không cung cấp qua bucket stats") == 2
    assert "Không có trong bucket stats" in response.text


def test_bucket_detail_summarizes_request_and_error_trend(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(object_storage_route, "fetch_bucket_list", lambda host: ["archive"])
    monkeypatch.setattr(object_storage_route, "fetch_bucket_stats", lambda host, name: _stats())
    monkeypatch.setattr(object_storage_route, "fetch_bucket_access_log", lambda host, name: [
        {"timestamp": datetime(2026, 8, 17, 1, 10, tzinfo=timezone.utc), "status": 200},
        {"timestamp": datetime(2026, 8, 17, 1, 20, tzinfo=timezone.utc), "status": 404},
        {"timestamp": datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc), "status": 503},
    ])
    _login(dashboard_client)

    response = dashboard_client.get("/object-storage/buckets/archive")

    assert response.status_code == 200
    assert "3</strong> request" in response.text
    assert "2</strong> lỗi HTTP 4xx/5xx" in response.text
    assert "66.7%" in response.text
    assert response.text.count("2026-08-17T0") >= 2
