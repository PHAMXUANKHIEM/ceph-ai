import dashboard.routes.object_storage as object_storage_route
from datetime import datetime, timezone
import bcrypt
from config.settings import settings
from shared import db
from shared.models import Cluster, User
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
