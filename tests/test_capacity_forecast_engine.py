from datetime import datetime, timedelta

from shared.capacity_forecast_engine import (
    CapacityObservation,
    aggregate_volume_observations,
    build_breakdown,
    build_chart,
    detect_counter_reset,
)


def _observation(**overrides):
    values = {
        "timestamp": datetime(2026, 1, 1),
        "entity_type": "pool",
        "entity_name": "images",
        "physical_used_bytes": 1700,
        "physical_total_bytes": 2000,
        "provisioned_bytes": 1000,
        "logical_used_bytes": 400,
        "snapshot_provisioned_bytes": 1000,
        "snapshot_bytes": 100,
        "snapshot_count": 2,
        "replica_factor": 3,
        "ec_k": None,
        "ec_m": None,
        "failure_domain_reserve_percent": 10.0,
    }
    values.update(overrides)
    return CapacityObservation(**values)


def test_breakdown_separates_physical_logical_redundancy_and_reserve():
    breakdown = build_breakdown(_observation())

    assert breakdown.physical_used_bytes == 1700
    assert breakdown.usable_physical_bytes == 1800
    assert breakdown.failure_domain_reserve_bytes == 200
    assert breakdown.thin_unallocated_bytes == 1500
    assert breakdown.redundancy_overhead_bytes == 200
    assert breakdown.provisioned_percent_of_usable == round(1000 * 100 / 1800, 4)


def test_volume_aggregation_preserves_snapshot_and_thin_attribution():
    observation = aggregate_volume_observations(
        datetime(2026, 1, 1),
        "pool",
        "images",
        [
            {
                "name": "vm-a",
                "provisioned_size": 1000,
                "used_size": 400,
                "snapshot_count": 2,
                "snapshot_provisioned_size": 1000,
                "snapshot_used_size": 100,
            }
        ],
        physical_used_bytes=600,
        physical_total_bytes=1000,
        replica_factor=3,
    )

    assert observation.provisioned_bytes == 1000
    assert observation.logical_used_bytes == 400
    assert observation.snapshot_provisioned_bytes == 1000
    assert observation.snapshot_bytes == 100
    assert observation.snapshot_count == 2
    assert observation.quality_status == "OK"
    assert build_breakdown(observation).thin_unallocated_bytes == 1500


def test_missing_snapshot_sample_is_marked_partial_and_empty_pool_is_missing():
    partial = aggregate_volume_observations(
        datetime(2026, 1, 1), "pool", "images", [{"snapshot_count": 1}],
        physical_used_bytes=10, physical_total_bytes=100,
    )
    missing = aggregate_volume_observations(
        datetime(2026, 1, 1), "pool", "new-pool", [],
        physical_used_bytes=10, physical_total_bytes=100,
    )

    assert partial.quality_status == "PARTIAL_SNAPSHOT_USAGE"
    assert missing.quality_status == "MISSING_VOLUME_OBSERVATION"


def test_counter_reset_is_only_for_monotonic_counters():
    assert detect_counter_reset(100, 10) is True
    assert detect_counter_reset(10, 100) is False
    assert detect_counter_reset(None, 10) is False


def test_chart_contains_actual_forecast_and_confidence_band():
    observations = [
        _observation(
            timestamp=datetime(2026, 1, 1) + timedelta(days=index),
            physical_used_bytes=500 + index * 50,
            physical_total_bytes=1000,
        )
        for index in range(3)
    ]

    chart = build_chart(observations, horizon_days=30)

    assert chart[-1].forecast_percent is not None
    assert chart[-1].confidence_low is not None
    assert chart[-1].confidence_high is not None
    assert all(point.quality_status for point in chart)
