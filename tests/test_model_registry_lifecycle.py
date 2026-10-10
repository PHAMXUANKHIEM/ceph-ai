import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

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


def test_promotion_blocks_interval_alert_and_quality_regressions():
    rows = []
    for index in range(3):
        row = _evaluation("candidate", "active", datetime(2026, 9, 21) + timedelta(hours=index))
        row.evidence_json = json.dumps({
            "candidate_interval_coverage": 0.75,
            "active_interval_coverage": 0.90,
            "candidate_alert_volume": 11,
            "active_alert_volume": 10,
            "candidate_data_quality_failure_rate": 0.10,
            "active_data_quality_failure_rate": 0.05,
            "resource_budget_ok": True,
            "candidate_drift_status": "STABLE",
            "candidate_drift_score": 0.0,
        })
        rows.append(row)
    decision = model_registry.evaluate_guarded_promotion(
        rows,
        policy=model_registry.PromotionPolicy(
            minimum_outcomes=20, required_consecutive_evaluations=3,
            minimum_interval_coverage=0.8,
            max_alert_volume_increase=0,
            max_data_quality_failure_rate=0.0,
        ),
    )
    assert decision.allowed is False
    assert decision.checks["interval_coverage_guard"] is False
    assert decision.checks["alert_volume_guard"] is False
    assert decision.checks["data_quality_guard"] is False


def test_baseline_bootstrap_is_audited_as_a_system_registration(db_session):
    active, _candidate = model_registry.ensure_shadow_model_pair(
        db_session, _comparison(), now=datetime(2026, 9, 21),
    )
    audits = db_session.query(ForecastModelPromotionAudit).filter_by(candidate_model_id=active.id).all()
    assert [(row.event_type, row.actor) for row in audits] == [
        (model_registry.BASELINE_REGISTERED, model_registry.BOOTSTRAP_ACTOR),
    ]
    assert json.loads(audits[0].evidence_json)["algorithm"] == "linear"

    # A second pass for the same scope must not register or audit again.
    model_registry.ensure_shadow_model_pair(db_session, _comparison(), now=datetime(2026, 9, 22))
    assert db_session.query(ForecastModelPromotionAudit).filter_by(candidate_model_id=active.id).count() == 1


def test_learned_runtime_model_is_never_bootstrapped_as_active(db_session):
    comparison = _comparison()
    comparison.active_algorithm = "river_linear_v2"
    with pytest.raises(ValueError, match="online-learned"):
        model_registry.ensure_shadow_model_pair(db_session, comparison, now=datetime(2026, 9, 21))
    assert db_session.query(ForecastModelRegistry).filter_by(status="ACTIVE").count() == 0


def test_learned_algorithm_detection():
    assert model_registry.is_learned_algorithm("river_mean")
    assert model_registry.is_learned_algorithm("river_linear_v2:24h")
    assert not model_registry.is_learned_algorithm("linear")
    assert not model_registry.is_learned_algorithm("seasonal_median:168h")



# --- approval selects production-style registry rows (09/10/2026) ------------------------------

def _registry_row(scope_type, scope_key, algorithm, window):
    return SimpleNamespace(scope_type=scope_type, scope_key=scope_key, algorithm=algorithm,
                           training_window_hours=window)


def test_approval_selects_the_windowed_algorithm_within_the_keyed_horizon(db_session):
    states = {}
    for horizon in (6, 24):
        for window in (24, 72):
            states[(horizon, window)] = NodeResourceModelState(
                cluster_name="CS-LAB", host="10.3.54.118", metric="ram", algorithm="linear",
                window_hours=window, horizon_hours=horizon, selected=(window == 24),
            )
    db_session.add_all(states.values())
    db_session.flush()

    model_registry._select_runtime_state(
        db_session, _registry_row("NODE_RESOURCE", "CS-LAB|10.3.54.118|ram|h6", "linear:72h", 72), selected=True,
    )

    assert {key for key, state in states.items() if state.selected} == {(6, 72), (24, 24)}  # h24 untouched


