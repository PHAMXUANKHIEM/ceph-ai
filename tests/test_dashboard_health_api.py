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


def test_dashboard_health_api_never_returns_sample_counts_on_query_failure(dashboard_client, monkeypatch):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    def fake(*args):
        raise incidents.CephQueryError("all MON nodes failed")

    monkeypatch.setattr(incidents, "run_ceph_json_command_with", fake)
    response = dashboard_client.get("/api/dashboard/health")

    assert response.status_code == 502
    assert response.json()["detail"] == "all MON nodes failed"
