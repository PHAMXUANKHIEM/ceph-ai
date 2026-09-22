import json
import math
import random
from datetime import datetime, timedelta, timezone

from shared.forecast_features import MetricPoint, build_features
from shared.time_series_pipeline import normalize_series


UTC = timezone.utc
ORIGIN = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


def _points(values, *, start=ORIGIN, step=timedelta(hours=1)):
    return [
        MetricPoint(observed_at=start + index * step, value=value)
        for index, value in enumerate(values)
    ]


def test_lag_and_rolling_features_are_deterministic_and_timezone_normalized():
    points = _points(range(1, 31))
    local_time = (ORIGIN + timedelta(hours=29)).astimezone(timezone(timedelta(hours=7)))

    result = build_features(
        points,
        observed_at=local_time,
        expected_interval_seconds=3600,
        max_gap_seconds=5400,
        metric="cpu",
        horizon_hours=1,
    )

    assert result.observed_at == ORIGIN + timedelta(hours=29)
    assert result.features["current"] == 30.0
    assert result.features["lag_1"] == 29.0
    assert result.features["lag_3"] == 27.0
    assert result.features["rolling_mean_6"] == 27.5
    assert result.features["rolling_std_6"] == math.sqrt(35 / 12)
    assert result.quality_status == "OK"


def test_duplicate_timestamp_keeps_last_finite_observation_once():
    points = [
        MetricPoint(ORIGIN, 10),
        MetricPoint(ORIGIN + timedelta(hours=1), 20),
        MetricPoint(ORIGIN + timedelta(hours=1), 99),
        MetricPoint(ORIGIN + timedelta(hours=2), 30),
    ]

    result = build_features(points, metric="cpu", horizon_hours=1)

    assert result.sample_count == 3
    assert result.features["lag_1"] == 99.0
    assert result.features["current"] == 30.0


def test_gap_is_reported_without_forward_filling_values():
    points = [
        MetricPoint(ORIGIN, 10),
        MetricPoint(ORIGIN + timedelta(hours=1), 20),
        MetricPoint(ORIGIN + timedelta(hours=3), 40),
    ]

    result = build_features(
        points,
        expected_interval_seconds=3600,
        max_gap_seconds=3600,
        metric="cpu",
        horizon_hours=1,
    )

    assert result.max_gap_seconds == 7200
    assert result.quality_status == "GAP_DETECTED"
    assert result.sample_count == 3
    assert result.features["current"] == 40.0


def test_counter_reset_is_explicit_in_capacity_normalization():
    result = normalize_series(
        "used",
        [
            {"observed_at": ORIGIN, "value": 100},
            {"observed_at": ORIGIN + timedelta(minutes=5), "value": 120},
            {"observed_at": ORIGIN + timedelta(minutes=10), "value": 4},
        ],
        now=ORIGIN + timedelta(minutes=10),
    )

    assert result.reset_count == 1
    assert result.quality_status == "COUNTER_RESET"


def test_replay_never_reads_a_future_sample():
    past = _points([10, 20, 30])
    future = past + [MetricPoint(ORIGIN + timedelta(hours=3), 999999)]
    cutoff = ORIGIN + timedelta(hours=2)

    from_past = build_features(past, observed_at=cutoff, metric="cpu", horizon_hours=1)
    with_future = build_features(future, observed_at=cutoff, metric="cpu", horizon_hours=1)

    assert with_future == from_past
    assert with_future.features["current"] == 30.0
    assert all(value != 999999 for value in with_future.features.values())


def test_fuzz_nan_inf_reversed_timestamps_and_large_input_stay_bounded():
    rng = random.Random(20260921)
    for _ in range(100):
        values = [rng.choice([
            rng.uniform(-100, 100),
            float("nan"),
            float("inf"),
            float("-inf"),
        ]) for _ in range(40)]
        points = _points(values)
        rng.shuffle(points)
        result = build_features(points, metric="cpu", horizon_hours=1)
        assert result.sample_count <= 40
        assert result.observed_at.tzinfo is not None
        assert all(math.isfinite(value) for value in result.features.values())

    empty = build_features([], metric="cpu", horizon_hours=1)
    assert empty.quality_status == "INSUFFICIENT_SAMPLES"

    large = build_features(_points(range(5000)), metric="cpu", horizon_hours=1)
    state = {
        "features": large.features,
        "feature_schema": large.feature_schema,
        "sample_count": large.sample_count,
        "coverage_ratio": large.coverage_ratio,
        "max_gap_seconds": large.max_gap_seconds,
        "missing_features": list(large.missing_features),
        "quality_status": large.quality_status,
    }
    encoded = json.dumps(state, sort_keys=True).encode("utf-8")
    restored = json.loads(encoded)
    assert large.sample_count == 5000
    assert restored["features"] == large.features
    assert len(encoded) < 4096
