"""Tests for read-only inconsistent-object analysis."""

from watcher.inconsistent_object_analysis import analyze_inconsistent_objects


def test_classifies_inconsistent_pg_and_locks_repair():
    result = analyze_inconsistent_objects(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pg_rows=[{
            "pgid": "1.a",
            "pool": "data",
            "state": "active+inconsistent",
            "inconsistent_objects": ["1234567890abcdef"],
        }],
    )

    finding = result["findings"][0]
    assert finding["severity"] == "high"
    assert finding["repair_classification"] == "DESTRUCTIVE/RISKY"
    assert finding["repair_allowed"] is False
    assert result["repair_policy"]["automatic_repair"] is False


def test_escalates_unfound_or_corrupt_evidence():
    result = analyze_inconsistent_objects(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pg_rows=[{
            "pgid": "2.b",
            "state": "active+unfound",
        }],
    )

    assert result["findings"][0]["severity"] == "critical"
    assert "OBJECT_LEVEL_EVIDENCE_UNAVAILABLE" in {
        gap["code"] for gap in result["evidence_gaps"]
    }


def test_missing_object_details_is_an_evidence_gap():
    result = analyze_inconsistent_objects(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pg_rows=[{"pgid": "1.a", "state": "active+degraded"}],
    )

    assert result["findings"] == []
    assert "OBJECT_LEVEL_EVIDENCE_UNAVAILABLE" in {
        gap["code"] for gap in result["evidence_gaps"]
    }


def test_no_repair_command_or_secret_material_is_returned():
    result = analyze_inconsistent_objects(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pg_rows=[{
            "pgid": "1.a",
            "state": "active+inconsistent",
            "secret_key": "MUST-NOT-LEAK",
        }],
    )

    assert "MUST-NOT-LEAK" not in repr(result)
    assert result["repair_policy"]["commands_included"] is False
