from shared.forecast_drift_policy import apply_drift_policy


def test_residual_drift_reduces_confidence_and_blocks_reset_and_promotion():
    decision = apply_drift_policy(
        residual_status="DRIFT", mae_status="STABLE", metric_status="STABLE",
        consecutive_good_outcomes=99,
    )
    assert decision.candidate_status == "DRIFT"
    assert decision.confidence_multiplier == 0.5
    assert decision.promotion_allowed is False
    assert decision.model_reset_allowed is False
    assert decision.requires_operator_review is True


def test_mae_drift_extends_evaluation_and_metric_drift_is_workload_evidence():
    mae = apply_drift_policy(
        residual_status="STABLE", mae_status="DRIFT", metric_status="STABLE",
        consecutive_good_outcomes=4,
    )
    workload = apply_drift_policy(
        residual_status="STABLE", mae_status="STABLE", metric_status="DRIFT",
        consecutive_good_outcomes=4,
    )
    assert mae.evaluation_window_multiplier == 2.0
    assert mae.promotion_allowed is False
    assert workload.workload_changed is True
    assert workload.candidate_status == "SHADOW"
    assert workload.promotion_allowed is False


def test_candidate_becomes_eligible_only_after_required_good_outcomes():
    blocked = apply_drift_policy(
        residual_status="STABLE", mae_status="STABLE", metric_status="STABLE",
        consecutive_good_outcomes=2, required_good_outcomes=3,
    )
    eligible = apply_drift_policy(
        residual_status="STABLE", mae_status="STABLE", metric_status="STABLE",
        consecutive_good_outcomes=3, required_good_outcomes=3,
    )
    assert blocked.candidate_status == "SHADOW"
    assert blocked.promotion_allowed is False
    assert eligible.candidate_status == "ELIGIBLE"
    assert eligible.promotion_allowed is True
