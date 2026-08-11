from dashboard.routes import incidents


def test_dashboard_health_api_uses_live_daemon_counts(dashboard_client, monkeypatch):
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

    def fake(*args):
        assert args[-1] == "ceph -s"
        return "10.20.1.112", payload

    monkeypatch.setattr(incidents, "run_ceph_json_command_with", fake)
    response = dashboard_client.get("/api/dashboard/health")

    assert response.status_code == 200
    body = response.json()
    assert body["osds"] == {"up": 5, "total": 7}
    assert body["mons"] == {"up": 2, "total": 3}
    assert body["health"] == "WARN"
    assert body["utilization"]["percent"] == 21
    assert body["placement_groups"] == "OKAY"


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
    assert body["servers"] == {"online": 1, "total": 3}


def test_dashboard_health_api_never_returns_sample_counts_on_query_failure(dashboard_client, monkeypatch):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    def fake(*args):
        raise incidents.CephQueryError("all MON nodes failed")

    monkeypatch.setattr(incidents, "run_ceph_json_command_with", fake)
    response = dashboard_client.get("/api/dashboard/health")

    assert response.status_code == 502
    assert response.json()["detail"] == "all MON nodes failed"
