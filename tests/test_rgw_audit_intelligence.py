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


def test_rgw_metrics_api_is_bounded_and_does_not_infer_quota(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        object_storage_route,
        "_cached_rgw_audit_intelligence",
        lambda cluster: {
            "cluster_id": cluster.id, "captured_at": "2026-09-20T10:00:00Z",
            "status": "analyzed", "collection": {"status": "observed"}, "cache": {},
            "window": {"record_count": 10, "observed": {
                "bytes_total": 2048, "status_counts": {"200": 8, "403": 2},
                "latency_median_ms": 4.0, "latency_p95_ms": 19.0,
                "bucket_counts": {"archive": 10}, "requester_counts": {"operator": 10},
                "user_agent_counts": {"aws-cli": 10}, "time_start": "a", "time_end": "b",
            }},
            "evidence_gaps": [],
        },
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get("/api/object-storage/rgw-metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["metrics"]["request_count"] == 10
    assert body["metrics"]["error_count"] == 2
    assert body["metrics"]["error_rate_percent"] == 20.0
    assert body["metrics"]["top_buckets"] == {"archive": 10}
    assert body["action_id"] is None
    assert body["read_only"] is True
    assert any("quota" in gap.casefold() for gap in body["evidence_gaps"])


def test_rgw_metrics_export_reuses_bounded_secret_free_payload(dashboard_client, monkeypatch):
    monkeypatch.setattr(object_storage_route, "_rgw_metrics_payload", lambda cluster: {
        "cluster_id": cluster.id, "captured_at": "2026-09-20T10:00:00Z", "source": "rgw_audit_log",
        "metrics": {"request_count": 2, "bytes_total": 128, "top_buckets": {"archive": 2}},
        "evidence_gaps": ["test gap"], "read_only": True, "action_id": None,
    })
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    json_response = dashboard_client.get("/api/object-storage/rgw-metrics/export?format=json")
    csv_response = dashboard_client.get("/api/object-storage/rgw-metrics/export?format=csv")

    assert json_response.status_code == 200
    assert json_response.json()["metrics"]["request_count"] == 2
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=rgw-metrics.csv" in csv_response.headers["content-disposition"]
    assert "metrics.top_buckets,archive,2" in csv_response.text
    assert "secret" not in csv_response.text.casefold()
