from shared.ceph_runner import get_metrics


def test_ceph_latency_debug_requires_login(dashboard_client):
    response = dashboard_client.get("/api/debug/ceph-latency", follow_redirects=False)
    assert response.status_code == 303


def test_ceph_latency_debug_is_admin_only(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/debug/ceph-latency")
    assert response.status_code == 200
    assert "metrics" in response.json()
    assert "recent" in response.json()["metrics"]


def test_runner_metrics_do_not_include_credentials():
    metrics = get_metrics()
    serialized = repr(metrics).lower()
    assert "private_key" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized
