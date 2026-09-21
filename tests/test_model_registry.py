from time import perf_counter

from shared import model_registry
from shared.models import (
    ForecastModelPromotionAudit,
    ForecastModelRegistry,
    NodeResourceModelState,
)


def test_promotion_is_blocked_for_unmigrated_scope(db_session):
    active = ForecastModelRegistry(
        scope_type="NODE_RESOURCE", scope_key="cluster-a|node-a|cpu",
        name="resource", version="linear:1h", algorithm="linear",
        feature_schema="resource-v2", training_window_hours=1, status="ACTIVE",
    )
    candidate = ForecastModelRegistry(
        scope_type="NODE_RESOURCE", scope_key="cluster-a|node-a|cpu",
        name="resource", version="candidate:1h", algorithm="candidate_d",
        feature_schema="resource-v2", training_window_hours=1, status="SHADOW",
    )
    db_session.add_all((active, candidate))
    db_session.flush()
    decision = model_registry.request_promotion(db_session, candidate_id=candidate.id, actor="admin")
    assert decision.allowed is False
    assert "scope" in decision.reason


def test_guarded_rollback_restores_exact_previous_model_within_target(db_session):
    scope = "CS-LAB|10.20.1.153|cpu"
    previous = ForecastModelRegistry(
        scope_type="NODE_RESOURCE", scope_key=scope,
        name="node-resource-forecast", version="linear:24h",
        algorithm="linear", feature_schema="node-resource-v1",
        training_window_hours=24, status="RETIRED",
    )
    candidate = ForecastModelRegistry(
        scope_type="NODE_RESOURCE", scope_key=scope,
        name="node-resource-forecast", version="river_mean:24h",
        algorithm="river_mean", feature_schema="node-resource-v1",
        training_window_hours=24, status="ACTIVE",
    )
    db_session.add_all((previous, candidate))
    db_session.flush()
    db_session.add_all((
        NodeResourceModelState(
            cluster_name="CS-LAB", host="10.20.1.153", metric="cpu",
            algorithm="linear", window_hours=24, selected=False,
        ),
        NodeResourceModelState(
            cluster_name="CS-LAB", host="10.20.1.153", metric="cpu",
            algorithm="river_mean", window_hours=24, selected=True,
        ),
        ForecastModelPromotionAudit(
            candidate_model_id=candidate.id,
            previous_active_model_id=previous.id,
            event_type=model_registry.PROMOTED,
            actor="admin", reason="guarded test promotion",
        ),
    ))
    db_session.commit()

    started = perf_counter()
    restored = model_registry.rollback_promotion(
        db_session, candidate_id=candidate.id, actor="admin",
    )
    elapsed = perf_counter() - started

    assert restored.id == previous.id
    assert candidate.status == "RETIRED"
    assert previous.status == "ACTIVE"
    assert db_session.query(NodeResourceModelState).filter_by(
        algorithm="linear", selected=True,
    ).one().host == "10.20.1.153"
    assert db_session.query(NodeResourceModelState).filter_by(
        algorithm="river_mean", selected=True,
    ).count() == 0
    assert elapsed < 1.0
