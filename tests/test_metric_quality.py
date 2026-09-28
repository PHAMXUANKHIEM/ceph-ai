from datetime import datetime, timedelta, timezone

import pytest

from shared.metric_quality import (
    MetricQualityStatus,
    assess_metric_quality,
    source_error,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)


def _points(count=5, interval=60, *, end=NOW - timedelta(seconds=30)):
    return [
        end - timedelta(seconds=interval * offset)
        for offset in reversed(range(count))
    ]


def _kwargs(**overrides):
    values = {
        "now": NOW,
        "max_age_seconds": 120,
        "minimum_samples": 4,
        "expected_interval_seconds": 60,
        "minimum_coverage_ratio": 0.9,
        "maximum_gap_seconds": 120,
        "minimum_history_seconds": 180,
    }
    values.update(overrides)
    return values


def test_valid_series_is_usable():
    result = assess_metric_quality(_points(), **_kwargs())

    assert result.status == MetricQualityStatus.OK.value
    assert result.usable is True
    assert result.sample_count == 5
    assert result.coverage_ratio == 1.0
    assert result.longest_gap_seconds == 60.0


def test_duplicate_timestamps_are_counted_once():
    points = _points() + [_points()[2]]

    result = assess_metric_quality(points, **_kwargs())

    assert result.status == MetricQualityStatus.OK.value
    assert result.sample_count == 5


def test_stale_stream_wins_over_historical_sample_count():
    result = assess_metric_quality(
        _points(end=NOW - timedelta(seconds=121)),
        **_kwargs(),
    )

    assert result.status == MetricQualityStatus.STALE.value
    assert result.usable is False
    assert "latest sample" in result.reason


def test_insufficient_samples_is_not_a_healthy_result():
    result = assess_metric_quality(
        _points(count=3),
        **_kwargs(minimum_samples=4),
    )

    assert result.status == MetricQualityStatus.INSUFFICIENT_SAMPLES.value


def test_history_can_be_insufficient_even_when_sample_count_is_high():
    result = assess_metric_quality(
        _points(count=5, interval=10),
        **_kwargs(minimum_history_seconds=180),
    )

    assert result.status == MetricQualityStatus.INSUFFICIENT_SAMPLES.value


def test_long_gap_is_reported_before_low_coverage():
    points = [
        NOW - timedelta(seconds=270),
        NOW - timedelta(seconds=210),
        NOW - timedelta(seconds=150),
        NOW - timedelta(seconds=30),
    ]

    result = assess_metric_quality(points, **_kwargs(maximum_gap_seconds=100))

    assert result.status == MetricQualityStatus.GAP_DETECTED.value
    assert result.longest_gap_seconds >= 120


def test_low_coverage_is_reported_when_gap_limit_allows_it():
    points = [
        NOW - timedelta(seconds=300),
        NOW - timedelta(seconds=240),
        NOW - timedelta(seconds=180),
        NOW - timedelta(seconds=30),
    ]

    result = assess_metric_quality(
        points,
        **_kwargs(
            minimum_samples=4,
            maximum_gap_seconds=1000,
            minimum_coverage_ratio=0.9,
        ),
    )

    assert result.status == MetricQualityStatus.LOW_COVERAGE.value
    assert result.coverage_ratio < 0.9


def test_source_error_is_never_usable():
    result = source_error("Loki request failed")

    assert result.status == MetricQualityStatus.SOURCE_ERROR.value
    assert result.usable is False
    assert result.reason == "Loki request failed"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_age_seconds": -1}, "max_age_seconds"),
        ({"minimum_samples": 0}, "minimum_samples"),
        ({"expected_interval_seconds": 0}, "expected_interval_seconds"),
        ({"minimum_coverage_ratio": 2}, "minimum_coverage_ratio"),
        ({"maximum_gap_seconds": -1}, "maximum_gap_seconds"),
        ({"minimum_history_seconds": -1}, "minimum_history_seconds"),
    ],
)
def test_contract_rejects_invalid_thresholds(kwargs, message):
    with pytest.raises(ValueError, match=message):
        assess_metric_quality(_points(), **_kwargs(**kwargs))
