from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from shared.model_registry import (
    PromotionPolicy,
    approve_promotion,
    evaluate_guarded_promotion,
    ensure_shadow_model_pair,
    list_scope,
    register_candidate,
    record_shadow_evaluation,
    request_promotion,
    rollback_promotion,
    set_status,
)
from shared.models import ForecastModelEvaluation, NodeResourceModelState


def _evaluation(index: int, *, mae_delta: float = -1.0, smape_delta: float = -1.0,
                fpr_delta: float = 0.0):
    return SimpleNamespace(
        target_at=datetime(2026, 9, 18, 6 + index),
        active_evaluated=20, candidate_evaluated=20,
        active_mae=10.0, candidate_mae=10.0 + mae_delta,
        active_smape=20.0, candidate_smape=20.0 + smape_delta,
        active_false_positive_rate=0.10,
        candidate_false_positive_rate=0.10 + fpr_delta,
    )


def test_guarded_promotion_requires_a_consecutive_clean_streak():
    policy = PromotionPolicy(minimum_outcomes=20, required_consecutive_evaluations=3)
    decision = evaluate_guarded_promotion([_evaluation(index) for index in range(3)], policy=policy)
    assert decision.allowed is True
    assert decision.status == "PROMOTION_REQUESTED"

    blocked = evaluate_guarded_promotion(
        [_evaluation(0), _evaluation(1), _evaluation(2, fpr_delta=.01)], policy=policy,
    )
    assert blocked.allowed is False
    assert "false_positive_rate_guard" in blocked.reason


def test_guarded_promotion_fails_closed_when_outcomes_are_insufficient():
    policy = PromotionPolicy(minimum_outcomes=20, required_consecutive_evaluations=3)
    insufficient = _evaluation(0)
    insufficient.active_evaluated = 19
    insufficient.candidate_evaluated = 19
    decision = evaluate_guarded_promotion(
        [insufficient, _evaluation(1), _evaluation(2)], policy=policy,
    )
    assert decision.allowed is False
    assert "minimum_outcomes" in decision.reason


def test_guarded_promotion_blocks_latency_or_drift_budget_breach():
    policy = PromotionPolicy(
        minimum_outcomes=20, required_consecutive_evaluations=1,
        max_poll_latency_ms=100.0, max_drift_score=0.0,
    )
    slow = _evaluation(0)
    slow.evidence_json = json.dumps({"poll_latency_ms": 101.0})
    decision = evaluate_guarded_promotion([slow], policy=policy)
    assert decision.allowed is False
    assert "resource_budget_guard" in decision.reason

    drifted = _evaluation(0)
    drifted.evidence_json = json.dumps({"candidate_drift_status": "DRIFT"})
    decision = evaluate_guarded_promotion([drifted], policy=policy)
    assert decision.allowed is False
    assert "drift_guard" in decision.reason


def test_registry_tracks_versioned_lifecycle_without_selecting_a_model(db_session):
    now = datetime(2026, 9, 18, 6, 0)
    row = register_candidate(
        db_session, scope_type="node_resource", scope_key="CS-LAB/node-a/cpu",
        name="resource-forecast", version="v2", algorithm="linear",
        feature_schema="cpu-v1", training_window_hours=24, now=now,
    )
    assert row.status == "CANDIDATE"
    assert set_status(db_session, row, status="SHADOW", now=now).status == "SHADOW"
    assert set_status(db_session, row, status="ACTIVE", reason="shadow passed", now=now).active_since == now
    assert set_status(db_session, row, status="RETIRED", now=now).retired_at == now
    assert list_scope(db_session, scope_type="NODE_RESOURCE", scope_key="CS-LAB/node-a/cpu") == [row]


def test_registry_rejects_ambiguous_or_unsafe_registration(db_session):
    with pytest.raises(ValueError, match="scope type"):
        register_candidate(
            db_session, scope_type="UNKNOWN", scope_key="x", name="m", version="v1",
            algorithm="linear", feature_schema="v1", training_window_hours=1,
        )
    row = register_candidate(
        db_session, scope_type="VOLUME", scope_key="c/p/i/latency",
        name="volume-forecast", version="v1", algorithm="seasonal_median",
        feature_schema="volume-v1", training_window_hours=72,
    )
    with pytest.raises(ValueError, match="requires a reason"):
        set_status(db_session, row, status="BLOCKED")


def test_registry_allows_only_one_active_model_per_scope(db_session):
    common = dict(
        scope_type="NODE_RESOURCE", scope_key="c/node-a/ram",
        algorithm="linear", feature_schema="ram-v1", training_window_hours=24,
    )
    first = register_candidate(db_session, name="resource", version="v1", **common)
    second = register_candidate(db_session, name="resource", version="v2", **common)
    set_status(db_session, first, status="ACTIVE")
    with pytest.raises(ValueError, match="already has an active"):
        set_status(db_session, second, status="ACTIVE")


