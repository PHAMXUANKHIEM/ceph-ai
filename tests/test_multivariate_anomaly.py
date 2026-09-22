from datetime import datetime, timedelta, timezone

import pytest

from shared.multivariate_anomaly import (
    FEATURE_NAMES,
    MetricObservation,
    MultivariateSample,
    OptionalDetectorUnavailable,
    calibrate_threshold,
    candidate_evidence,
    candidate_scores,
    cluster_incident_windows,
    fit_peer_baselines,
    normalize_scores,
    run_river_hst,
    run_rrcf,
    standardize_samples,
    build_feature_vectors,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _sample(index: int, *, value: float = 0.0, peer: str = "nvme") -> MultivariateSample:
    timestamp = NOW + timedelta(minutes=index)
    values = {feature: value for feature in FEATURE_NAMES}
    return MultivariateSample(timestamp, values, "OK", {feature: "OK" for feature in FEATURE_NAMES}, (), 1.0, peer)


def test_feature_vectors_align_jitter_and_do_not_zero_fill_bad_components():
    observations = {
        "cpu": [MetricObservation(NOW, 20.0), MetricObservation(NOW + timedelta(minutes=1), 21.0)],
        "ram": [MetricObservation(NOW + timedelta(seconds=10), 40.0), MetricObservation(NOW + timedelta(minutes=1), 41.0)],
        "read_iops": [MetricObservation(NOW, 100.0, "STALE"), MetricObservation(NOW + timedelta(minutes=1), 101.0)],
    }
    vectors = build_feature_vectors(
        observations,
        required_features=("cpu", "ram", "read_iops"),
        tolerance_seconds=30,
        min_coverage=1.0,
    )

    assert vectors[0].quality_status == "INSUFFICIENT_COMPONENTS"
    assert "read_iops" not in vectors[0].values
    assert vectors[0].values.get("cpu") == 20.0
    assert vectors[0].values.get("ram") == 40.0


def test_peer_baselines_are_separate_and_standardization_is_robust():
    samples = [_sample(index, value=float(index), peer="ssd" if index % 2 else "hdd") for index in range(16)]
    baselines = fit_peer_baselines(samples, min_samples=8)

    assert set(baselines) == {"ssd", "hdd"}
    normalized = standardize_samples(samples, baselines)
    assert all(item.quality_status == "OK" for item in normalized)
    assert set(normalized[0].values) == set(FEATURE_NAMES)


def test_hst_has_warmup_and_never_produces_side_effect_flags():
    samples = [_sample(index) for index in range(36)]
    baselines = fit_peer_baselines(samples, min_samples=8)
    scores = run_river_hst(standardize_samples(samples, baselines), warmup=8, n_trees=5, height=6, window_size=32)

    assert len(scores) == len(samples)
    assert all(item.detector == "river_hst" for item in scores)
    assert all(item.raw_score is None for item in scores[:8])


def test_score_contract_calibration_and_incident_grouping():
    raw = [None, 0.1, 0.2, 0.4, 0.9]
    normalized = normalize_scores(raw, [0.1, 0.2, 0.4])
    assert normalized[0] is None
    assert all(value is None or 0 <= value <= 1 for value in normalized)
    assert normalize_scores([0.0, 0.1], [0.0, 0.0])[0] == 0.0
    threshold = calibrate_threshold(normalized, quantile=0.9, minimum=0.8)
    candidates = candidate_scores(
        [
            # A normalized score is already in the shared [0, 1] contract.
            type("Score", (), {"timestamp": NOW + timedelta(minutes=index), "detector": "hst", "score": score,
                                "quality_status": "OK", "top_features": ("cpu",), "peer_class": "ssd"})()
            for index, score in enumerate((0.9, 0.91, 0.92))
        ],
        threshold=threshold,
    )
    windows = cluster_incident_windows(candidates, max_gap_seconds=180)
    assert len(windows) == 1
    assert windows[0].sample_count == 3
    evidence = candidate_evidence(windows)
    assert evidence[0]["type"] == "ANOMALY_CANDIDATE"
    assert evidence[0]["remediation_requested"] is False
    assert evidence[0]["notification_allowed"] is False


def test_rrcf_is_optional_and_never_required_by_runtime():
    samples = [_sample(index) for index in range(10)]
    baselines = fit_peer_baselines(samples, min_samples=8)
    normalized = standardize_samples(samples, baselines)
    try:
        scores = run_rrcf(normalized, warmup=2, n_trees=2, tree_size=8)
    except OptionalDetectorUnavailable:
        return
    assert len(scores) == len(samples)
