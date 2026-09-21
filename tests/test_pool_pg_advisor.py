"""Tests for the read-only Pool/PG advisor."""

from watcher.pool_pg_advisor import build_pool_pg_advisor


def test_blocks_pg_tuning_when_cluster_is_degraded():
    result = build_pool_pg_advisor(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pool_rows=[{"name": "data", "pgs": 128, "size": 3}],
        pg_rows=[{
            "pool": "data",
            "state": "active+degraded",
            "acting": [0, 1, 2],
        }],
    )

    assert result["status"] == "observed"
    assert result["findings"][0]["code"] == "POOL_PG_HEALTH_BLOCKS_TUNING"
    assert result["findings"][0]["severity"] == "high"
    assert result["findings"][0]["action_id"] is None


def test_reports_pg_density_only_with_explicit_assumptions():
    result = build_pool_pg_advisor(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pool_rows=[{"name": "data", "pgs": 1024, "size": 3}],
        pg_rows=[
            {"pool": "data", "state": "active+clean", "acting": [0, 1, 2]},
            {"pool": "data", "state": "active+clean", "acting": [0, 1, 2]},
        ],
        target_pgs_per_osd=100,
    )

    finding = result["findings"][0]
    assert finding["code"] == "POOL_PG_DENSITY_OUTLIER"
    assert finding["evidence"]["recommended_pg_num"] == 100
    assert result["assumptions"]["movement_estimate"] == "unknown"
    assert "AUTOSCALER_MODE_UNAVAILABLE" in {
        gap["code"] for gap in result["evidence_gaps"]
    }


def test_fails_closed_for_missing_inventory_and_ec_simulation():
    result = build_pool_pg_advisor(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pool_rows=[{
            "name": "ec-data",
            "pgs": 64,
            "size": 4,
            "redundancy": "EC · ecprofile",
        }],
        pg_rows=[],
    )

    codes = {gap["code"] for gap in result["evidence_gaps"]}
    assert result["findings"] == []
    assert "PG_STATE_INVENTORY_UNAVAILABLE" in codes
    assert "EC_PROFILE_SIMULATION_UNAVAILABLE" in codes
    assert result["limits"]["mutation_commands_included"] is False


def test_advisor_never_returns_command_or_secret_material():
    result = build_pool_pg_advisor(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pool_rows=[{
            "name": "data",
            "pgs": 128,
            "size": 3,
            "secret_key": "MUST-NOT-LEAK",
        }],
        pg_rows=[],
    )

    assert "MUST-NOT-LEAK" not in repr(result)
    assert all("command" not in finding for finding in result["findings"])
