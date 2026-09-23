from datetime import datetime, timedelta, timezone

from shared.anomaly_aggregation import AnomalyCandidate, aggregate_candidates, normalize_score


def test_score_normalization_requires_finite_baseline():
    assert normalize_score(3.0, [1.0, 2.0, 3.0]) == 1.0
    assert normalize_score(float("nan"), [1.0, 2.0]) == 0.0
    assert normalize_score(3.0, []) == 0.0


def test_candidates_are_grouped_and_top_features_are_explained():
    origin = datetime(2026, 9, 23, tzinfo=timezone.utc)
    candidates = [
        AnomalyCandidate("cluster|node|cpu|h1", origin, 0.8, {"cpu": 0.9, "ram": 0.2}),
        AnomalyCandidate("cluster|node|cpu|h1", origin + timedelta(seconds=60), 0.95, {"cpu": 0.4, "latency": 1.2}),
        AnomalyCandidate("cluster|node|cpu|h1", origin + timedelta(seconds=600), 0.7, {"ram": 0.8}),
        AnomalyCandidate("other|node|cpu|h1", origin, 0.9, {"cpu": 0.7}),
    ]
    incidents = aggregate_candidates(candidates, incident_gap_seconds=300)
    assert len(incidents) == 3
    assert incidents[0].candidate_count == 2
    assert incidents[0].top_features == ("latency", "cpu", "ram")
    assert incidents[0].event_type == "ANOMALY_CANDIDATE"
    assert all(item.execution_mode == "SHADOW_ONLY" for item in incidents)
