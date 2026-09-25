from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db
from shared.db import Base
from shared.models import (
    Cluster,
    ForecastModelPromotionAudit,
    ForecastModelRegistry,
    NodeResourceForecastRun,
    OnlineLearnerAudit,
    OnlineLearnerCycleAudit,
    OnlineLearnerLabel,
    WatcherHeartbeat,
)
from shared.online_learning_consumer import consume_sample, consume_samples
from shared.online_learning_controls import PAUSED, set_status
from shared.online_learning_labels import enqueue_verified_outcomes
import shared.online_learning_consumer as consumer_module


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
        assert audit.backend_name == "river"
        assert audit.backend_version == "0.25.0"
        assert audit.feature_schema == "scalar-v1"


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


def test_consumer_never_targets_active_model(monkeypatch):
    _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_enabled", True)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_require_verified_label", False)
    targets = []
    original = consumer_module.guarded_update

    def capture_target(*args, **kwargs):
        targets.append(kwargs.get("target", "shadow"))
        return original(*args, **kwargs)

    monkeypatch.setattr(consumer_module, "guarded_update", capture_target)
    now = datetime.now(timezone.utc)
    with consumer_module.db.SessionLocal() as session:
        session.add(Cluster(id="cluster-shadow", name="CS-LAB", ceph_mon_nodes="", ssh_user="root", ssh_key_path="/tmp/key"))
        session.commit()

    result = consumer_module.consume_sample(
        cluster_id="cluster-shadow", host="node-1", metric="cpu", value=42.0,
        observed_at=now, sample_id="shadow-target-only", label=42.0,
    )

    assert result is not None
    assert targets == ["shadow"]


def test_consumer_targets_active_only_after_registry_promotion_audit(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_enabled", True)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_mode", "ACTIVE")
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_require_verified_label", False)
    now = datetime.now(timezone.utc)
    with factory() as session:
        session.add(Cluster(
            id="cluster-active", name="CS-LAB", ceph_mon_nodes="",
            ssh_user="root", ssh_key_path="/tmp/key",
        ))
        session.add(WatcherHeartbeat(
            id=1, cluster_id="cluster-active", success=True, mon_node="mon-1",
            error_message=None, polled_at=now, consecutive_failures=0,
            last_success_at=now,
        ))
        model = ForecastModelRegistry(
            scope_type="NODE_RESOURCE", scope_key="cluster-active|node-1|cpu",
            scope_schema="forecast-scope-v2", cluster_id="cluster-active",
            entity_type="node", entity_id="node-1", host="node-1", metric="cpu",
            horizon_hours=24, name="online-learner", version="river-mean-v1",
            algorithm="river_mean", feature_schema="scalar-v1",
            training_window_hours=24, status="ACTIVE",
        )
        session.add(model)
        session.flush()
        session.add(ForecastModelPromotionAudit(
            candidate_model_id=model.id, event_type="PROMOTED", actor="operator",
            reason="guarded promotion approved",
        ))
        session.commit()

    targets = []
    original = consumer_module.guarded_update

    def capture_target(*args, **kwargs):
        targets.append(kwargs.get("target"))
        return original(*args, **kwargs)

    monkeypatch.setattr(consumer_module, "guarded_update", capture_target)
    result = consumer_module.consume_sample(
        cluster_id="cluster-active", host="node-1", metric="cpu", value=42.0,
        observed_at=now, sample_id="active-target", label=42.0,
    )

    assert result is not None
    assert result.update_applied is True
    assert targets == ["active"]
    with factory() as session:
        audit = session.query(OnlineLearnerAudit).filter_by(sample_id="active-target").one()
        assert "target=active" in audit.runtime_reason


def test_cycle_audit_records_backend_identity(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_enabled", True)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_require_verified_label", True)
    now = datetime.now(timezone.utc)

    consume_sample(
        cluster_id="cluster-cycle", host="node-1", metric="cpu", value=42.0,
        observed_at=now, sample_id="cycle-backend", label=None,
    )

    with factory() as session:
        cycle = session.scalar(select(OnlineLearnerCycleAudit))
        assert cycle is not None
        assert cycle.backend_name == "river"
        assert cycle.backend_version == "0.25.0"
        assert cycle.feature_schema == "scalar-v1"


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
            observed_at=now.replace(tzinfo=None), value=43.0, label=None,
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
        cluster_id="cluster-a", host="node-1", metric="cpu", value=43.0,
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
        assert len(label.evidence_fingerprint) == 64
        assert label.source_model_version == "linear:w24:h24"
        assert label.outcome_observed_at is not None


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


