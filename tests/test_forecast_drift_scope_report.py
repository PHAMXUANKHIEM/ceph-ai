from scripts.forecast_drift_scope_report import classify_drift
from config.settings import settings


def test_classifies_baseline_shift_only_when_quality_is_adequate():
    classification, reason = classify_drift(
        drift_reason="baseline shift 31.10; residual shift 19.17",
        coverage_ratio=0.99,
        max_gap_hours=1.5,
    )
    assert classification == "WORKLOAD_OR_DISTRIBUTION_CHANGE_CANDIDATE"
    assert "operator review" in reason


def test_does_not_hide_collector_gap_as_workload_change():
    classification, _reason = classify_drift(
        drift_reason="baseline shift 31.10",
        coverage_ratio=float(settings.node_resource_forecast_min_coverage) / 2,
        max_gap_hours=1.5,
    )
    assert classification == "DATA_QUALITY_OR_COLLECTOR_GAP"


def test_unknown_drift_reason_remains_unclassified():
    classification, _reason = classify_drift(
        drift_reason="unknown detector transition",
        coverage_ratio=0.99,
        max_gap_hours=1.5,
    )
    assert classification == "UNCLASSIFIED_DRIFT"
