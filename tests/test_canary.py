from datetime import datetime, timedelta

from shared import canary
from shared.models import Cluster, ForecastModelEvaluation, ForecastModelRegistry


def test_canary_report_is_read_only_and_fail_closed(db_session):
    cluster = Cluster(
        name="canary-cluster", ceph_mon_nodes="10.0.0.1", ssh_user="root",
        ssh_key_path="/key", is_active=True,
    )
    db_session.add(cluster)
    db_session.commit()

    report = canary.build_canary_report(
        db_session, cluster_id=cluster.id, cluster_name=cluster.name,
    )

    assert report["read_only"] is True
    assert report["candidate_count"] == 0
    assert report["operator_approval_required"] is True
    assert report["auto_promotion"] is False
    assert report["remediation_executed"] is False


def test_canary_report_summarizes_shadow_evidence_without_promoting(db_session):
    cluster = Cluster(
        name="canary-cluster-2", ceph_mon_nodes="10.0.0.1", ssh_user="root",
        ssh_key_path="/key", is_active=True,
    )
    db_session.add(cluster)
    db_session.flush()
    scope = f"{cluster.name}|10.0.0.1|cpu"
    active = ForecastModelRegistry(
        scope_type="NODE_RESOURCE", scope_key=scope, name="node-resource-forecast",
        version="linear:24h", algorithm="linear", feature_schema="node-resource-v1",
        training_window_hours=24, status="ACTIVE",
    )
    candidate = ForecastModelRegistry(
        scope_type="NODE_RESOURCE", scope_key=scope, name="node-resource-forecast",
        version="rolling_quantile:24h", algorithm="rolling_quantile",
        feature_schema="node-resource-v1", training_window_hours=24, status="SHADOW",
    )
    db_session.add_all([active, candidate])
    db_session.flush()
    db_session.add(ForecastModelEvaluation(
        candidate_model_id=candidate.id, active_model_id=active.id,
        target_at=datetime.utcnow() - timedelta(hours=1),
        active_evaluated=2, candidate_evaluated=2,
        active_mae=10.0, candidate_mae=8.0,
        active_rmse=11.0, candidate_rmse=9.0,
        active_smape=12.0, candidate_smape=10.0,
        active_bias=1.0, candidate_bias=0.5,
        active_false_positive_rate=0.1, candidate_false_positive_rate=0.1,
        status="OK", reason="shadow evidence",
    ))
    db_session.commit()

    report = canary.build_canary_report(
        db_session, cluster_id=cluster.id, cluster_name=cluster.name,
    )
    scope_report = report["scopes"][0]
    assert report["candidate_count"] == 1
    assert scope_report["comparison"]["evaluation_count"] == 1
    assert scope_report["comparison"]["candidate_mae"] == 8.0
    assert scope_report["comparison"]["promotion_guard"]["allowed"] is False
    assert db_session.query(ForecastModelRegistry).filter_by(status="ACTIVE").count() == 1
