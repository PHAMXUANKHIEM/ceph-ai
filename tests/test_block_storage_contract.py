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


def test_rbd_copy_contract_is_dashboard_only_and_approval_gated(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        block_storage_route,
        "cluster_selection",
        lambda _request: ([], type("Cluster", (), {"id": "cluster-contract"})()),
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    body = dashboard_client.get("/api/v1/block-storage/contract").json()
    contract = body["action_contracts"]["rbd_copy_volume"]

    assert contract["surface"] == "dashboard_worker_only"
    assert contract["incident_autopilot"] is False
    assert contract["classification"] == "RISKY"
    assert contract["target_type"] == "volume"
    assert contract["requires_approval"] is True
    assert contract["typed_params"]["snapshot"] == "explicit source snapshot"
    assert contract["source_preserved"] is True


def test_rbd_move_contract_is_destructive_and_verification_gated(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        block_storage_route,
        "cluster_selection",
        lambda _request: ([], type("Cluster", (), {"id": "cluster-contract"})()),
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    contract = dashboard_client.get("/api/v1/block-storage/contract").json()["action_contracts"]["rbd_move_volume"]
    assert contract["classification"] == "DESTRUCTIVE"
    assert contract["requires_approval"] is True
    assert contract["typed_params"]["delete_source"]
    assert contract["source_preserved_until_verified"] is True
