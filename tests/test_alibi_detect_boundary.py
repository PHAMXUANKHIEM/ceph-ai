from shared.alibi_detect_boundary import detect_outlier


def test_detector_boundary_requires_scope_schema_and_baseline():
    assert detect_outlier([1], baseline=[1, 1], scope_key=None, feature_schema="scalar-v1").status == "DATA_QUALITY"
    assert detect_outlier([1], baseline=[1, 1], scope_key="c|h|m", feature_schema="other").status == "DATA_QUALITY"
    assert detect_outlier([1], baseline=[], scope_key="c|h|m", feature_schema="scalar-v1").status == "INSUFFICIENT_DATA"


def test_detector_returns_evidence_only_for_stable_or_drift():
    stable = detect_outlier([10.0, 11.0], baseline=[9.0, 10.0, 11.0], scope_key="c|h|cpu", feature_schema="scalar-v1")
    drift = detect_outlier([100.0], baseline=[9.0, 10.0, 11.0], scope_key="c|h|cpu", feature_schema="scalar-v1")
    assert stable.status == "STABLE"
    assert drift.status == "DRIFT"
    assert drift.detection_delay_samples == 1
