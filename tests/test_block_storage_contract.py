import dashboard.routes.block_storage as block_storage_route


def test_block_storage_contract_requires_authentication(dashboard_client):
    response = dashboard_client.get("/api/v1/block-storage/contract", follow_redirects=False)
    assert response.status_code in {303, 307}


def test_block_storage_contract_is_versioned_and_fail_closed(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        block_storage_route,
        "cluster_selection",
        lambda _request: ([], type("Cluster", (), {"id": "cluster-contract"})()),
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get("/api/v1/block-storage/contract")

    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "v1"
    assert body["cluster_id"] == "cluster-contract"
    assert body["mutation"]["direct_execution"] is False
    assert body["mutation"]["required_header"] == "Idempotency-Key"
    assert body["iac"]["policy_bypass"] is False
