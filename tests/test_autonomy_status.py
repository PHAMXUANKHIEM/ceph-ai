from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import autonomy_status
from shared.db import Base
from shared.models import Cluster, OnlineLearnerLabel

NOW = datetime(2026, 10, 5, 9, 0)


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _cluster(session, cluster_id="cluster-a", default=True):
    cluster = Cluster(id=cluster_id, name="CS-LAB", ceph_mon_nodes="", ssh_user="root",
                      ssh_key_path="/tmp/key", is_default=default)
    session.add(cluster)
    session.commit()
    return cluster


def _label(session, index, cluster_key="cluster-a", status="CONSUMED"):
    session.add(OnlineLearnerLabel(
        cluster_key=cluster_key, host="h1", metric="cpu", sample_id=f"s{index}", source_run_id=f"r{index}",
        observed_at=NOW, label_value=40.0, outcome="VERIFIED_SUCCESS", status=status, reason="test",
        verified_at=NOW, evidence_fingerprint=f"f{index}", source_model_version="linear:w24:h6",
        outcome_observed_at=NOW,
    ))


def test_status_combines_every_section_for_one_cluster():
    session = _session()
    cluster = _cluster(session)
    _label(session, 1)
    _label(session, 2, status="READY")
    _label(session, 3, cluster_key="other-cluster")
    session.commit()

    report = autonomy_status.build(session, cluster, now=NOW)

    assert report["errors"] == {}
    assert report["online_learning"]["verified"] == 2
    assert report["online_learning"]["scored"] == 1
    assert report["online_learning"]["decision"] == "KEEP_SHADOW"
    assert report["evidence"]["incidents_investigated"] == 0
    assert report["decisions"]["by_source"] == {}
    assert report["false_release_rate"] is None
    assert report["ope"]["decisions_with_reward"] == 0


def test_a_failing_section_is_reported_and_the_rest_survives(monkeypatch):
    session = _session()
    cluster = _cluster(session)

    def boom(*_args, **_kwargs):
        raise RuntimeError("table missing")

    monkeypatch.setattr(autonomy_status.river_v2_evidence, "build_report", boom)

    report = autonomy_status.build(session, cluster, now=NOW)

    assert report["online_learning"] is None
    assert report["errors"] == {"online_learning": "RuntimeError"}
    assert report["evidence"] is not None


def test_api_requires_admin_and_validates_the_window(dashboard_client):
    anonymous = dashboard_client.get("/api/ai-learning/autonomy-status", follow_redirects=False)
    assert anonymous.status_code in {303, 401}
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get("/api/ai-learning/autonomy-status?days=7")

    assert response.status_code == 200
    assert response.json()["period_days"] == 7
    assert dashboard_client.get("/api/ai-learning/autonomy-status?days=0").status_code == 400


def test_learning_page_loads_the_autonomy_status_card():
    root = Path(__file__).resolve().parents[1]
    page = (root / "dashboard/templates/ai_learning.html").read_text(encoding="utf-8")
    assert 'id="autonomy-status"' in page
    assert "/static/autonomy_status.js" in page
    assert (root / "dashboard/static/autonomy_status.js").is_file()
