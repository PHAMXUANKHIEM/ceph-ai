from datetime import datetime, timedelta, timezone

from config.settings import settings
from shared.models import NodeResourceForecastRun, NodeResourceModelState
from watcher.forecast_replay import (
    compare_persisted_forecast_runs, evaluate_shadow, replay_resource_forecasts,
)


def test_replay_is_read_only_and_scores_linear_rolling_and_consensus(monkeypatch):
    monkeypatch.setattr(settings, "node_resource_forecast_min_samples", 6)
    monkeypatch.setattr(settings, "node_resource_forecast_min_consensus_candidates", 2)
    monkeypatch.setattr(settings, "node_resource_forecast_min_consensus_ratio", .67)
    origin = datetime(2026, 8, 1, tzinfo=timezone.utc)
    points = [
        (origin + timedelta(hours=index), 30.0 + index * .2)
        for index in range(40)
    ]

    result = replay_resource_forecasts(
        points, "cpu", horizon_hours=1, window_hours=[6, 12, 24],
    )

    assert set(result) == {"linear", "rolling_quantile", "consensus"}
    assert result["linear"].evaluated > 0
    assert result["rolling_quantile"].evaluated > 0
    assert result["consensus"].evaluated > 0
    assert result["consensus"].mae is not None
    assert result["consensus"].smape is not None


def test_replay_does_not_create_alerts_or_database_state(monkeypatch):
    monkeypatch.setattr(settings, "node_resource_forecast_min_samples", 6)
    origin = datetime(2026, 8, 1, tzinfo=timezone.utc)
    points = [(origin + timedelta(hours=index), 50.0) for index in range(12)]

    result = replay_resource_forecasts(points, "ram", horizon_hours=1, window_hours=[6])

    assert result["consensus"].evaluated > 0


def test_shadow_evaluation_compares_candidate_without_promotion_or_execution(monkeypatch):
    monkeypatch.setattr(settings, "node_resource_forecast_min_samples", 6)
    origin = datetime(2026, 8, 1, tzinfo=timezone.utc)
    points = [(origin + timedelta(hours=index), 50.0 + index * 0.1) for index in range(30)]

    result = evaluate_shadow(
        points, "cpu", horizon_hours=1, window_hours=[6, 12], minimum_evaluated=6,
    )

    comparison = result["comparison"]
    assert comparison.execution_mode == "SHADOW_ONLY"
    assert comparison.status in {"PROMISING", "HOLD", "INSUFFICIENT_DATA"}
    assert "promotion" not in comparison.reason.lower()


def test_persisted_shadow_comparison_pairs_active_and_candidate_without_writes(db_session, monkeypatch):
    monkeypatch.setattr(settings, "node_resource_learning_min_outcomes", 2)
    state = NodeResourceModelState(
        cluster_name="CS-LAB", host="node-a", metric="cpu", algorithm="linear",
        window_hours=24, selected=True,
    )
    db_session.add(state)
    origin = datetime(2026, 8, 1)
    for index, (active_value, candidate_value, actual) in enumerate(
        ((50, 52, 51), (60, 57, 58), (70, 68, 69)),
    ):
        target = origin + timedelta(hours=index + 1)
        db_session.add_all((
            NodeResourceForecastRun(
                cluster_name="CS-LAB", host="node-a", metric="cpu", algorithm="linear",
                window_hours=24, predicted_at=target - timedelta(hours=1), target_at=target,
                current_percent=active_value, predicted_percent=active_value,
                actual_percent=actual, confidence=.9, status="EVALUATED",
                idempotency_key=f"active-{index}",
            ),
            NodeResourceForecastRun(
                cluster_name="CS-LAB", host="node-a", metric="cpu", algorithm="rolling_quantile",
                window_hours=24, predicted_at=target - timedelta(hours=1), target_at=target,
                current_percent=candidate_value, predicted_percent=candidate_value,
                actual_percent=actual, confidence=.9, status="EVALUATED",
                idempotency_key=f"candidate-{index}",
            ),
        ))
    db_session.commit()

    before = db_session.query(NodeResourceForecastRun).count()
    result = compare_persisted_forecast_runs(db_session, minimum_evaluated=2)

    assert len(result) == 1
    assert result[0].candidate_algorithm == "rolling_quantile:24h"
    assert result[0].execution_mode == "SHADOW_ONLY"
    assert db_session.query(NodeResourceForecastRun).count() == before
