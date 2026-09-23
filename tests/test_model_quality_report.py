from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager

from shared import db
from shared.models import ForecastModelEvaluation, ForecastModelRegistry
from shared.model_quality_report import quality_report


def _model(cluster_id, status, version):
    return ForecastModelRegistry(
        cluster_id=cluster_id, scope_type="NODE_RESOURCE",
        scope_key=f"{cluster_id}|node-1|cpu|h1", scope_schema="v2",
        entity_type="host", entity_id="node-1", host="node-1", metric="cpu",
        horizon_hours=1, name="linear", version=version, algorithm="linear",
        feature_schema="resource-v2", training_window_hours=24, status=status,
    )


def test_quality_report_is_scoped_and_windowed(default_cluster_id, db_session, monkeypatch):
    from shared.models import Cluster

    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    other = Cluster(name="quality-other", ceph_mon_nodes="10.0.0.2",
                    ssh_user="root", ssh_key_path="/tmp/key")
    db_session.add(other)
    db_session.flush()
    active = _model(default_cluster_id, "ACTIVE", "v1")
    candidate = _model(default_cluster_id, "SHADOW", "v2")
    foreign = _model(other.id, "SHADOW", "v3")
    db_session.add_all([active, candidate, foreign])
    db_session.flush()
    for age, mae in ((2, 4.0), (12, 6.0)):
        db_session.add(ForecastModelEvaluation(
            candidate_model_id=candidate.id, active_model_id=active.id,
            target_at=(now - timedelta(hours=age)).replace(tzinfo=None),
            evaluated_at=(now - timedelta(hours=age)).replace(tzinfo=None),
            active_evaluated=10, candidate_evaluated=10, active_mae=5.0,
            candidate_mae=mae, active_smape=10.0, candidate_smape=9.0,
            status="HOLD", reason="shadow evidence",
        ))
    db_session.commit()

    @contextmanager
    def same_session():
        yield db_session

    monkeypatch.setattr(db, "SessionLocal", same_session)

    short = quality_report(default_cluster_id, hours=6, now=now)
    day = quality_report(default_cluster_id, hours=24, now=now)
    assert short["evaluation_count"] == 1
    assert day["evaluation_count"] == 2
    candidate_item = next(item for item in short["models"] if item["version"] == "v2")
    assert candidate_item["candidate_mae"] == 4.0
    assert candidate_item["horizon_hours"] == 1
    assert all(item["cluster_id"] == default_cluster_id for item in day["models"])
    assert all(item["version"] != "v3" for item in day["models"])


def test_quality_report_endpoint_rejects_other_windows(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    assert dashboard_client.get("/api/ai-learning/model-quality-report?hours=12").status_code == 400
    assert dashboard_client.get("/api/ai-learning/model-quality-report?hours=6").status_code == 200


def test_learning_page_contains_lazy_quality_panel():
    root = Path(__file__).resolve().parents[1]
    page = (root / "dashboard/templates/ai_learning.html").read_text()
    assert 'id="model-quality-report"' in page
    assert "/static/model_quality_report.js" in page
