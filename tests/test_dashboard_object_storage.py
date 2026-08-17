import dashboard.routes.object_storage as object_storage_route
from config.settings import settings
from shared import db
from shared.models import Cluster
from watcher.rgw_access_log import RgwLogError


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


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
