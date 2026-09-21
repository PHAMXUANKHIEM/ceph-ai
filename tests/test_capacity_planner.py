"""Tests for deterministic capacity planning."""

import pytest

from watcher.capacity_planner import plan_capacity


def _payload():
    return {
        "platform": "ceph",
        "vm_count": 20,
        "volume_count": 50,
        "provisioned_gib": 10240,
        "growth_percent_year": 25,
        "headroom_percent": 20,
        "iops": 30000,
        "throughput_mbps": 1000,
        "osd_capacity_gib": 4000,
        "osds_per_node": 4,
        "failure_domain_count": 3,
    }


def test_planner_is_reproducible_and_explains_formula():
    first = plan_capacity(_payload())
    second = plan_capacity(_payload())

    assert first == second
    assert first["read_only"] is True
    assert first["scenarios"][0]["osds_required"] >= first["scenarios"][0]["osds_for_capacity"]
    assert "raw = logical" in first["explainability"]["formula"]


def test_compares_replica_and_ec_without_inventing_cost():
    result = plan_capacity({**_payload(), "platform": "vitastor"})
    names = {scenario["name"] for scenario in result["scenarios"]}
    assert names == {"replica", "erasure_coding"}
    assert result["comparison"]["cost"] == "unknown_without_price_catalog"
    assert result["limits"]["costs_invented"] is False


def test_warns_when_failure_domain_is_singleton():
    result = plan_capacity({**_payload(), "failure_domain_count": 1})
    assert any(warning["code"] == "FAILURE_DOMAIN_SINGLETON"
               for warning in result["warnings"])


def test_rejects_invalid_input():
    with pytest.raises(ValueError):
        plan_capacity({"platform": "ceph", "provisioned_gib": 0})
    with pytest.raises(ValueError):
        plan_capacity({"platform": "unknown", "provisioned_gib": 1})
