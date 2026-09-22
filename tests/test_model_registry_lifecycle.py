import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from shared import model_registry
from shared.forecast_scope import SCOPE_SCHEMA
from shared.models import (
    ForecastModelEvaluation,
    ForecastModelPromotionAudit,
    ForecastModelRegistry,
    NodeResourceModelState,
)


SCOPE_KEY = "CS-LAB|10.20.1.153|cpu"


def _comparison():
    return SimpleNamespace(
        scope_type="NODE_RESOURCE",
        scope_key=SCOPE_KEY,
        active_algorithm="linear",
        active_window_hours=24,
        candidate_algorithm="river_mean",
        candidate_window_hours=24,
        horizon_hours=24,
    )


def _evaluation(candidate_id, active_id, target_at):
    return ForecastModelEvaluation(
        candidate_model_id=candidate_id,
        active_model_id=active_id,
        target_at=target_at,
        active_evaluated=20,
        candidate_evaluated=20,
        active_mae=10.0,
        candidate_mae=8.0,
        active_smape=10.0,
        candidate_smape=8.0,
        active_false_positive_rate=0.1,
        candidate_false_positive_rate=0.1,
        status="PROMISING",
        reason="candidate evidence",
        evidence_json=json.dumps({
            "resource_budget_ok": True,
            "poll_latency_ms": 100.0,
            "candidate_drift_status": "STABLE",
            "candidate_drift_score": 0.0,
        }),
    )


def test_shadow_pair_records_full_cluster_host_metric_scope(db_session):
    active, candidate = model_registry.ensure_shadow_model_pair(
        db_session, _comparison(), now=datetime(2026, 9, 21),
    )

    assert active.status == "ACTIVE"
    assert candidate.status == "SHADOW"
    assert candidate.scope_schema == SCOPE_SCHEMA
    assert candidate.cluster_id == "CS-LAB"
    assert candidate.entity_type == "node"
    assert candidate.entity_id == "10.20.1.153"
    assert candidate.host == "10.20.1.153"
    assert candidate.metric == "cpu"
    assert candidate.horizon_hours == 24


def test_promotion_connects_shadow_candidate_to_registry_and_rollback(db_session):
    active, candidate = model_registry.ensure_shadow_model_pair(
        db_session, _comparison(), now=datetime(2026, 9, 21),
    )
    db_session.add_all([
        NodeResourceModelState(
            cluster_name="CS-LAB", host="10.20.1.153", metric="cpu",
            algorithm="linear", window_hours=24, selected=True,
        ),
        NodeResourceModelState(
            cluster_name="CS-LAB", host="10.20.1.153", metric="cpu",
            algorithm="river_mean", window_hours=24, selected=False,
        ),
    ])
    target = datetime(2026, 9, 21)
    db_session.add_all([
        _evaluation(candidate.id, active.id, target + timedelta(hours=index))
        for index in range(3)
    ])
    db_session.commit()

    policy = model_registry.PromotionPolicy(
        minimum_outcomes=20, required_consecutive_evaluations=3,
    )
    decision = model_registry.request_promotion(
        db_session, candidate_id=candidate.id, actor="operator", policy=policy,
    )
    assert decision.allowed is True
    assert candidate.status == "SHADOW"

    promoted = model_registry.approve_promotion(
        db_session, candidate_id=candidate.id, actor="operator", policy=policy,
    )
    assert promoted.status == "ACTIVE"
    assert active.status == "RETIRED"
    assert db_session.query(NodeResourceModelState).filter_by(
        algorithm="river_mean", selected=True,
    ).one().host == "10.20.1.153"

    restored = model_registry.rollback_promotion(
        db_session, candidate_id=candidate.id, actor="operator",
    )
    assert restored.id == active.id
    assert restored.status == "ACTIVE"
    assert candidate.status == "RETIRED"
    assert db_session.query(ForecastModelPromotionAudit).filter(
        ForecastModelPromotionAudit.candidate_model_id == candidate.id,
        ForecastModelPromotionAudit.event_type.in_((model_registry.PROMOTED, model_registry.ROLLED_BACK)),
    ).count() == 2


def test_promotion_blocks_registry_scope_missing_host_or_metric(db_session):
    active = model_registry.register_candidate(
        db_session, scope_type="NODE_RESOURCE", scope_key="CS-LAB||cpu",
        name="node-resource-forecast", version="linear:24h",
        algorithm="linear", feature_schema="node-resource-v1", training_window_hours=24,
    )
    model_registry.set_status(db_session, active, status="ACTIVE", reason="test baseline")
    candidate = model_registry.register_candidate(
        db_session, scope_type="NODE_RESOURCE", scope_key="CS-LAB||cpu",
        name="node-resource-forecast", version="river_mean:24h",
        algorithm="river_mean", feature_schema="node-resource-v1", training_window_hours=24,
    )
    model_registry.set_status(db_session, candidate, status="SHADOW", reason="test shadow")
    db_session.commit()

    decision = model_registry.request_promotion(
        db_session, candidate_id=candidate.id, actor="operator",
    )

    assert decision.allowed is False
    assert decision.status == model_registry.PROMOTION_BLOCKED
    assert decision.checks == {"scope_dimensions": False}
    audit = db_session.query(ForecastModelPromotionAudit).filter_by(
        candidate_model_id=candidate.id,
        event_type=model_registry.PROMOTION_BLOCKED,
    ).one()
    assert "missing dimensions" in audit.reason


def test_volume_scope_keeps_cluster_pool_image_metric_and_horizon(db_session):
    row = model_registry.register_candidate(
        db_session,
        scope_type="VOLUME",
        scope_key="cluster-a|pool-a|image-a|used_bytes",
        name="volume-forecast",
        version="seasonal_median:24h",
        algorithm="seasonal_median",
        feature_schema="volume-v1",
        training_window_hours=24,
    )
    assert row.scope_schema == SCOPE_SCHEMA
    assert row.cluster_id == "cluster-a"
    assert row.entity_type == "volume"
    assert row.entity_id == "pool-a/image-a"
    assert row.host is None
    assert row.metric == "used_bytes"
    assert row.horizon_hours == 24
