import dashboard.routes.object_storage as object_storage_route

from watcher.rgw_bucket_diagnosis import build_bucket_access_diagnosis


def test_diagnosis_distinguishes_auth_policy_quota_and_backend_failures():
    records = [
        {"status": 401, "method": "GET", "action": "Tải xuống"},
        {"status": 403, "method": "PUT", "action": "Tải lên"},
        {"status": 503, "method": "GET", "action": "Tải xuống"},
    ]
    result = build_bucket_access_diagnosis(
        records,
        bucket="archive",
        operation="create",
        bucket_stats={
            "quota_enabled": True,
            "size_bytes": 100,
            "quota_max_size_bytes": 100,
            "num_objects": 2,
            "quota_max_objects": 10,
        },
        rgw_evidence={"endpoints": {"status": "observed"}},
    )

    codes = {finding["code"] for finding in result["findings"]}
    assert result["status"] == "diagnosed"
    assert {"S3_AUTHENTICATION_FAILURE", "S3_QUOTA_REACHED", "RGW_BACKEND_OR_SERVICE_FAILURE"} <= codes
    assert all(finding["action_id"] is None for finding in result["findings"])
    assert result["recommendation_mode"] == "ADVISORY"


def test_diagnosis_does_not_guess_when_access_log_is_empty():
    result = build_bucket_access_diagnosis(
        [],
        bucket="missing",
        operation="delete",
        rgw_evidence={"endpoints": {"status": "inferred"}},
    )

    assert result["status"] == "insufficient_evidence"
    assert result["findings"] == []
    assert any("access log" in gap for gap in result["evidence_gaps"])
    assert any("DNS/TLS" in gap for gap in result["evidence_gaps"])


def test_bucket_diagnosis_api_is_read_only_and_cluster_scoped(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        object_storage_route,
        "_rgw_hosts",
        lambda _cluster: [{"host": "rgw-1", "roles": ["RGW"]}],
    )
    monkeypatch.setattr(
        object_storage_route,
        "fetch_bucket_access_log",
        lambda _host, _bucket: [{"status": 403, "method": "GET", "action": "Liệt kê"}],
    )
    monkeypatch.setattr(
        object_storage_route,
        "fetch_bucket_stats",
        lambda _host, _bucket: {
            "usage": {"rgw.main": {"num_objects": 1, "size_utilized": 10}},
            "bucket_quota": {"enabled": True, "max_size": 100, "max_objects": 10},
        },
    )
    monkeypatch.setattr(
        object_storage_route,
        "get_rgw_evidence",
        lambda cluster: {"status": "ready", "cluster_id": cluster.id, "endpoints": {"status": "observed"}},
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get(
        "/api/object-storage/buckets/archive/diagnosis?operation=list"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cluster_id"]
    assert body["status"] == "diagnosed"
    assert body["findings"][0]["code"] == "S3_POLICY_OR_PERMISSION_DENIED"
    assert body["read_only"] is True
    assert body["action_id"] is None
