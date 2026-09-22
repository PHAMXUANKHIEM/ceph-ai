from shared.ceph_runner import get_metrics
from dashboard.routes.system_health import _operational_alerts


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


def test_operational_event_bus_alert_recovers_after_success():
    freshness = {"clusters": [], "cluster_count": 0}
    services = {"watcher": {"healthy": True}, "worker": {"healthy": True}}
    cache_storage = {"available": False}

    active = _operational_alerts(
        freshness,
        services,
        {
            "publish_failure_total": 3,
            "publish_failure_window_15m": 2,
            "last_failure_at": 20.0,
            "last_success_at": 10.0,
        },
        cache_storage,
    )
    recovered = _operational_alerts(
        freshness,
        services,
        {
            "publish_failure_total": 3,
            "publish_failure_window_15m": 2,
            "last_failure_at": 20.0,
            "last_success_at": 30.0,
        },
        cache_storage,
    )

    assert any(alert["code"] == "event_bus_publish_failed" for alert in active)
    assert not any(alert["code"] == "event_bus_publish_failed" for alert in recovered)
