from datetime import datetime, timezone

import pytest

from shared.time_series_pipeline import normalize_capacity_snapshot, normalize_series


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def test_normalizer_orders_deduplicates_and_marks_gaps_and_counter_resets():
    series = normalize_series(
        "used",
        [
            {"observed_at": "2026-09-21T11:10:00Z", "value": 20},
            {"observed_at": "2026-09-21T11:00:00Z", "value": 10},
            {"observed_at": "2026-09-21T11:00:00Z", "value": 11},
            {"observed_at": "2026-09-21T11:30:00Z", "value": 4},
        ],
        now=NOW,
    )
    assert [point.value for point in series.points] == [11, 20, 4]
    assert series.reset_count == 1
    assert series.gap_count == 1
    assert series.quality_status == "GAP_DETECTED"


def test_invalid_old_future_negative_and_non_numeric_samples_are_dropped():
    series = normalize_series(
        "raw",
        [
            {"observed_at": "2026-01-01T00:00:00Z", "value": 1},
            {"observed_at": "2026-09-21T12:10:00Z", "value": 2},
            {"observed_at": "2026-09-21T11:59:00Z", "value": -1},
            {"observed_at": "bad", "value": 2},
        ],
        now=NOW,
    )
    assert series.points == ()
    assert series.dropped_count == 4
    assert series.quality_status == "EMPTY"


def test_capacity_snapshot_keeps_only_declared_metrics_and_does_not_invent_missing_data():
    output = normalize_capacity_snapshot(
        [
            {"observed_at": "2026-09-21T11:55:00Z", "metrics": {"raw": 100, "used": 40, "ec_overhead": 12, "unknown": 999}},
            {"observed_at": "2026-09-21T12:00:00Z", "metrics": {"raw": 120, "used": 50, "thin_provisioned": 200}},
        ],
        now=NOW,
    )
    assert set(output) == {"raw", "used", "ec_overhead", "thin_provisioned"}
    assert [point.value for point in output["raw"].points] == [100, 120]
    assert output["thin_provisioned"].points[-1].value == 200
    assert "available" not in output


def test_unknown_series_is_rejected():
    with pytest.raises(ValueError):
        normalize_series("not_capacity", [], now=NOW)
