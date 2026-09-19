from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db
from shared.db import Base
from shared.models import (
    Cluster,
    NodeResourceForecastRun,
    OnlineLearnerAudit,
    OnlineLearnerLabel,
    WatcherHeartbeat,
)
from shared.online_learning_consumer import consume_sample, consume_samples
from shared.online_learning_controls import PAUSED, set_status
from shared.online_learning_labels import enqueue_verified_outcomes


def _session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    return factory


def test_consumer_audits_no_label_without_learning(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_enabled", True)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_require_verified_label", True)
    with factory() as session:
        cluster = Cluster(
            id="cluster-a", name="CS-LAB", ceph_mon_nodes="", ssh_user="root", ssh_key_path="/tmp/key",
        )
        session.add(cluster)
        session.commit()
    result = consume_sample(
        cluster_id="cluster-a", host="node-1", metric="cpu", value=42.0,
        observed_at=datetime.now(timezone.utc), sample_id="s-1",
    )
    assert result is not None
    assert result.quality.status == "NO_LABEL"
    assert result.update_applied is False
    with factory() as session:
        audit = session.scalar(select(OnlineLearnerAudit).where(OnlineLearnerAudit.sample_id == "s-1"))
        assert audit is not None
        assert audit.quality_status == "NO_LABEL"


def test_consumer_requires_healthy_runtime_before_shadow_update(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_enabled", True)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_require_verified_label", False)
    now = datetime.now(timezone.utc)
    with factory() as session:
        session.add(Cluster(id="cluster-a", name="CS-LAB", ceph_mon_nodes="", ssh_user="root", ssh_key_path="/tmp/key"))
        session.add(WatcherHeartbeat(
            id=1, cluster_id="cluster-a", success=True, mon_node="mon-1",
            error_message=None, polled_at=now, consecutive_failures=0,
            last_success_at=now,
        ))
        session.commit()
    result = consume_sample(
        cluster_id="cluster-a", host="node-1", metric="cpu", value=42.0,
        observed_at=now, sample_id="s-2", label=42.0,
    )
    assert result is not None
    assert result.quality.status == "READY_TO_LEARN"
    assert result.update_applied is False  # AUDIT_ONLY remains fail-closed


def test_paused_scope_skips_audit_and_learning_write(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_enabled", True)
    now = datetime.now(timezone.utc)
    with factory() as session:
        set_status(
            session,
            cluster_id="cluster-paused",
            host="node-1",
            metric="cpu",
            status=PAUSED,
            actor="admin",
            reason="operator is investigating drift",
        )
        session.commit()

    result = consume_sample(
        cluster_id="cluster-paused",
        host="node-1",
        metric="cpu",
        value=42.0,
        observed_at=now,
        sample_id="paused-scope",
        label=42.0,
    )

    assert result.runtime_mode == "PAUSED"
    assert result.update_applied is False
    with factory() as session:
        assert session.query(OnlineLearnerAudit).filter_by(sample_id="paused-scope").count() == 0


def test_consumer_uses_bounded_batch_runner(monkeypatch):
    _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_enabled", True)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_require_verified_label", True)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_max_samples_per_cycle", 1)
    now = datetime.now(timezone.utc)
    results = consume_samples([
        {"cluster_id": "cluster-a", "host": "node-1", "metric": "cpu", "value": 1.0, "observed_at": now, "sample_id": "s-3"},
        {"cluster_id": "cluster-a", "host": "node-1", "metric": "memory", "value": 2.0, "observed_at": now, "sample_id": "s-4"},
    ])
    assert len(results) == 1
    assert results[0].sample_id == "s-3"


def test_verified_forecast_outcome_releases_previous_no_label_audit(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_enabled", True)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_mode", "SHADOW_ONLY")
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_require_verified_label", True)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_min_verified_evidence", 1)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with factory() as session:
        session.add(Cluster(id="cluster-a", name="CS-LAB", ceph_mon_nodes="", ssh_user="root", ssh_key_path="/tmp/key"))
        session.add(WatcherHeartbeat(
            id=1, cluster_id="cluster-a", success=True, mon_node="mon-1",
            error_message=None, polled_at=now, consecutive_failures=0,
            last_success_at=now,
        ))
        session.add(OnlineLearnerAudit(
            cluster_key="cluster-a", host="node-1", metric="cpu", sample_id="s-verified",
            observed_at=now.replace(tzinfo=None), value=42.0, label=None,
            quality_status="NO_LABEL", quality_reason="verified label is required",
            runtime_mode="AUDIT_ONLY", runtime_reason="initial audit", update_applied=False,
            model_version="river-mean-v1",
        ))
        session.add(NodeResourceForecastRun(
            id="run-verified", cluster_name="CS-LAB", host="node-1", metric="cpu",
            algorithm="linear", window_hours=24, predicted_at=now.replace(tzinfo=None),
            target_at=now.replace(tzinfo=None), current_percent=42.0,
            predicted_percent=45.0, confidence=0.9, actual_percent=43.0,
            absolute_error=2.0, status="EVALUATED", idempotency_key="run-key",
            evaluated_at=now.replace(tzinfo=None), consensus_status="CONSENSUS",
        ))
        session.commit()

    result = consume_sample(
        cluster_id="cluster-a", host="node-1", metric="cpu", value=42.0,
        observed_at=now, sample_id="s-verified",
    )
    assert result is not None
    assert result.quality.status == "READY_TO_LEARN"
    assert result.update_applied is True
    with factory() as session:
        audit = session.query(OnlineLearnerAudit).filter_by(sample_id="s-verified").one()
        label = session.query(OnlineLearnerLabel).filter_by(sample_id="s-verified").one()
        assert audit.label == 43.0
        assert audit.update_applied is True
        assert label.status == "CONSUMED"
        assert label.outcome == "VERIFIED_SUCCESS"
        assert label.evidence_count == 1


def test_verified_outcome_waits_for_minimum_evidence(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_min_verified_evidence", 2)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with factory() as session:
        session.add(Cluster(id="cluster-a", name="CS-LAB", ceph_mon_nodes="", ssh_user="root", ssh_key_path="/tmp/key"))
        session.add(OnlineLearnerAudit(
            cluster_key="cluster-a", host="node-1", metric="cpu", sample_id="s-evidence",
            observed_at=now.replace(tzinfo=None), value=42.0, label=None,
            quality_status="NO_LABEL", quality_reason="waiting", runtime_mode="AUDIT_ONLY",
            runtime_reason="initial audit", update_applied=False, model_version="river-mean-v1",
        ))
        session.add(NodeResourceForecastRun(
            id="run-evidence", cluster_name="CS-LAB", host="node-1", metric="cpu",
            algorithm="linear", window_hours=24, predicted_at=now.replace(tzinfo=None),
            target_at=now.replace(tzinfo=None), current_percent=42.0, predicted_percent=45.0,
            confidence=0.9, actual_percent=43.0, absolute_error=2.0, status="EVALUATED",
            idempotency_key="run-evidence-key", evaluated_at=now.replace(tzinfo=None),
        ))
        session.commit()
        assert enqueue_verified_outcomes(session, max_rows=10) == 0
        assert session.query(OnlineLearnerLabel).count() == 0
