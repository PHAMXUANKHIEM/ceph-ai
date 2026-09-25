from shared.reliability import collect_reliability


def test_reliability_contract_is_read_only_and_bounded():
    payload = collect_reliability()
    assert payload["schema"] == "ceph-ai.reliability.v1"
    assert payload["runtime_owner"]["owner"]
    assert payload["slo"]["window"] == "30d"
    assert set(payload["slo"]) >= {"window", "dashboard_freshness", "incident_processing"}
    assert isinstance(payload["queues"], list)
    assert isinstance(payload["alerts"], list)
    assert len(payload["failed_deploys_24h"]) <= 20


def test_reliability_endpoint_requires_admin(dashboard_client):
    response = dashboard_client.get("/api/system/reliability", follow_redirects=False)
    assert response.status_code == 303
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/system/reliability")
    assert response.status_code in {200, 503}
    assert response.json()["schema"] == "ceph-ai.reliability.v1"
