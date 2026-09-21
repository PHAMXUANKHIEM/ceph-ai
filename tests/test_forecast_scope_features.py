from datetime import datetime, timedelta, timezone

import pytest

from shared.forecast_anomaly import candidate_d_alerts, candidate_d_isolation_scores
from shared.forecast_features import MetricPoint, build_features
from shared.forecast_scope import ForecastScope, parse_legacy_scope, validate_scope_dimensions


def test_scope_parser_keeps_node_and_volume_dimensions_separate():
    node = parse_legacy_scope("NODE_RESOURCE", "cluster-a|node-a|cpu", horizon_hours=1)
    volume = parse_legacy_scope("VOLUME", "cluster-a|pool-a|image-a|used_percent", horizon_hours=24)
    assert node and node.entity_type == "node" and node.host == "node-a"
    assert volume and volume.entity_id == "pool-a/image-a" and volume.host is None
    assert node.canonical_key != volume.canonical_key


def test_scope_rejects_unknown_or_legacy_schema_for_promotion():
    scope = ForecastScope("cluster-a", "node", "node-a", "cpu", 1, host="node-a")
    assert validate_scope_dimensions(scope=scope, scope_schema="forecast-scope-v2") == (True, None)
    assert validate_scope_dimensions(scope=scope, scope_schema="legacy-v1")[0] is False
    assert parse_legacy_scope("NODE_RESOURCE", "broken", horizon_hours=1) is None


def test_features_are_leakage_safe_and_report_gaps():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    points = [MetricPoint(base + timedelta(minutes=5 * i), float(i)) for i in range(30)]
    result = build_features(points, observed_at=base + timedelta(minutes=145), expected_interval_seconds=300)
    assert result.features["current"] == 29.0
    assert "lag_1" in result.features
    assert result.quality_status == "OK"
    assert build_features(points[:2]).quality_status == "INSUFFICIENT_SAMPLES"
    gapped = points[:10] + [MetricPoint(base + timedelta(hours=4), 100.0)]
    assert build_features(gapped, max_gap_seconds=900).quality_status == "GAP_DETECTED"


def test_candidate_d_is_bounded_and_does_not_alert_during_warmup():
    rows = [{"cpu": float(i), "ram": float(i) / 2} for i in range(30)]
    rows.append({"cpu": 1000.0, "ram": 800.0})
    scores = candidate_d_isolation_scores(rows, history_size=12)
    assert scores[:6] == [None] * 6
    assert scores[-1] is not None and scores[-1] > 3.5
    assert candidate_d_alerts(scores)[-1] is True


def test_scope_rejects_pipe_in_dimension():
    with pytest.raises(ValueError):
        ForecastScope("cluster|bad", "node", "node-a", "cpu", 1, host="node-a")
