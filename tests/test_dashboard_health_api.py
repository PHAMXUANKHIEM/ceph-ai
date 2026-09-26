from dashboard.routes import incidents
from dashboard import cluster_scope
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


def test_dashboard_health_error_retains_last_good_snapshot(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    cluster_id = _default_cluster_id()
    from watcher.cluster_snapshot_collector import publish_health_error

    published = publish_snapshot(
        cluster_id,
        {"health": {"status": "HEALTH_WARN", "checks": {"OSD_DOWN": {}}}},
    )
    publish_health_error(cluster_id, "MON query timed out")

    body = dashboard_client.get(f"/api/dashboard/health?cluster={cluster_id}").json()

    assert body["health"] == "WARN"
    assert body["health_available"] is True
    assert body["generation"] == published["generation"]
    assert body["collected_at"] == published["collected_at"]
    assert body["last_error"] == "MON query timed out"
    assert body["partial_errors"] == {"health": "MON query timed out"}


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


def test_cluster_resolution_collapses_concurrent_snapshot_reads(dashboard_client, monkeypatch):
    calls = []
    original = cluster_scope.list_active_clusters
    monkeypatch.setattr(
        cluster_scope,
        "list_active_clusters",
        lambda session: calls.append(1) or original(session),
    )
    cluster_scope.clear_cluster_selection_cache()

    first_clusters, first = cluster_scope.resolve_cluster_selection("", "")
    second_clusters, second = cluster_scope.resolve_cluster_selection(first.id, "")

    assert first.id == second.id
    assert [row.id for row in first_clusters] == [row.id for row in second_clusters]
    assert calls == [1]


def test_cluster_resolution_cache_has_explicit_write_invalidation(dashboard_client, monkeypatch):
    calls = []
    original = cluster_scope.list_active_clusters
    monkeypatch.setattr(
        cluster_scope,
        "list_active_clusters",
        lambda session: calls.append(1) or original(session),
    )
    cluster_scope.clear_cluster_selection_cache()

    cluster_scope.resolve_cluster_selection("", "")
    cluster_scope.clear_cluster_selection_cache()
    cluster_scope.resolve_cluster_selection("", "")

    assert calls == [1, 1]


def test_realtime_dashboard_first_paint_skips_hidden_legacy_feeds(dashboard_client, monkeypatch):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    cluster_id = _default_cluster_id()
    publish_snapshot(cluster_id, {"health": {"status": "HEALTH_OK", "checks": {}}})
    monkeypatch.setattr(incidents.settings, "dashboard_legacy_feed_enabled", False)
    monkeypatch.setattr(
        incidents,
        "_fetch_dashboard_data",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("snapshot first paint must not load hidden legacy feeds")
        ),
    )
    monkeypatch.setattr(
        incidents.heartbeat,
        "get_latest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("snapshot first paint must not query heartbeat")
        ),
    )

    response = dashboard_client.get(f"/?cluster={cluster_id}")

    assert response.status_code == 200
    assert 'id="dashboard-bootstrap-data"' in response.text
    assert "Incident Feed" not in response.text
    assert "Audit Trail" not in response.text


def _card_values(status, **kwargs):
    cluster = Cluster(
        id="c1", name="lab", ceph_mon_nodes="10.0.0.1", ceph_mgr_nodes="", ceph_osd_nodes="",
        ceph_rgw_nodes="", ceph_mon_hostnames="", ssh_user="root", ssh_key_path="/k",
        ceph_exec_mode="docker", ceph_container_name="ceph-mon", is_default=True, is_active=True,
    )
    return incidents._dashboard_health_payload(status, cluster, **kwargs)


def test_unreachable_cluster_reports_unknown_not_zero():
    payload = _card_values({})
    assert payload["mons"] == {"up": None, "total": None}
    assert payload["metrics"]["bandwidth_bps"] is None
    assert payload["metrics"]["iops"] is None
    assert payload["placement_groups"] == "UNKNOWN"
    assert payload["osds"] == {"up": None, "total": None}


def test_known_values_and_an_idle_cluster_still_report_numbers():
    payload = _card_values({
        "monmap": {"num_mons": 3},
        "quorum_names": ["a", "b"],
        "pgmap": {"pgs_by_state": [{"state_name": "active+undersized", "count": 4}]},
    })
    assert payload["mons"] == {"up": 2, "total": 3}
    # A real pgmap without rate keys means an idle cluster: zero, not unknown.
    assert payload["metrics"]["bandwidth_bps"] == 0
    assert payload["metrics"]["iops"] == 0
    assert payload["placement_groups"] == "WARN"