def test_shadow_pair_reconciles_ungoverned_adaptive_baseline_without_promotion(db_session):
    old = register_candidate(
        db_session, scope_type="NODE_RESOURCE", scope_key="CS-LAB|node-a|cpu",
        name="node-resource-forecast", version="linear:720h", algorithm="linear",
        feature_schema="node-resource-v1", training_window_hours=720,
    )
    set_status(db_session, old, status="ACTIVE")
    comparison = SimpleNamespace(
        scope_type="NODE_RESOURCE", scope_key="CS-LAB|node-a|cpu",
        active_algorithm="linear", active_window_hours=168,
        candidate_algorithm="rolling_quantile", candidate_window_hours=168,
    )
    active, candidate = ensure_shadow_model_pair(db_session, comparison)
    assert active.algorithm == "linear"
    assert active.training_window_hours == 168
    assert active.status == "ACTIVE"
    assert candidate.status == "SHADOW"
    assert old.status == "RETIRED"


def test_shadow_pair_still_fails_closed_after_governed_promotion_history(db_session):
    old = register_candidate(
        db_session, scope_type="NODE_RESOURCE", scope_key="CS-LAB|node-a|cpu",
        name="node-resource-forecast", version="linear:720h", algorithm="linear",
        feature_schema="node-resource-v1", training_window_hours=720,
    )
    set_status(db_session, old, status="ACTIVE")
    db_session.add(ForecastModelPromotionAudit(
        candidate_model_id=old.id, previous_active_model_id=None,
        event_type="PROMOTION_BLOCKED", actor="test", reason="governed",
    ))
    db_session.flush()
    comparison = SimpleNamespace(
        scope_type="NODE_RESOURCE", scope_key="CS-LAB|node-a|cpu",
        active_algorithm="linear", active_window_hours=168,
        candidate_algorithm="rolling_quantile", candidate_window_hours=168,
    )
    with pytest.raises(ValueError, match="runtime active model differs"):
        ensure_shadow_model_pair(db_session, comparison)


def test_operator_approved_promotion_updates_runtime_selection_and_rolls_back(db_session):
    active_state = NodeResourceModelState(
        cluster_name="CS-LAB", host="node-a", metric="cpu", algorithm="linear",
        window_hours=24, selected=True,
    )
    candidate_state = NodeResourceModelState(
        cluster_name="CS-LAB", host="node-a", metric="cpu", algorithm="rolling_quantile",
        window_hours=72, selected=False,
    )
    db_session.add_all([active_state, candidate_state])
    db_session.flush()
    common = dict(
        scope_type="NODE_RESOURCE", scope_key="CS-LAB|node-a|cpu",
        name="node-resource-forecast", feature_schema="node-resource-v1",
    )
    active = register_candidate(
        db_session, version="linear:24h", algorithm="linear", training_window_hours=24, **common,
    )
    candidate = register_candidate(
        db_session, version="rolling_quantile:72h", algorithm="rolling_quantile",
        training_window_hours=72, **common,
    )
    set_status(db_session, active, status="ACTIVE")
    set_status(db_session, candidate, status="SHADOW")
    for index in range(3):
        comparison = SimpleNamespace(
            latest_target_at=datetime(2026, 9, 18, 6 + index),
            execution_mode="SHADOW_ONLY", active_algorithm="linear",
            candidate_algorithm="rolling_quantile", status="PROMISING",
            reason="better", active_evaluated=20, candidate_evaluated=20,
            active_mae=10.0, candidate_mae=9.0,
            active_rmse=11.0, candidate_rmse=10.0,
            active_smape=20.0, candidate_smape=19.0,
            active_bias=0.1, candidate_bias=0.0,
            active_false_positive_rate=0.1, candidate_false_positive_rate=0.1,
        )
        record_shadow_evaluation(
            db_session, active_model=active, candidate_model=candidate,
            comparison=comparison,
        )
    db_session.commit()

    decision = request_promotion(db_session, candidate_id=candidate.id, actor="operator")
    assert decision.allowed is True
    db_session.commit()
    promoted = approve_promotion(db_session, candidate_id=candidate.id, actor="operator")
    assert promoted.status == "ACTIVE"
    assert active.status == "RETIRED"
    assert candidate_state.selected is True
    assert active_state.selected is False
    db_session.commit()

    previous = rollback_promotion(db_session, candidate_id=candidate.id, actor="operator")
    assert previous.id == active.id
    assert active.status == "ACTIVE"
    assert candidate.status == "RETIRED"
    assert active_state.selected is True
    assert candidate_state.selected is False
    assert db_session.query(ForecastModelEvaluation).count() == 3
