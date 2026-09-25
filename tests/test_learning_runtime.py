from datetime import datetime, timedelta

from config.settings import settings
from shared import heartbeat, learning_runtime
from shared.models import (
    Cluster,
    ForecastModelPromotionAudit,
    ForecastModelRegistry,
    WatcherHeartbeat,
)


NOW = datetime(2026, 9, 18, 10, 0, 0)


def _record(db_session, *, success=True, failures=0, polled_at=NOW, cluster_id=None):
    row = WatcherHeartbeat(
        cluster_id=cluster_id,
        success=success,
        mon_node="10.0.0.1" if success else None,
        error_message=None if success else "MON unavailable",
        polled_at=polled_at,
        consecutive_failures=failures,
        last_success_at=polled_at if success else None,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_disabled_by_default_is_fail_closed(db_session, monkeypatch):
    monkeypatch.setattr(settings, "online_learning_enabled", False)
    decision = learning_runtime.evaluate(db_session, "cluster-1", now=NOW)
    assert decision.enabled is False
    assert decision.can_observe is False
    assert decision.reason == "online learning feature flag is disabled"


def test_audit_only_observes_but_does_not_update(db_session, monkeypatch):
    _record(db_session)
    monkeypatch.setattr(settings, "online_learning_enabled", True)
    monkeypatch.setattr(settings, "online_learning_mode", "AUDIT_ONLY")
    decision = learning_runtime.evaluate(db_session, None, now=NOW)
    assert decision.can_observe is True
    assert decision.can_update_shadow is False
    assert decision.can_update_active is False


def test_shadow_only_allows_shadow_state_only(db_session, monkeypatch):
    _record(db_session)
    monkeypatch.setattr(settings, "online_learning_enabled", True)
    monkeypatch.setattr(settings, "online_learning_mode", "SHADOW_ONLY")
    decision = learning_runtime.evaluate(db_session, None, now=NOW)
    assert decision.can_update_shadow is True
    assert decision.can_update_active is False


def test_active_allows_active_update_only_when_healthy(db_session, monkeypatch):
    _record(db_session)
    monkeypatch.setattr(settings, "online_learning_enabled", True)
    monkeypatch.setattr(settings, "online_learning_mode", "ACTIVE")
    decision = learning_runtime.evaluate(db_session, None, now=NOW)
    assert decision.can_update_active is True


def _runtime_active(db_session, monkeypatch):
    db_session.add(Cluster(
        id="cluster-1", name="CS-LAB", ceph_mon_nodes="",
        ssh_user="root", ssh_key_path="/tmp/key",
    ))
    db_session.flush()
    _record(db_session, cluster_id="cluster-1")
    monkeypatch.setattr(settings, "online_learning_enabled", True)
    monkeypatch.setattr(settings, "online_learning_mode", "ACTIVE")
    return learning_runtime.evaluate(
        db_session, "cluster-1", host="node-a", metric="cpu", now=NOW,
    )


def _registry_row(db_session, *, status="SHADOW"):
    row = ForecastModelRegistry(
        scope_type="NODE_RESOURCE",
        scope_key="cluster-1|node-a|cpu",
        scope_schema="forecast-scope-v2",
        cluster_id="cluster-1",
        entity_type="node",
        entity_id="node-a",
        host="node-a",
        metric="cpu",
        horizon_hours=24,
        name="online-learner",
        version="river-mean-v1",
        algorithm="river_mean",
        feature_schema="scalar-v1",
        training_window_hours=24,
        status=status,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_update_target_defaults_to_shadow_without_registry(db_session, monkeypatch):
    runtime = _runtime_active(db_session, monkeypatch)
    decision = learning_runtime.resolve_update_target(
        db_session, runtime, cluster_id="cluster-1", host="node-a", metric="cpu",
        algorithm="river_mean", model_version="river-mean-v1",
    )
    assert decision.target == "shadow"
    assert decision.allowed is True
    assert decision.registry_model_id is None


def test_update_target_keeps_candidate_in_shadow(db_session, monkeypatch):
    _registry_row(db_session, status="CANDIDATE")
    db_session.commit()
    runtime = _runtime_active(db_session, monkeypatch)
    decision = learning_runtime.resolve_update_target(
        db_session, runtime, cluster_id="cluster-1", host="node-a", metric="cpu",
        algorithm="river_mean", model_version="river-mean-v1",
    )
    assert decision.target == "shadow"
    assert decision.allowed is True
    assert "CANDIDATE" in decision.reason


def test_update_target_does_not_cross_host_or_metric_scope(db_session, monkeypatch):
    _registry_row(db_session, status="CANDIDATE")
    db_session.commit()
    runtime = _runtime_active(db_session, monkeypatch)

    wrong_host = learning_runtime.resolve_update_target(
        db_session, runtime, cluster_id="cluster-1", host="node-b", metric="cpu",
        algorithm="river_mean", model_version="river-mean-v1",
    )
    wrong_metric = learning_runtime.resolve_update_target(
        db_session, runtime, cluster_id="cluster-1", host="node-a", metric="memory",
        algorithm="river_mean", model_version="river-mean-v1",
    )
    assert wrong_host.registry_model_id is None
    assert wrong_metric.registry_model_id is None
    assert wrong_host.target == "shadow"
    assert wrong_metric.target == "shadow"


def test_update_target_requires_operator_promotion_audit_for_active(db_session, monkeypatch):
    row = _registry_row(db_session, status="ACTIVE")
    db_session.commit()
    runtime = _runtime_active(db_session, monkeypatch)
    blocked = learning_runtime.resolve_update_target(
        db_session, runtime, cluster_id="cluster-1", host="node-a", metric="cpu",
        algorithm="river_mean", model_version="river-mean-v1",
    )
    assert blocked.target == "shadow"
    assert blocked.allowed is False
    assert "promotion audit" in blocked.reason

    db_session.add(ForecastModelPromotionAudit(
        candidate_model_id=row.id,
        event_type="PROMOTED",
        actor="operator",
        reason="approved after guarded evaluation",
    ))
    db_session.commit()
    active = learning_runtime.resolve_update_target(
        db_session, runtime, cluster_id="cluster-1", host="node-a", metric="cpu",
        algorithm="river_mean", model_version="river-mean-v1",
    )
    assert active.target == "active"
    assert active.allowed is True


def test_update_target_blocks_promoted_model_when_runtime_is_shadow_only(db_session, monkeypatch):
    row = _registry_row(db_session, status="ACTIVE")
    db_session.add(ForecastModelPromotionAudit(
        candidate_model_id=row.id,
        event_type="PROMOTED",
        actor="operator",
        reason="approved after guarded evaluation",
    ))
    db_session.commit()
    _runtime_active(db_session, monkeypatch)
    monkeypatch.setattr(settings, "online_learning_mode", "SHADOW_ONLY")
    runtime = learning_runtime.evaluate(
        db_session, "cluster-1", host="node-a", metric="cpu", now=NOW,
    )
    decision = learning_runtime.resolve_update_target(
        db_session, runtime, cluster_id="cluster-1", host="node-a", metric="cpu",
        algorithm="river_mean", model_version="river-mean-v1",
    )
    assert decision.target == "shadow"
    assert decision.allowed is False
    assert "not ACTIVE" in decision.reason


def test_canary_scope_fails_closed_outside_one_stream(db_session, monkeypatch):
    db_session.add(Cluster(
        id="cluster-1", name="CS-LAB", ceph_mon_nodes="",
        ssh_user="root", ssh_key_path="/tmp/key",
    ))
    db_session.commit()
    _record(db_session, cluster_id="cluster-1")
    monkeypatch.setattr(settings, "online_learning_enabled", True)
    monkeypatch.setattr(settings, "online_learning_mode", "SHADOW_ONLY")
    monkeypatch.setattr(settings, "online_learning_canary_enabled", True)
    monkeypatch.setattr(settings, "online_learning_canary_cluster_id", "cluster-1")
    monkeypatch.setattr(settings, "online_learning_canary_host", "node-a")
    monkeypatch.setattr(settings, "online_learning_canary_metrics", "cpu")

    allowed = learning_runtime.evaluate(
        db_session, "cluster-1", host="node-a", metric="cpu", now=NOW,
    )
    assert allowed.can_update_shadow is True

    blocked = learning_runtime.evaluate(
        db_session, "cluster-1", host="node-b", metric="cpu", now=NOW,
    )
    assert blocked.enabled is False
    assert blocked.mode == "CANARY_SCOPE"
    assert "outside" in blocked.reason


def test_canary_scope_with_missing_identity_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "online_learning_canary_enabled", True)
    monkeypatch.setattr(settings, "online_learning_canary_cluster_id", "cluster-1")
    monkeypatch.setattr(settings, "online_learning_canary_host", "node-a")
    monkeypatch.setattr(settings, "online_learning_canary_metrics", "cpu")
    assert learning_runtime.canary_scope_allows("cluster-1", None, "cpu") is False


def test_kill_switch_blocks_every_mode(db_session, monkeypatch):
    _record(db_session)
    monkeypatch.setattr(settings, "online_learning_enabled", True)
    monkeypatch.setattr(settings, "online_learning_mode", "ACTIVE")
    monkeypatch.setattr(settings, "online_learning_kill_switch", True)
    decision = learning_runtime.evaluate(db_session, None, now=NOW)
    assert decision.enabled is False
    assert decision.reason == "online learning kill switch is enabled"


def test_stale_heartbeat_blocks_learning(db_session, monkeypatch):
    _record(db_session, polled_at=NOW - timedelta(seconds=121))
    monkeypatch.setattr(settings, "online_learning_enabled", True)
    monkeypatch.setattr(settings, "online_learning_mode", "SHADOW_ONLY")
    monkeypatch.setattr(settings, "online_learning_watcher_staleness_seconds", 120)
    decision = learning_runtime.evaluate(db_session, None, now=NOW)
    assert decision.enabled is False
    assert decision.reason == "Watcher heartbeat is stale"


def test_consecutive_watcher_failures_block_learning(db_session, monkeypatch):
    _record(db_session, success=False, failures=3)
    monkeypatch.setattr(settings, "online_learning_enabled", True)
    monkeypatch.setattr(settings, "online_learning_mode", "SHADOW_ONLY")
    monkeypatch.setattr(settings, "online_learning_watcher_failure_threshold", 3)
    decision = learning_runtime.evaluate(db_session, None, now=NOW)
    assert decision.enabled is False
    assert decision.reason == "Watcher has consecutive failures"
    assert decision.watcher_failure_streak == 3


def test_heartbeat_record_resets_and_increments_failure_streak(db_session):
    heartbeat.record(
        db_session, cluster_id=None, success=True,
        mon_node="10.0.0.1", error_message=None, polled_at=NOW,
    )
    heartbeat.record(
        db_session, cluster_id=None, success=False,
        mon_node=None, error_message="down", polled_at=NOW + timedelta(seconds=15),
    )
    heartbeat.record(
        db_session, cluster_id=None, success=False,
        mon_node=None, error_message="still down", polled_at=NOW + timedelta(seconds=30),
    )
    db_session.commit()
    row = heartbeat.get_latest(db_session, None)
    assert row.consecutive_failures == 2
    assert row.last_success_at == NOW

    heartbeat.record(
        db_session, cluster_id=None, success=True,
        mon_node="10.0.0.1", error_message=None, polled_at=NOW + timedelta(seconds=45),
    )
    db_session.commit()
    assert row.consecutive_failures == 0
    assert row.last_success_at == NOW + timedelta(seconds=45)
