"""Tests for the read-only RGW security insight contract."""

from datetime import datetime

from watcher.rgw_security_insight import build_security_insights


def _snapshot(*keys, refreshing=False):
    return {
        "refreshing": refreshing,
        "items": [{
            "uid": "alice",
            "access_keys": list(keys),
        }],
    }


def test_reports_only_old_active_keys():
    result = build_security_insights(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        user_snapshot=_snapshot(
            {"created_at": "2025-01-01T00:00:00Z", "status": "active"},
            {"created_at": "2025-01-01T00:00:00Z", "status": "revoked"},
        ),
        now=datetime(2025, 4, 15),
        key_rotation_days=90,
    )

    assert result["status"] == "observed"
    assert result["summary"]["keys_scanned"] == 2
    assert result["summary"]["active_keys_scanned"] == 1
    assert result["summary"]["rotation_gap_count"] == 1
    finding = result["findings"][0]
    assert finding["code"] == "ACCESS_KEY_ROTATION_GAP"
    assert finding["target"] == {"type": "s3_user", "uid": "alice"}
    assert finding["read_only"] is True
    assert finding["action_id"] is None


def test_missing_created_at_is_an_evidence_gap_not_a_finding():
    result = build_security_insights(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        user_snapshot=_snapshot({"status": "active"}),
        now=datetime(2025, 4, 15),
    )

    assert result["findings"] == []
    assert any(
        gap["code"] == "ACCESS_KEY_CREATED_AT_MISSING"
        for gap in result["evidence_gaps"]
    )


def test_empty_or_refreshing_inventory_fails_closed():
    result = build_security_insights(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        user_snapshot={"items": [], "refreshing": True},
        now=datetime(2025, 4, 15),
    )

    assert result["status"] == "not_available"
    assert result["findings"] == []
    codes = {gap["code"] for gap in result["evidence_gaps"]}
    assert "S3_USER_INVENTORY_UNAVAILABLE" in codes
    assert "S3_USER_INVENTORY_REFRESHING" in codes


def test_output_never_contains_raw_credentials():
    result = build_security_insights(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        user_snapshot=_snapshot({
            "access_key": "AKIA-RAW-MUST-NOT-LEAK",
            "secret_key": "SECRET-MUST-NOT-LEAK",
            "created_at": "2024-01-01T00:00:00Z",
            "status": "active",
        }),
        now=datetime(2025, 4, 15),
    )

    serialized = repr(result)
    assert "AKIA-RAW-MUST-NOT-LEAK" not in serialized
    assert "SECRET-MUST-NOT-LEAK" not in serialized
    assert result["limits"]["secrets_included"] is False
