from datetime import datetime, timedelta

from watcher.performance_simulation import simulate_scenario


NOW = datetime(2026, 9, 19, 12, 0, 0)


def _payload(scenario, values, *, captured_at=NOW - timedelta(minutes=1)):
    return {
        "scenario": scenario,
        "target": {"pool": "rbd", "image": "vm-a"},
        "values": values,
        "evidence": {"source_ids": ["volume_metrics", "volume_osd_mappings"], "captured_at": captured_at.isoformat() + "Z"},
    }


def test_resize_simulation_is_read_only_and_reports_capacity_headroom():
    result = simulate_scenario(_payload("resize", {
        "current_size_gib": 100, "proposed_size_gib": 200, "used_gib": 60,
    }), now=NOW)

    assert result["status"] == "ready"
    assert result["read_only"] is True
    assert result["action_id"] is None
    assert result["expected_benefit"]["capacity_headroom_gib"] == 140
    assert result["rebalance"]["status"] == "not_expected_immediately"


def test_replica_simulation_exposes_failure_domain_risk_and_move_estimate():
    result = simulate_scenario(_payload("placement", {
        "current_replica": 2, "proposed_replica": 3, "used_bytes": 1000,
        "failure_domain_count": 4, "proposed_failure_domain_count": 3,
    }), now=NOW)

    assert result["status"] == "ready"
    assert result["failure_domain_risk"]["status"] == "elevated"
    assert result["rebalance"]["estimated_bytes_moved"] == 500
    assert "Review failure domain" in result["recommendation"]


def test_replica_ec_simulation_uses_coding_width_and_recovery_throughput():
    result = simulate_scenario(_payload("replica_ec", {
        "current_replica": 3, "proposed_replica": 3,
        "current_ec_k": 4, "current_ec_m": 2,
        "proposed_ec_k": 6, "proposed_ec_m": 2,
        "used_bytes": 8 * 1024 * 1024,
        "failure_domain_count": 4, "proposed_failure_domain_count": 4,
        "recovery_throughput_mib_s": 2,
    }), now=NOW)

    assert result["status"] == "ready"
    assert result["expected_benefit"]["coding_width_delta"] == 2
    assert result["rebalance"]["estimated_bytes_moved"] == 2796203
    assert result["duration"]["status"] == "estimated"
    assert result["duration"]["minutes"] == 0.02


def test_pg_simulation_does_not_claim_direct_pg_evidence():
    result = simulate_scenario(_payload("pg_change", {
        "current_pg": 128, "proposed_pg": 256, "pool_bytes": 1000000, "osd_count": 6,
    }), now=NOW)

    assert result["status"] == "ready"
    assert result["failure_domain_risk"]["status"] == "unknown"
    assert result["rebalance"]["estimated_bytes_moved"] == 1000000
    assert result["duration"]["status"] == "unknown"
    assert result["read_only"] is True


def test_simulation_fails_closed_for_stale_or_incomplete_evidence():
    stale = simulate_scenario(_payload("qos", {
        "current_iops": 100, "proposed_iops": 200, "observed_iops": 300,
    }, captured_at=NOW - timedelta(hours=1)), now=NOW)
    incomplete = simulate_scenario(_payload("flatten", {"snapshot_count": 2}), now=NOW)

    assert stale["status"] == "insufficient_evidence"
    assert "15-minute" in " ".join(stale["evidence_gaps"])
    assert incomplete["status"] == "insufficient_evidence"
    assert "missing numeric scenario fields" in " ".join(incomplete["evidence_gaps"])


def test_simulation_endpoint_is_cluster_scoped_and_does_not_create_action(dashboard_client, default_cluster_id):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.post(
        f"/api/performance-rca/simulate?cluster={default_cluster_id}",
        json=_payload("qos", {"current_iops": 100, "proposed_iops": 200, "observed_iops": 300}, captured_at=datetime.utcnow() - timedelta(minutes=1)),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cluster_id"] == default_cluster_id
    assert body["cluster_scoped"] is True
    assert body["recommendation_mode"] == "SIMULATION_ONLY"
    assert body["action_id"] is None
