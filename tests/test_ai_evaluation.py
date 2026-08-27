import pytest
from shared.ai_evaluation import evaluate

def test_evaluate_uses_independent_action_and_diagnosis_labels():
    golden = [
        {"id": "a", "should_act": True, "expected_action_id": "resync_ntp", "diagnosis_correct": True},
        {"id": "b", "should_act": True, "expected_action_id": "restart_osd_daemon", "diagnosis_correct": False},
        {"id": "c", "should_act": False, "expected_action_id": None, "diagnosis_correct": False},
        {"id": "d", "should_act": False, "expected_action_id": None, "diagnosis_correct": None},
        {"id": "unlabeled", "should_act": None, "diagnosis_correct": None},
    ]
    predictions = [
        {"id": "a", "action_id": "resync_ntp", "confidence": 0.9},
        {"id": "b", "action_id": "resync_ntp", "confidence": 0.8},
        {"id": "c", "abstain": True, "confidence": 0.2},
        {"id": "d", "action_id": "resync_ntp", "confidence": 0.6},
        {"id": "unlabeled", "action_id": "x", "confidence": 0.5},
    ]
    report = evaluate(golden, predictions)
    assert report.total == report.matched == 5
    assert report.actionable_labels == 2 and report.action_accuracy == 0.5
    assert report.abstention_labels == 2 and report.abstention_recall == 0.5
    assert report.unsafe_negative_rate == 0.5 and report.unsafe_overall_rate == 0.2
    assert report.diagnosis_labels == 3
    assert report.diagnosis_brier_score == pytest.approx((0.01 + 0.64 + 0.04) / 3)

def test_missing_prediction_is_coverage_not_abstention():
    report = evaluate([{"id": "a", "should_act": False}], [])
    assert report.matched == 0 and report.abstention_recall is None

@pytest.mark.parametrize("rows,match", [
    ([{"id": "a"}, {"id": "a"}], "duplicate golden"),
    ([{"id": "", "should_act": False}], "non-empty string id"),
    ([{"id": "a", "should_act": "false"}], "should_act"),
    ([{"id": "a", "should_act": True}], "expected_action_id"),
    ([{"id": "a", "should_act": False, "expected_action_id": "x"}], "cannot have"),
])
def test_rejects_invalid_golden_schema(rows, match):
    with pytest.raises(ValueError, match=match): evaluate(rows, [])

@pytest.mark.parametrize("predictions,match", [
    ([{"id": "a"}, {"id": "a"}], "duplicate prediction"),
    ([{"id": "a", "abstain": "false"}], "abstain"),
    ([{"id": "a", "abstain": True, "action_id": "x"}], "both abstain"),
    ([{"id": "a", "confidence": True}], "invalid confidence"),
    ([{"id": "a", "confidence": 1.1}], "outside"),
])
def test_rejects_invalid_prediction_schema(predictions, match):
    with pytest.raises(ValueError, match=match):
        evaluate([{"id": "a", "should_act": None}], predictions)
