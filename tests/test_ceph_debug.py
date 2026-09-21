from shared.ceph_runner import get_metrics


def test_ceph_latency_debug_requires_login(dashboard_client):
    response = dashboard_client.get("/api/debug/ceph-latency", follow_redirects=False)
    assert response.status_code == 303


def test_ceph_latency_debug_is_admin_only(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get(
        "/api/debug/ceph-latency",
        headers={"X-Request-ID": "test-correlation-id"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-correlation-id"
    assert "metrics" in response.json()
    assert "recent" in response.json()["metrics"]
    assert "cache" in response.json()
    assert "snapshot_collector" in response.json()
    assert "api" in response.json()
    assert "retry" in response.json()
    assert "snapshot_freshness" in response.json()
    assert "mon_circuit" in response.json()
    assert "cephadm_circuit" in response.json()
    assert "event_bus" in response.json()
    assert "operational_alerts" in response.json()
    assert "cache_storage" in response.json()
    assert response.json()["snapshot_freshness"]["cluster_count"] >= 1
    assert response.json()["api"]["recent"]


def test_request_id_with_header_control_characters_is_replaced(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get(
        "/api/debug/ceph-latency",
        headers={"X-Request-ID": "bad value"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "bad value"
    assert len(response.headers["X-Request-ID"]) == 32


def test_runner_metrics_do_not_include_credentials():
    metrics = get_metrics()
    serialized = repr(metrics).lower()
    assert "private_key" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized
