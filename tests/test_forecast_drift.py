from shared.forecast_drift import (
    DRIFT,
    INSUFFICIENT_DATA,
    STABLE,
    RiverAdwinDetector,
    evaluate_drift,
)


def test_adwin_snapshot_is_bounded_and_quality_fail_closed():
    detector = RiverAdwinDetector(scope_key="cluster|node|cpu")
    skipped = detector.update(1.0, quality_status="GAP_DETECTED")
    assert skipped.sample_count == 0
    for index in range(20):
        detector.update(float(index))
    restored = RiverAdwinDetector.from_snapshot(detector.snapshot())
    assert restored.sample_count == detector.sample_count
    assert restored.report().detector == "river_adwin"


def test_drift_detector_fails_closed_when_windows_are_too_small():
    report = evaluate_drift([10, 11], [50, 51], minimum_samples=3)
    assert report.status == INSUFFICIENT_DATA
    assert report.confidence_multiplier == 1.0


def test_drift_detector_reports_stable_windows():
    report = evaluate_drift(
        [50] * 10, [51] * 10,
        baseline_residuals=[1] * 10, recent_residuals=[2] * 10,
        baseline_coverages=[1.0] * 10, recent_coverages=[0.95] * 10,
        baseline_alerts=[False] * 10, recent_alerts=[False] * 10,
        minimum_samples=10,
    )
    assert report.status == STABLE
    assert report.score == 0.0


def test_drift_detector_combines_baseline_residual_coverage_and_alert_rate():
    report = evaluate_drift(
        [20] * 10, [50] * 10,
        baseline_residuals=[0] * 10, recent_residuals=[30] * 10,
        baseline_coverages=[1.0] * 10, recent_coverages=[0.5] * 10,
        baseline_alerts=[False] * 10, recent_alerts=[True] * 10,
        minimum_samples=10,
        baseline_shift_threshold=10,
        residual_shift_threshold=10,
        coverage_drop_threshold=0.2,
        alert_rate_increase_threshold=0.2,
    )
    assert report.status == DRIFT
    assert report.score == 4.0
    assert report.confidence_multiplier == 0.5
    assert "baseline shift" in report.reason
