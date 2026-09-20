from types import SimpleNamespace

import dashboard.routes.object_storage as object_storage_route
from watcher.rgw_audit_intelligence import build_rgw_audit_intelligence


def _row(**overrides):
    row = {
        "method": "GET", "status": 200, "requester": "operator",
        "remote_addr": "10.0.0.10", "user_agent": "aws-cli",
        "bucket": "archive", "latency_ms": 10, "encryption": "SSE-S3",
        "transaction_id": "tx-1",
    }
    row.update(overrides)
    return row


def test_audit_intelligence_flags_anonymous_successful_write_without_action():
    result = build_rgw_audit_intelligence([
        _row(method="PUT", requester="anonymous", remote_addr="198.51.100.4", transaction_id="tx-public"),
    ])

    finding = result["findings"][0]
    assert result["status"] == "analyzed"
    assert finding["code"] == "ANONYMOUS_SUCCESSFUL_WRITE"
    assert finding["severity"] == "critical"
    assert finding["read_only"] is True
    assert finding["action_id"] is None
    assert result["baseline"]["historical_available"] is False


def test_audit_intelligence_detects_auth_failure_burst_and_keeps_scope_bounded():
    rows = [
        _row(status=403, remote_addr="198.51.100.9", requester=f"user-{index}", transaction_id=f"tx-{index}")
        for index in range(5)
    ]
    result = build_rgw_audit_intelligence(rows, source_hosts=["rgw-1"])

    codes = {finding["code"] for finding in result["findings"]}
    assert "AUTHORIZATION_FAILURE_BURST" in codes
    assert result["window"]["source_hosts"] == ["rgw-1"]
    assert all("secret" not in str(finding).casefold() for finding in result["findings"])


def test_audit_intelligence_does_not_call_normal_traffic_anomalous():
    rows = [_row(transaction_id=f"tx-{index}", remote_addr=f"10.0.0.{index + 1}") for index in range(4)]
    result = build_rgw_audit_intelligence(rows, historical_records=rows)

    assert result["findings"] == []
    assert result["evidence_gaps"] == []
    assert result["baseline"]["method"] == "historical_rows"


def test_audit_collection_deduplicates_transaction_ids(monkeypatch):
    cluster = SimpleNamespace(id="cluster-audit", is_default=True)
    monkeypatch.setattr(object_storage_route, "_rgw_hosts", lambda _cluster: ["rgw-1", "rgw-2"])
    monkeypatch.setattr(
        object_storage_route,
        "fetch_rgw_audit_log",
        lambda _host: [_row(transaction_id="same-tx")],
    )

    result = object_storage_route._collect_rgw_audit_intelligence(cluster)

    assert result["window"]["record_count"] == 1
    assert result["collection"]["status"] == "observed"
    assert result["read_only"] is True
    assert result["action_id"] is None


def test_audit_intelligence_api_is_authenticated_and_cluster_scoped(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        object_storage_route,
        "_cached_rgw_audit_intelligence",
        lambda cluster: {
            "cluster_id": cluster.id, "status": "insufficient_evidence",
            "read_only": True, "action_id": None, "findings": [],
        },
    )
    response = dashboard_client.get("/api/object-storage/rgw-audit-intelligence", follow_redirects=False)
    assert response.status_code in {303, 307}

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/object-storage/rgw-audit-intelligence")
    assert response.status_code == 200
    assert response.json()["cluster_id"]
    assert response.json()["read_only"] is True
