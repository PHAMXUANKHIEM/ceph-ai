import math
from datetime import datetime, timedelta, timezone

import pytest

from shared.river_snarimax import (
    ALGORITHM,
    MAX_SNAPSHOT_BYTES,
    MODEL_VERSION,
    SnarimaxShadowModel,
    SnarimaxShadowRunner,
    fallback_result,
    profile_for,
)


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def seasonal_points(count=72):
    return [
        (
            ORIGIN + timedelta(hours=index),
            50.0 + 8.0 * math.sin(2 * math.pi * (index % 24) / 24) + index * 0.02,
        )
        for index in range(count)
    ]


def test_profiles_are_fixed_and_scope_cpu_ram_iops():
    assert profile_for("cpu").m == 24
    assert profile_for("ram").p == 1
    assert profile_for("disk_iops").metric == "iops"
    with pytest.raises(ValueError):
        profile_for("latency")


def test_snarimax_shadow_generates_bounded_interval_and_json_snapshot():
    model = SnarimaxShadowModel(profile_for("cpu"))
    result = model.fit(seasonal_points(), timeout_seconds=3)

    assert result.status == "SHADOW_ONLY"
    assert result.prediction is not None
    assert result.predicted_low is not None
    assert result.predicted_high is not None
    assert result.predicted_low <= result.prediction <= result.predicted_high
    assert result.interval_sample_count >= 8
    assert result.state_bytes <= MAX_SNAPSHOT_BYTES
    assert result.snapshot["algorithm"] == ALGORITHM
    assert result.snapshot["version"] == MODEL_VERSION


def test_restart_restores_state_and_continues_from_last_timestamp():
    points = seasonal_points()
    first = SnarimaxShadowModel(profile_for("ram"))
    first_result = first.fit(points, timeout_seconds=3)
    restored = SnarimaxShadowModel.from_snapshot(first_result.snapshot)

    continued = restored.fit(
        points + [(ORIGIN + timedelta(hours=72), 51.0)],
        timeout_seconds=3,
        resume=True,
    )

    assert continued.status == "SHADOW_ONLY"
    assert continued.sample_count == 73
    assert continued.observed_at == ORIGIN + timedelta(hours=72)


def test_corrupt_snapshot_falls_back_without_partial_restore():
    model = SnarimaxShadowModel(profile_for("cpu"))
    result = model.fit(seasonal_points(), timeout_seconds=3)
    corrupt = dict(result.snapshot)
    corrupt["sample_count"] = 999999

    with pytest.raises(ValueError, match="checksum"):
        SnarimaxShadowModel.from_snapshot(corrupt)

    fallback = SnarimaxShadowRunner(failure_threshold=2).run(
        "cluster|host|cpu", "cpu", seasonal_points(),
        fallback=42.0, timeout_seconds=3, snapshot=corrupt,
    )
    assert fallback.status == "FALLBACK"
    assert fallback.prediction == 42.0
    assert "SNARIMAX_ERROR" in fallback.reason


def test_timeout_opens_circuit_and_uses_active_baseline():
    runner = SnarimaxShadowRunner(failure_threshold=2, cooldown_seconds=300)
    first = runner.run("cluster|host|cpu", "cpu", seasonal_points(), fallback=40.0, timeout_seconds=0.001)
    second = runner.run("cluster|host|cpu", "cpu", seasonal_points(), fallback=40.0, timeout_seconds=0.001)
    third = runner.run("cluster|host|cpu", "cpu", seasonal_points(), fallback=40.0, timeout_seconds=3)

    assert first.status == "FALLBACK"
    assert first.prediction == 40.0
    assert second.status == "FALLBACK"
    assert third.reason == "CIRCUIT_OPEN"


def test_insufficient_history_is_not_a_model_failure():
    model = SnarimaxShadowModel(profile_for("iops"))
    result = model.fit(seasonal_points(12), timeout_seconds=3)

    assert result.status == "NOT_ELIGIBLE"
    assert result.reason == "INSUFFICIENT_SEASONAL_HISTORY"


def test_fallback_result_is_explicitly_shadow_safe():
    result = fallback_result("cpu", 55.0, "TIMEOUT")
    assert result.status == "FALLBACK"
    assert result.prediction == 55.0
    assert result.as_dict()["execution_mode"] == "SHADOW_ONLY"
