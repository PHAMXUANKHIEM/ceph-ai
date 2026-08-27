import pytest

from shared.ai_evaluation import evaluate


def test_evaluate_scores_actions_abstention_safety_and_calibration():
    golden = [
        {"id": "a", "should_act": True, "expected_action_id": "resync_ntp"},
        {"id": "b", "should_act": True, "expected_action_id": "restart_osd_daemon"},
        {"id": "c", "should_act": False, "expected_action_id": None},
        {"id": "d", "should_act": False, "expected_action_id": None},
    ]
    predictions = [
        {"id": "a", "action_id": "resync_ntp", "confidence": 0.9},
        {"id": "b", "action_id": "resync_ntp", "confidence": 0.8},
        {"id": "c", "abstain": True, "confidence": 0.7},
        {"id": "d", "action_id": "resync_ntp", "confidence": 0.6},
    ]
    report = evaluate(golden, predictions)
    assert report.total == report.matched == 4
    assert report.action_accuracy == 0.5
    assert report.abstention_recall == 0.5
    assert report.unsafe_action_rate == 0.25
    assert report.brier_score == pytest.approx((0.01 + 0.64 + 0.09 + 0.36) / 4)


def test_evaluate_does_not_treat_missing_prediction_as_abstention():
    report = evaluate([{"id": "a", "should_act": False}], [])
    assert report.total == 1 and report.matched == 0
    assert report.abstention_recall is None


@pytest.mark.parametrize("confidence", [-0.1, 1.1, "bad"])
def test_evaluate_rejects_invalid_confidence(confidence):
    with pytest.raises(ValueError):
        evaluate([{"id": "a", "should_act": True, "expected_action_id": "x"}], [{"id": "a", "action_id": "x", "confidence": confidence}])
