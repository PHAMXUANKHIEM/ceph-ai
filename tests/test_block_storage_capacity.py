from watcher.block_storage_capacity import build_capacity_risk


def _inventory():
    return [
        {"name": "volume-a", "provisioned_size": 700, "used_size": 300},
        {"name": "volume-b", "provisioned_size": 200, "used_size": 100},
    ]


def test_capacity_risk_separates_physical_logical_and_replica_overhead():
    result = build_capacity_risk(
        "vms", _inventory(),
        {"type": "replicated", "replica_size": 3, "bytes_used": 500, "max_available": 500},
    )

    assert result["physical"] == {
        "used_bytes": 500, "available_bytes": 500, "total_bytes": 1000, "used_percent": 50.0,
    }
    assert result["logical"]["provisioned_bytes"] == 900
    assert result["overhead"]["factor"] == 3.0
    assert result["logical"]["raw_equivalent_provisioned_bytes"] == 2700
    assert result["thin_provisioning"]["overcommitted"] is True
    assert result["status"] == "OVERCOMMITTED"
    assert result["read_only"] is True


def test_ec_overhead_fails_closed_without_k_m():
    result = build_capacity_risk(
        "ec", _inventory(),
        {"type": "erasure", "erasure_code_profile": "ec42", "bytes_used": 100, "max_available": 900},
    )

    assert result["overhead"]["known"] is False
    assert result["logical"]["raw_equivalent_provisioned_bytes"] is None
    assert any("k/m" in gap for gap in result["evidence"]["gaps"])


def test_failure_domain_reserve_is_reported_and_can_raise_severity():
    result = build_capacity_risk(
        "vms", _inventory(),
        {"type": "replicated", "replica_size": 3, "bytes_used": 500, "max_available": 500},
        failure_simulation={"status": "ready", "scenarios": [
            {"domain_name": "host-a", "remaining_capacity_bytes": 150},
            {"domain_name": "host-b", "remaining_capacity_bytes": 50},
        ]},
    )

    assert result["failure_domain_reserve"]["reserve_bytes"] == 50
    assert result["failure_domain_reserve"]["reserve_percent"] == 5.0
    assert result["failure_domain_reserve"]["worst_domain"] == "host-b"
    assert result["status"] == "HIGH"


def test_missing_forecast_and_stale_inventory_are_explicit_evidence_gaps():
    result = build_capacity_risk(
        "vms", [],
        {"type": "replicated", "replica_size": 3, "bytes_used": 100, "max_available": 900},
        inventory_state={"source": "stale-cache", "stale": True, "age_seconds": 901},
    )

    assert result["status"] == "HEALTHY"
    assert result["evidence"]["inventory_stale"] is True
    assert "inventory đang dùng snapshot cũ" in result["evidence"]["gaps"]
    assert "chưa đủ lịch sử capacity" in " ".join(result["evidence"]["gaps"])
