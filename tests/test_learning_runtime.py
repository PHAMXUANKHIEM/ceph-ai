from datetime import datetime, timedelta

from config.settings import settings
from shared import heartbeat, learning_runtime
from shared.models import Cluster, WatcherHeartbeat


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
