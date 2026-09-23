from dashboard.routes import incidents
from shared import db
from shared.cluster_snapshot import publish_snapshot
from shared.models import Cluster


def _default_cluster_id():
    with db.SessionLocal() as session:
        return session.query(Cluster).filter(Cluster.is_default.is_(True)).one().id


def test_dashboard_health_api_reads_shared_snapshot_without_ceph_query(dashboard_client, monkeypatch):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    payload = {
        "health": {"status": "HEALTH_WARN"},
        "monmap": {"mons": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
        "quorum_names": ["a", "c"],
        "osdmap": {"num_osds": 7, "num_up_osds": 5, "num_pools": 4},
        "pgmap": {
            "bytes_used": 21_490_000_000,
            "bytes_total": 100_000_000_000,
            "pgs_by_state": [{"state_name": "active+clean", "count": 64}],
        },
    }

    cluster_id = _default_cluster_id()
    publish_snapshot(cluster_id, {"health": payload})
    monkeypatch.setattr(
        incidents,
        "run_ceph_json_command_with",
        lambda *args: (_ for _ in ()).throw(AssertionError("GET must not query Ceph")),
    )

    response = dashboard_client.get("/api/dashboard/health")

    assert response.status_code == 200
    body = response.json()
    assert body["osds"] == {"up": 5, "total": 7}
    assert body["mons"] == {"up": 2, "total": 3}
    assert body["health"] == "WARN"
    assert body["utilization"]["percent"] == 21
    assert body["placement_groups"] == "OKAY"
    assert body["cluster_id"] == cluster_id
    assert body["generation"] == 1
    assert body["cached"] is True
    assert body["stale"] is False
    assert body["collected_at"]
    assert body["age_seconds"] >= 0


def test_dashboard_health_get_does_not_schedule_implicit_refresh(dashboard_client, monkeypatch):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    calls = []
    cluster_id = _default_cluster_id()
    publish_snapshot(cluster_id, {"health": {"status": "HEALTH_OK", "checks": {}}})
    monkeypatch.setattr(incidents, "_schedule_dashboard_health_refresh", lambda *_: calls.append(1))

    assert dashboard_client.get("/api/dashboard/health").status_code == 200
    assert dashboard_client.get("/api/dashboard/health").status_code == 200
    assert calls == []

    response = dashboard_client.post(f"/api/dashboard/health/refresh?cluster={cluster_id}")
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "cluster_id": cluster_id, "refreshing": True}
    assert calls == [1]


def test_dashboard_health_payload_exposes_live_metrics_and_servers():
    cluster = type("ClusterConfig", (), {
        "ceph_mon_nodes": "10.0.0.1", "ceph_mgr_nodes": "10.0.0.2",
        "ceph_osd_nodes": "10.0.0.3", "ceph_rgw_nodes": "",
    })()
    status = {
        "health": {"status": "HEALTH_OK"},
        "osdmap": {"num_osds": 3, "num_up_osds": 3},
        "monmap": {"num_mons": 1}, "quorum_names": ["a"],
        "pgmap": {
            "read_bytes_sec": 1_000, "write_bytes_sec": 2_000,
            "read_op_per_sec": 4, "write_op_per_sec": 6,
            "pgs_by_state": [{"state_name": "active+clean", "count": 8}],
        },
    }
    perf = {"osd_perf_infos": [
        {"perf_stats": {"apply_latency_ms": 2, "commit_latency_ms": 4}},
        {"perf_stats": {"apply_latency_ms": 6, "commit_latency_ms": 8}},
    ]}
    hosts = [{"hostname": "node-a", "status": ""}, {"hostname": "node-b", "status": "offline"}]

    body = incidents._dashboard_health_payload(status, cluster, perf, hosts)

    assert body["metrics"] == {"latency_ms": 5.0, "bandwidth_bps": 3000, "iops": 10}
    assert body["servers"] == {"online": 1, "total": 2}


def test_dashboard_health_payload_prefers_per_osd_up_flags():
    cluster = type("ClusterConfig", (), {
        "ceph_mon_nodes": "10.0.0.1", "ceph_mgr_nodes": "",
        "ceph_osd_nodes": "10.0.0.1", "ceph_rgw_nodes": "",
    })()
    status = {
        "health": {"status": "HEALTH_WARN"},
        "osdmap": {"num_osds": 3, "num_up_osds": 0},
        "pgmap": {}, "monmap": {"num_mons": 1}, "quorum_names": ["a"],
    }
    osd_dump = {"osds": [
        {"osd": 0, "up": 1}, {"osd": 1, "up": 1}, {"osd": 2, "up": 1},
    ]}

    body = incidents._dashboard_health_payload(status, cluster, osd_dump=osd_dump)

    assert body["osds"] == {"up": 3, "total": 3}


def test_dashboard_health_api_returns_loading_state_when_snapshot_is_missing(dashboard_client, monkeypatch):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    monkeypatch.setattr(
        incidents,
        "run_ceph_json_command_with",
        lambda *args: (_ for _ in ()).throw(AssertionError("GET must not query Ceph")),
    )
    response = dashboard_client.get("/api/dashboard/health")

    assert response.status_code == 200
    body = response.json()
    assert body["health"] == "UNKNOWN"
    assert body["cached"] is False
    assert body["generation"] == 0
    assert body["stale"] is True
    assert body["refreshing"] is False


def test_dashboard_health_api_surfaces_snapshot_refresh_error(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    cluster_id = _default_cluster_id()
    from watcher.cluster_snapshot_collector import publish_health_error

    publish_health_error(cluster_id, "MON unreachable")

    response = dashboard_client.get(f"/api/dashboard/health?cluster={cluster_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["health"] == "UNKNOWN"
    assert body["health_available"] is False
    assert body["stale"] is True
    assert body["last_error"] == "MON unreachable"

def test_dashboard_health_api_maps_flat_watcher_health_status(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    cluster_id = _default_cluster_id()
    publish_snapshot(cluster_id, {"health": {"status": "HEALTH_WARN", "checks": {}}})

    body = dashboard_client.get(f"/api/dashboard/health?cluster={cluster_id}").json()

    assert body["health"] == "WARN"
    assert body["health_available"] is True


def test_new_critical_health_overrides_old_status_section(dashboard_client):
    from shared.cluster_snapshot import publish_section_snapshot

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    cluster_id = _default_cluster_id()
    publish_section_snapshot(
        cluster_id,
        "status",
        {"health": {"status": "HEALTH_WARN"}, "osdmap": {"num_osds": 3, "num_up_osds": 3}},
    )
    publish_snapshot(cluster_id, {"health": {"status": "HEALTH_ERR", "checks": {}}})

    body = dashboard_client.get(f"/api/dashboard/health?cluster={cluster_id}").json()

    assert body["health"] == "ERR"
    assert body["osds"] == {"up": 3, "total": 3}
