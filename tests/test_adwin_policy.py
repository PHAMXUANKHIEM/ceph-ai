from datetime import datetime, timedelta, timezone

from shared.adwin_policy import AdwinPolicy, DriftSample, compare_replay


def _samples(*, metric=50.0, error=0.0, count=90):
    origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return [
        DriftSample(
            origin + timedelta(hours=index),
            metric if index < 45 else metric,
            error if index < 45 else error,
            abs(error) if index < 45 else abs(error),
        )
        for index in range(count)
    ]


def test_residual_drift_reduces_confidence_and_marks_drift():
    policy = AdwinPolicy(min_samples=10, warmup_samples=4)
    origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
    decision = None
    for index in range(100):
        error = 0.0 if index < 40 else 30.0
        decision = policy.update(DriftSample(
            origin + timedelta(hours=index), 50.0, error, abs(error),
        ))
        if decision.residual_drift:
            break

    assert decision is not None
    assert decision.status == "DRIFT"
    assert decision.confidence_multiplier < 1.0
    assert decision.promotion_blocked is True
    assert "residual drift" in decision.reason


def test_mae_drift_blocks_promotion_even_when_metric_level_is_stable():
    policy = AdwinPolicy(min_samples=10, warmup_samples=0)
    origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
    decision = None
    for index in range(100):
        error = 0.0 if index < 40 else 30.0
        decision = policy.update(DriftSample(
            origin + timedelta(hours=index), 50.0, error, abs(error),
        ))
        if decision.mae_drift:
            break

    assert decision is not None
    assert decision.metric_drift is False
    assert decision.mae_drift is True
    assert decision.promotion_blocked is True
    assert "MAE drift" in decision.reason


def test_metric_drift_is_not_reported_as_model_error():
    policy = AdwinPolicy(min_samples=10, warmup_samples=0)
    origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
    decision = None
    for index in range(100):
        value = 50.0 if index < 40 else 100.0
        decision = policy.update(DriftSample(
            origin + timedelta(hours=index), value, 0.0, 0.0,
        ))
        if decision.metric_drift:
            break

    assert decision is not None
    assert decision.status == "METRIC_DRIFT"
    assert decision.residual_drift is False
    assert decision.mae_drift is False


def test_drift_has_warmup_and_recovery_hysteresis():
    policy = AdwinPolicy(min_samples=10, warmup_samples=3, clear_consecutive=3)
    origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
    drift = None
    for index in range(100):
        error = 0.0 if index < 40 else 30.0
        drift = policy.update(DriftSample(
            origin + timedelta(hours=index), 50.0, error, abs(error),
        ))
        if drift.residual_drift:
            break
    assert drift is not None and drift.warmup_remaining > 0

    # A single clean sample must not clear the drift state.
    clean = policy.update(DriftSample(origin + timedelta(hours=101), 50.0, 0.0, 0.0))
    assert clean.status == "DRIFT"
    assert clean.residual_drift is True

    for index in range(102, 104):
        clean = policy.update(DriftSample(origin + timedelta(hours=index), 50.0, 0.0, 0.0))
    assert clean.status in {"OK", "WARMUP"}
    assert clean.residual_drift is False


def test_state_round_trip_and_replay_comparison_are_bounded():
    policy = AdwinPolicy(min_samples=10)
    samples = _samples()
    for sample in samples:
        policy.update(sample)
    restored = AdwinPolicy.from_json(policy.to_json())
    assert restored.to_dict()["samples"] == policy.to_dict()["samples"]

    origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
    injected = [
        DriftSample(origin + timedelta(hours=index), 50.0 if index < 45 else 100.0, 0.0, 0.0)
        for index in range(90)
    ]
    report = compare_replay(injected * 10)
    assert report["sample_count"] == 512
    assert report["adwin_first_detection"] is not None
    assert isinstance(report["adwin_detection_indexes"], list)
    assert isinstance(report["legacy_detection_indexes"], list)
