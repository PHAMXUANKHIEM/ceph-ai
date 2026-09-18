from datetime import datetime, timedelta

import pytest

from shared.metric_quality import MetricQualityStatus, assess_metric_quality


NOW = datetime(2026, 9, 18, 3, 15)


def _quality(**overrides):
    values = {
        "now": NOW,
        "max_age_seconds": 1800,
        "minimum_samples": 4,
        "expected_interval_seconds": 3600,
        "minimum_coverage_ratio": 0.6,
        "maximum_gap_seconds": 21600,
        "minimum_history_seconds": 10800,
    }
    values.update(overrides)
    return values


def test_quality_accepts_hourly_series_with_recent_sample():
    points = [NOW - timedelta(hours=offset) for offset in range(4, -1, -1)]
    result = assess_metric_quality(points, **_quality())

    assert result.status == MetricQualityStatus.OK.value
    assert result.coverage_ratio == 1.0
    assert result.usable


def test_quality_rejects_stale_series():
    points = [NOW - timedelta(hours=4), NOW - timedelta(hours=1)]
    result = assess_metric_quality(points, **_quality(minimum_samples=2))

    assert result.status == MetricQualityStatus.STALE.value
    assert not result.usable


def test_quality_rejects_large_gap():
    points = [
        NOW - timedelta(hours=5),
        NOW - timedelta(hours=4),
        NOW - timedelta(hours=1),
    ]
    result = assess_metric_quality(
        points,
        **_quality(
            minimum_samples=3,
            max_age_seconds=999999,
            maximum_gap_seconds=7200,
        ),
    )

    assert result.status == MetricQualityStatus.GAP_DETECTED.value


def test_quality_rejects_low_coverage():
    points = [
        NOW - timedelta(hours=8),
        NOW - timedelta(hours=7),
        NOW - timedelta(hours=1),
    ]
    result = assess_metric_quality(
        points,
        **_quality(
            minimum_samples=3,
            max_age_seconds=999999,
            maximum_gap_seconds=999999,
            minimum_history_seconds=3600,
            minimum_coverage_ratio=0.8,
        ),
    )

    assert result.status == MetricQualityStatus.LOW_COVERAGE.value
    assert result.coverage_ratio < 0.8


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_age_seconds": -1},
        {"minimum_samples": 0},
        {"expected_interval_seconds": 0},
        {"minimum_coverage_ratio": 2},
        {"maximum_gap_seconds": -1},
    ],
)
def test_quality_rejects_invalid_contract(kwargs):
    with pytest.raises(ValueError):
        assess_metric_quality([NOW], **_quality(**kwargs))