def test_approval_selects_a_volume_state_by_horizon(db_session):
    from shared.models import Cluster, VolumeModelState

    db_session.add(Cluster(id="c1", name="lab", ceph_mon_nodes="", ssh_user="root", ssh_key_path="/tmp/key"))
    db_session.flush()
    one = VolumeModelState(cluster_id="c1", pool="volumes", image="img", metric="write_latency_ms",
                           algorithm="seasonal_median", window_hours=168, horizon_hours=1, selected=False)
    six = VolumeModelState(cluster_id="c1", pool="volumes", image="img", metric="write_latency_ms",
                           algorithm="seasonal_median", window_hours=168, horizon_hours=6, selected=False)
    db_session.add_all([one, six])
    db_session.flush()

    model_registry._select_runtime_state(
        db_session,
        _registry_row("VOLUME", "c1|volumes|img|write_latency_ms|h6", "seasonal_median:168h", 168),
        selected=True,
    )

    assert (one.selected, six.selected) == (False, True)


@pytest.mark.parametrize("scope_key", ["CS-LAB|host", "CS-LAB|host|ram|6", "CS-LAB|host|ram|h6|extra"])
def test_a_malformed_scope_key_is_refused(db_session, scope_key):
    with pytest.raises(ValueError, match="scope key"):
        model_registry._select_runtime_state(
            db_session, _registry_row("NODE_RESOURCE", scope_key, "linear:72h", 72), selected=True,
        )


@pytest.mark.parametrize(("scope_type", "scope_key", "expected"), [
    ("NODE_RESOURCE", "CS-LAB|10.3.53.69|ram|h1", ("node", "10.3.53.69", "ram", 1, "10.3.53.69")),
    ("NODE_RESOURCE", "CS-LAB|10.3.53.69|cpu|h24", ("node", "10.3.53.69", "cpu", 24, "10.3.53.69")),
    ("NODE_RESOURCE", "CS-LAB|10.3.53.69|ram", ("node", "10.3.53.69", "ram", 72, "10.3.53.69")),
    ("VOLUME", "c1|volumes|img|write_latency_ms|h6", ("volume", "volumes/img", "write_latency_ms", 6, None)),
])
def test_scope_keys_with_a_horizon_suffix_parse_to_full_dimensions(scope_type, scope_key, expected):
    from shared.forecast_scope import parse_legacy_scope

    scope = parse_legacy_scope(scope_type, scope_key, horizon_hours=72)

    assert (scope.entity_type, scope.entity_id, scope.metric, scope.horizon_hours, scope.host) == expected


@pytest.mark.parametrize("scope_key", ["CS-LAB|host|ram|6", "CS-LAB|host|ram|h0", "CS-LAB|host|ram|h6|extra"])
def test_a_scope_key_with_a_bad_suffix_still_parses_to_nothing(scope_key):
    from shared.forecast_scope import parse_legacy_scope

    assert parse_legacy_scope("NODE_RESOURCE", scope_key, horizon_hours=24) is None


def test_a_horizon_keyed_model_gets_its_dimensions_when_registered_again(db_session):
    """10/10/2026: 288 node-resource models keyed "...|h1" had no dimensions; promotion said UNKNOWN_SCOPE."""
    from shared.models import ForecastModelRegistry

    legacy = ForecastModelRegistry(
        scope_type="NODE_RESOURCE", scope_key="CS-LAB|10.3.53.69|ram|h1", name="node-resource-forecast",
        version="rolling_quantile:72h:72h", algorithm="rolling_quantile:72h", feature_schema="node-resource-v1",
        training_window_hours=72, status="SHADOW",
    )
    db_session.add(legacy)
    db_session.flush()
    assert not model_registry._scope_ready(legacy)

    again = model_registry.register_candidate(
        db_session, scope_type="NODE_RESOURCE", scope_key="CS-LAB|10.3.53.69|ram|h1",
        name="node-resource-forecast", version="rolling_quantile:72h:72h", algorithm="rolling_quantile:72h",
        feature_schema="node-resource-v1", training_window_hours=72,
    )

    assert again.id == legacy.id and model_registry._scope_ready(again)
    assert (again.scope_schema, again.host, again.metric, again.horizon_hours) == (SCOPE_SCHEMA, "10.3.53.69", "ram", 1)
