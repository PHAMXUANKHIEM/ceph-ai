from datetime import datetime, timedelta, timezone

from shared.online_learning import RiverMeanLearner, guarded_update
from shared.online_learning_gate import (
    OnlineLearningGateStatus,
    OnlineLearningInputTracker,
    OnlineLearningSample,
    evaluate_sample,
)


NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


def sample(seconds=0, *, sample_id=None, value=50.0, label=50.0):
    return OnlineLearningSample(
        value=value, label=label, sample_id=sample_id,
        observed_at=NOW + timedelta(seconds=seconds),
    )


def test_ready_sample_is_recorded_and_duplicate_is_blocked():
    tracker = OnlineLearningInputTracker()
    first = evaluate_sample(sample(sample_id="a"), tracker, now=NOW)
    duplicate = evaluate_sample(sample(sample_id="a"), tracker, now=NOW)
    assert first.status == OnlineLearningGateStatus.READY_TO_LEARN.value
    assert first.allowed is True
    assert duplicate.status == OnlineLearningGateStatus.DATA_QUALITY.value
    assert duplicate.allowed is False


def test_quality_gate_blocks_missing_stale_and_out_of_order_samples():
    tracker = OnlineLearningInputTracker()
    assert evaluate_sample(
        OnlineLearningSample(50.0, None, label=50.0), tracker, now=NOW,
    ).status == OnlineLearningGateStatus.DATA_QUALITY.value
    assert evaluate_sample(
        sample(-121, sample_id="stale"), tracker, now=NOW, max_age_seconds=120,
    ).status == OnlineLearningGateStatus.DATA_QUALITY.value
    assert evaluate_sample(sample(10, sample_id="new"), tracker, now=NOW).allowed
    out_of_order = evaluate_sample(sample(5, sample_id="old"), tracker, now=NOW)
    assert out_of_order.status == OnlineLearningGateStatus.DATA_QUALITY.value


def test_quality_gate_blocks_gap_without_moving_cursor():
    tracker = OnlineLearningInputTracker()
    assert evaluate_sample(sample(1, sample_id="a"), tracker, now=NOW).allowed
    gap = evaluate_sample(
        sample(100, sample_id="gap"), tracker, now=NOW,
        max_forward_gap_seconds=30,
    )
    assert gap.status == OnlineLearningGateStatus.DATA_QUALITY.value
    assert tracker.last_observed_at == NOW + timedelta(seconds=1)


def test_no_label_and_drift_are_distinct_blockers():
    tracker = OnlineLearningInputTracker()
    no_label = evaluate_sample(
        sample(sample_id="no-label", label=None), tracker, now=NOW,
    )
    assert no_label.status == OnlineLearningGateStatus.NO_LABEL.value
    drift = evaluate_sample(
        sample(1, sample_id="drift", value=90.0), tracker, now=NOW,
        drift_reference=50.0, drift_absolute_threshold=10.0,
    )
    assert drift.status == OnlineLearningGateStatus.DRIFT.value


def test_guarded_update_never_learns_when_quality_gate_blocks():
    learner = RiverMeanLearner()
    tracker = OnlineLearningInputTracker()
    blocked = evaluate_sample(
        sample(sample_id="blocked", label=None), tracker, now=NOW,
    )
    result = guarded_update(
        learner, 50.0, type("Decision", (), {
            "can_update_shadow": True, "can_update_active": True,
            "reason": "healthy",
        })(), quality_decision=blocked,
    )
    assert result.applied is False
    assert learner.sample_count == 0


def test_tracker_accepts_naive_database_timestamps():
    # Audit rows are loaded from the DB as naive UTC; samples are aware.
    from datetime import datetime, timedelta, timezone

    from shared.online_learning_gate import (
        OnlineLearningInputTracker,
        OnlineLearningSample,
        evaluate_sample,
    )

    now = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
    tracker = OnlineLearningInputTracker(last_observed_at=(now - timedelta(seconds=921)).replace(tzinfo=None))
    decision = evaluate_sample(
        OnlineLearningSample(value=12.5, observed_at=now, label=12.5, sample_id="s1"),
        tracker,
        now=now,
        max_age_seconds=120,
        max_forward_gap_seconds=1350,
    )
    assert decision.status == "READY_TO_LEARN"
    tracker.remember("s2", (now + timedelta(seconds=5)).replace(tzinfo=None))
    assert tracker.last_observed_at.tzinfo is not None


def test_effective_gap_limit_follows_scan_cadence_unless_configured():
    from types import SimpleNamespace

    from shared.online_learning_gate import effective_max_gap_seconds

    derived = SimpleNamespace(online_learning_sample_max_gap_seconds=None, node_health_scan_interval_seconds=900)
    assert effective_max_gap_seconds(derived) == 1350.0
    explicit = SimpleNamespace(online_learning_sample_max_gap_seconds=600, node_health_scan_interval_seconds=900)
    assert effective_max_gap_seconds(explicit) == 600.0


def test_normal_scan_jitter_is_accepted_but_a_missed_scan_is_a_gap():
    from datetime import datetime, timedelta, timezone

    from shared.online_learning_gate import (
        OnlineLearningInputTracker,
        OnlineLearningSample,
        evaluate_sample,
    )

    now = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)

    def decide(gap_seconds):
        tracker = OnlineLearningInputTracker(last_observed_at=now - timedelta(seconds=gap_seconds))
        return evaluate_sample(
            OnlineLearningSample(value=10.0, observed_at=now, label=10.0, sample_id=f"g{gap_seconds}"),
            tracker, now=now, max_age_seconds=120, max_forward_gap_seconds=1350,
        )

    assert decide(941).status == "READY_TO_LEARN"      # measured p90 jitter
    assert decide(1800).status == "DATA_QUALITY"       # one scan missed
