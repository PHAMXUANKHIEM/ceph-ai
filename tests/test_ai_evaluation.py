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


def test_precision_calibration_hallucination_and_cost():
    from shared.ai_evaluation import expected_calibration_error

    golden = [
        {"id": "a", "should_act": True, "expected_action_id": "resync_ntp", "diagnosis_correct": True, "health_code": "MON_CLOCK_SKEW"},
        {"id": "b", "should_act": True, "expected_action_id": "restart_osd_daemon", "diagnosis_correct": False, "health_code": "OSD_DOWN"},
        {"id": "c", "should_act": False, "diagnosis_correct": True, "health_code": "OSD_DOWN"},
        {"id": "d", "should_act": None, "diagnosis_correct": None, "health_code": "OSD_DOWN"},
    ]
    predictions = [
        {"id": "a", "action_id": "resync_ntp", "confidence": 0.95, "cost_usd": 0.02},
        {"id": "b", "action_id": "invented_fix", "confidence": 0.85, "cost_usd": 0.03},
        {"id": "c", "abstain": True, "confidence": 0.9, "cost_usd": 0.01},
        {"id": "d", "action_id": "resync_ntp", "cost_usd": 0.04},
    ]
    report = evaluate(golden, predictions, known_action_ids={"resync_ntp", "restart_osd_daemon"})
    assert report.proposals == 3
    # Labeled proposals: a (correct) and b (wrong); d is unlabeled.
    assert report.action_precision == 0.5
    assert report.hallucination_rate == pytest.approx(1 / 3)
    assert report.correct_diagnoses == 2
    assert report.cost_usd_total == pytest.approx(0.10)
    assert report.cost_per_correct_diagnosis == pytest.approx(0.05)
    assert report.expected_calibration_error == pytest.approx(
        expected_calibration_error([(0.95, True), (0.85, False), (0.9, True)])
    )
    assert evaluate(golden, predictions).hallucination_rate is None


def test_expected_calibration_error_bins():
    from shared.ai_evaluation import expected_calibration_error

    assert expected_calibration_error([]) is None
    assert expected_calibration_error([(1.0, True), (0.0, False)]) == pytest.approx(0.0)
    # One bin, mean confidence 0.8, accuracy 0.5 -> 0.3.
    assert expected_calibration_error([(0.8, True), (0.8, False)]) == pytest.approx(0.3)


def test_evaluate_by_health_code_and_invalid_cost():
    from shared.ai_evaluation import evaluate_by

    golden = [
        {"id": "a", "should_act": True, "expected_action_id": "resync_ntp", "health_code": "MON_CLOCK_SKEW"},
        {"id": "b", "should_act": False, "health_code": "OSD_DOWN"},
    ]
    predictions = [{"id": "a", "action_id": "resync_ntp"}, {"id": "b", "action_id": "resync_ntp"}]
    groups = evaluate_by(golden, predictions, "health_code")
    assert groups["MON_CLOCK_SKEW"]["action_accuracy"] == 1.0
    assert groups["OSD_DOWN"]["unsafe_negative_rate"] == 1.0
    with pytest.raises(ValueError, match="negative cost_usd"):
        evaluate(golden, [{"id": "a", "action_id": "resync_ntp", "cost_usd": -1}])


def test_cli_catalogue_is_the_policy_action_list():
    import importlib.util
    from pathlib import Path

    module_path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_ai_diagnosis.py"
    spec = importlib.util.spec_from_file_location("evaluate_ai_diagnosis", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalogue = module.action_catalogue()
    assert {"resync_ntp", "restart_osd_daemon"} <= catalogue
    assert "invented_fix" not in catalogue
