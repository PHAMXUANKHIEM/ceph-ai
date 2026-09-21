"""Tests for the read-only CRUSH placement advisor."""

from watcher.crush_placement_advisor import build_crush_placement_advisor


def _tree():
    leaf_a = {
        "id": 1, "name": "osd.1", "type": "osd",
        "weight_normalized": 1.0, "bytes_used": 900, "pgs": 10, "children": [],
    }
    leaf_b = {
        "id": 2, "name": "osd.2", "type": "osd",
        "weight_normalized": 1.0, "bytes_used": 100, "pgs": 10, "children": [],
    }
    host_a = {
        "id": 10, "name": "host-a", "type": "host",
        "weight_normalized": 1.0, "bytes_used": 900, "children": [leaf_a],
    }
    host_b = {
        "id": 11, "name": "host-b", "type": "host",
        "weight_normalized": 1.0, "bytes_used": 100, "children": [leaf_b],
    }
    parent = {
        "id": -1, "name": "default", "type": "root",
        "weight_normalized": 2.0, "children": [host_a, host_b],
    }
    return {
        "state": "ok",
        "roots": [parent],
        "rules": [{
            "rule_name": "replicated_rule",
            "steps": [{"op": "chooseleaf_firstn", "type": "host"}],
        }],
    }


def test_reports_weight_usage_skew_without_mutation():
    result = build_crush_placement_advisor(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        crush_snapshot=_tree(),
    )

    assert result["status"] == "observed"
    assert result["findings"][0]["code"] == "CRUSH_WEIGHT_USAGE_SKEW"
    assert result["findings"][0]["target"]["name"] == "host-a"
    assert result["read_only"] is True
    assert result["action_id"] is None
    assert result["assumptions"]["data_movement_estimate"] == "unknown"


def test_detects_rule_failure_domain_mismatch():
    snapshot = _tree()
    snapshot["rules"][0]["steps"][0]["type"] = "rack"

    result = build_crush_placement_advisor(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        crush_snapshot=snapshot,
    )

    codes = {finding["code"] for finding in result["findings"]}
    assert "CRUSH_RULE_DOMAIN_MISMATCH" in codes


def test_missing_snapshot_fails_closed():
    result = build_crush_placement_advisor(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        crush_snapshot=None,
    )

    assert result["status"] == "not_available"
    assert result["findings"] == []
    assert "CRUSH_SNAPSHOT_UNAVAILABLE" in {
        gap["code"] for gap in result["evidence_gaps"]
    }


def test_no_command_or_secret_material_is_returned():
    snapshot = _tree()
    snapshot["secret_key"] = "MUST-NOT-LEAK"

    result = build_crush_placement_advisor(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        crush_snapshot=snapshot,
    )

    assert "MUST-NOT-LEAK" not in repr(result)
    assert result["limits"]["mutation_commands_included"] is False
