from datetime import datetime, timedelta

from config.settings import settings
from shared import db
from shared.clusters import ensure_default_cluster
from shared.models import (
    LogFaultStat,
    LogFinding,
    LogIngestRun,
    LogLearningSample,
    NodeResourceForecastRun,
    NodeResourceModelState,
)


NOW = datetime(2026, 8, 25, 3, 0)


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _seed_learning():
    with db.SessionLocal() as session:
        cluster = ensure_default_cluster(session)
        cluster.name = "CS-LAB"
        state = NodeResourceModelState(
            cluster_name="CS-LAB", host="10.3.53.69", metric="cpu",
            algorithm="linear", window_hours=24, evaluated_count=4,
            mean_absolute_error=3.49, last_absolute_error=5.3, selected=True,
            updated_at=NOW,
        )
        forecast = NodeResourceForecastRun(
            cluster_name="CS-LAB", host="10.3.53.69", metric="cpu",
            algorithm="linear", window_hours=24, predicted_at=NOW,
            target_at=NOW + timedelta(hours=24), current_percent=22.9,
            predicted_percent=24.1, confidence=.81, status="PENDING",
            idempotency_key="cpu-24-test",
        )
        evaluated = NodeResourceForecastRun(
            cluster_name="CS-LAB", host="10.3.53.69", metric="cpu",
            algorithm="linear", window_hours=24, predicted_at=NOW - timedelta(days=1),
            target_at=NOW, current_percent=20, predicted_percent=25,
            actual_percent=22, absolute_error=3, confidence=.8, status="EVALUATED",
            idempotency_key="cpu-24-evaluated", evaluated_at=NOW,
        )
        run = LogIngestRun(
            cluster_id=cluster.id, source="loki", window_start=NOW - timedelta(hours=1),
            window_end=NOW, status="OK", hosts_scanned=1, hosts_failed=0,
            lines_scanned=10, patterns_seen=1, patterns_new=1, patterns_flagged=1,
        )
        session.add_all((state, forecast, evaluated, run))
        session.flush()
        finding = LogFinding(
            cluster_id=cluster.id, ingest_run_id=run.id, verdict="FINDING",
            title="OSD heartbeat chậm", dedupe_key="a" * 64, status="OPEN",
            fault_family="network_heartbeat",
        )
        session.add(finding)
        session.flush()
        sample = LogLearningSample(
            cluster_id=cluster.id, log_finding_id=finding.id, ingest_run_id=run.id,
            daemon_type="osd", host="10.3.53.69", fault_family="network_heartbeat",
            entity_key="host:10.3.53.69", evidence_fingerprint="b" * 64,
            source="loki", window_start=run.window_start, window_end=run.window_end,
            ingest_status="OK", parser_version="v1", semantic_version="v1",
            state="CORRELATED", label="UNVERIFIED", eligible_for_learning=False,
            exclusion_reason="awaiting Remediation Case", updated_at=NOW,
        )
        stat = LogFaultStat(
            cluster_id=cluster.id, daemon_type="osd", fault_family="network_heartbeat",
            playbook_id="restart_osd_daemon", playbook_version="v1",
            sample_count=5, verified_count=3, success_count=2, failure_count=1,
            inconclusive_count=0, trust_score=.62,
            promotion_blocked_reason="audit-only; autopilot disabled",
        )
        session.add_all((sample, stat))
        session.commit()
        return cluster.id


def test_ai_learning_requires_login(dashboard_client):
    response = dashboard_client.get("/ai-learning", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_ai_learning_empty_state_is_readable(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/ai-learning")
    assert response.status_code == 200
    assert "AI đang học gì?" in response.text
    assert "Chưa có model CPU/RAM" in response.text
    assert "AUDIT_ONLY" in response.text


def test_ai_learning_shows_resource_quality_and_log_blockers(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "node_resource_forecast_enabled", True)
    cluster_id = _seed_learning()
    _login(dashboard_client)

    response = dashboard_client.get(f"/ai-learning?cluster={cluster_id}")

    assert response.status_code == 200
    assert "10.3.53.69" in response.text
    assert "96.51%" in response.text
    assert "Tín hiệu tốt, còn ít mẫu" in response.text
    assert "OSD heartbeat chậm" in response.text
    assert "awaiting Remediation Case" in response.text
    assert "62.0%" in response.text


def test_ai_learning_api_returns_selected_model_and_audit_only_mode(dashboard_client):
    cluster_id = _seed_learning()
    _login(dashboard_client)

    response = dashboard_client.get(f"/api/ai-learning?cluster={cluster_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cluster_name"] == "CS-LAB"
    selected = payload["resource_learning"]["selected_models"]
    assert selected[0]["accuracy_estimate"] == 96.51
    assert selected[0]["quality_status"] == "PROMISING"
    assert payload["log_learning"]["mode"] == "AUDIT_ONLY"
    assert payload["log_learning"]["blocked_count"] == 1


def test_twenty_good_outcomes_are_marked_reliable(dashboard_client):
    _seed_learning()
    with db.SessionLocal() as session:
        state = session.query(NodeResourceModelState).one()
        state.evaluated_count = 20
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.get("/api/ai-learning")

    assert response.json()["resource_learning"]["selected_models"][0]["quality_status"] == "RELIABLE"