def test_verified_outcome_rejects_mismatch_self_label_and_duplicates(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_labels.settings.online_learning_min_verified_evidence", 1)
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    with factory() as session:
        session.add(Cluster(id="cluster-a", name="CS-LAB", ceph_mon_nodes="", ssh_user="root", ssh_key_path="/tmp/key"))
        for suffix, audit_value, old_label in (
            ("ok", 43.0, None), ("mismatch", 44.0, None),
            ("self", 43.0, 43.0),
        ):
            session.add(OnlineLearnerAudit(
                cluster_key="cluster-a", host=f"node-{suffix}", metric="cpu",
                sample_id=f"sample-{suffix}", observed_at=now,
                value=audit_value, label=old_label, quality_status="NO_LABEL",
                quality_reason="waiting", runtime_mode="AUDIT_ONLY",
                runtime_reason="waiting", update_applied=False,
                model_version="river-mean-v1",
            ))
            session.add(NodeResourceForecastRun(
                id=f"run-{suffix}", cluster_name="CS-LAB", host=f"node-{suffix}",
                metric="cpu", algorithm="linear", window_hours=24,
                predicted_at=now, target_at=now, current_percent=42.0,
                predicted_percent=45.0, confidence=0.9, actual_percent=43.0,
                absolute_error=2.0, status="EVALUATED",
                idempotency_key=f"run-{suffix}", evaluated_at=now,
            ))
        session.commit()
        assert enqueue_verified_outcomes(session, max_rows=10, source_run_ids=[]) == 0
        assert enqueue_verified_outcomes(session, max_rows=10, source_run_ids=["run-ok"]) == 1
        session.commit()
        assert enqueue_verified_outcomes(
            session, max_rows=10, source_run_ids=["run-mismatch", "run-self", "run-ok"],
        ) == 0
        assert session.query(OnlineLearnerLabel).count() == 1
        assert session.query(OnlineLearnerLabel).one().source_run_id == "run-ok"


def test_verified_outcome_rejects_missing_audit_and_stale_run(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_labels.settings.online_learning_min_verified_evidence", 1)
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    with factory() as session:
        session.add(Cluster(id="cluster-a", name="CS-LAB", ceph_mon_nodes="", ssh_user="root", ssh_key_path="/tmp/key"))
        for suffix, target in (("missing", now), ("stale", now.replace(year=now.year - 1))):
            session.add(NodeResourceForecastRun(
                id=f"run-{suffix}", cluster_name="CS-LAB", host=f"node-{suffix}",
                metric="cpu", algorithm="linear", window_hours=24,
                predicted_at=target, target_at=target, current_percent=42.0,
                predicted_percent=45.0, confidence=0.9, actual_percent=43.0,
                absolute_error=2.0, status="EVALUATED",
                idempotency_key=f"run-{suffix}", evaluated_at=target,
            ))
        session.commit()
        assert enqueue_verified_outcomes(session, max_rows=10) == 0
        assert session.query(OnlineLearnerLabel).count() == 0


def test_control_database_failure_fails_closed(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("shared.online_learning_consumer.settings.online_learning_enabled", True)

    def fail_closed_control(*args, **kwargs):
        raise RuntimeError("control database unavailable")

    monkeypatch.setattr(
        "shared.online_learning_consumer.get_control",
        fail_closed_control,
    )
    result = consume_sample(
        cluster_id="cluster-a",
        host="node-1",
        metric="cpu",
        value=42.0,
        observed_at=datetime.now(timezone.utc),
        sample_id="db-failure",
        label=42.0,
    )

    assert result is None
    with factory() as session:
        assert session.query(OnlineLearnerAudit).filter_by(sample_id="db-failure").count() == 0
        cycle = session.query(OnlineLearnerCycleAudit).order_by(OnlineLearnerCycleAudit.created_at.desc()).first()
        assert cycle is not None
        assert cycle.failed == 1
