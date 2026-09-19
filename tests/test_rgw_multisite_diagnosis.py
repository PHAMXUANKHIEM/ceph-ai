from types import SimpleNamespace

import dashboard.routes.object_storage as object_storage_route
from watcher import rgw_evidence
from watcher.rgw_multisite_diagnosis import build_multisite_diagnosis


def _section(details, source, status="observed"):
    return {"status": status, "source": source, "details": details}


def _evidence(sync, *, period=None, sync_errors=None, gaps=None):
    return {
        "status": "ready",
        "topology": {
            "realm": _section({"id": "realm-1", "current_period": "period-7"}, "rgw_realm"),
            "zonegroup": _section({"master_zone": "zone-a", "period": "period-7"}, "rgw_zonegroup"),
            "zone": _section({"id": "zone-a", "epoch": 7}, "rgw_zone"),
        },
        "sync": _section(sync, "rgw_sync_status"),
        "sync_errors": _section(sync_errors or {"errors": []}, "rgw_sync_errors"),
        "period": _section(period or {"id": "period-7", "epoch": 7, "master_zone": "zone-a"}, "rgw_period"),
        "evidence_gaps": gaps or [],
    }


def test_multisite_diagnosis_reports_lag_and_keeps_read_only_contract():
    result = build_multisite_diagnosis(_evidence({"sync_status": "syncing", "lag_seconds": 420}))

    assert result["status"] == "diagnosed"
    assert result["observed"]["lag"]["seconds"] == 420
    assert result["findings"][0]["code"] == "RGW_MULTISITE_REPLICATION_LAG"
    assert result["read_only"] is True
    assert result["action_id"] is None


def test_multisite_diagnosis_detects_shard_error_conflict_and_period_mismatch():
    result = build_multisite_diagnosis(_evidence(
        {"sync_status": "error", "conflicts": 2, "sources": [{"failed_shards": 1}]},
        period={"id": "period-8", "epoch": 8, "master_zone": "zone-b"},
        sync_errors={"errors": [{"shard": 3, "message": "failed"}]},
    ))

    codes = {item["code"] for item in result["findings"]}
    assert "RGW_MULTISITE_SHARD_ERROR" in codes
    assert "RGW_MULTISITE_CONFLICT" in codes
    assert "RGW_MULTISITE_PERIOD_EPOCH_MISMATCH" in codes
    assert "RGW_MULTISITE_MASTER_STATE_CONFLICT" in codes


def test_multisite_diagnosis_fails_closed_when_period_and_sync_are_missing():
    evidence = _evidence({}, gaps=["sync unavailable"])
    evidence["sync"] = _section({}, "rgw_sync_status", status="not_available")
    evidence["period"] = _section({}, "rgw_period", status="not_available")

    result = build_multisite_diagnosis(evidence)

    assert result["status"] == "insufficient_evidence"
    assert result["findings"] == []
    assert result["evidence_gaps"]


def test_multisite_diagnosis_api_is_authenticated_and_cluster_scoped(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        object_storage_route,
        "get_rgw_evidence",
        lambda cluster: {
            "status": "partial",
            "cluster_id": cluster.id,
            "captured_at": "2026-09-19T10:00:00Z",
            "read_only": True,
            "action_id": None,
            "evidence_gaps": ["test"],
        },
    )
    monkeypatch.setattr(
        object_storage_route,
        "selected_cluster",
        lambda _request: SimpleNamespace(id="cluster-1"),
    )
    response = dashboard_client.get("/api/object-storage/multisite-diagnosis", follow_redirects=False)
    assert response.status_code in {303, 307}

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/object-storage/multisite-diagnosis")
    assert response.status_code == 200
    assert response.json()["cluster_id"] == "cluster-1"
    assert response.json()["read_only"] is True
    assert response.json()["action_id"] is None
