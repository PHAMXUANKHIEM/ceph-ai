from datetime import datetime, timedelta

from config.settings import settings
import dashboard.routes.ai_learning as ai_learning_route
from shared import db
from shared.clusters import ensure_default_cluster
from shared.models import (
    Action, Incident,
    LogFaultStat,
    LogFinding,
    LogIngestRun,
    LogLearningSample,
    HostMetricSample,
    NodeResourceForecastAlert,
    NodeResourceForecastRun,
    NodeResourceForecastTransition,
    NodeResourceModelState,
    RemediationCase,
    VolumeEarlyForecast, VolumeForecastRun,
    VolumeModelState,
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


def test_ai_learning_model_mutations_require_admin(dashboard_client, monkeypatch):
    _login(dashboard_client)
    monkeypatch.setattr(ai_learning_route, "is_admin_user", lambda _user: False)

    assert dashboard_client.post(
        "/api/ai-learning/models/unknown/promote"
    ).status_code == 403
    assert dashboard_client.post(
        "/api/ai-learning/models/unknown/rollback"
    ).status_code == 403
    assert dashboard_client.post(
        "/api/ai-learning/models/unknown/block", data={"reason": "test"}
    ).status_code == 403


def test_ai_learning_controls_reject_a_different_selected_cluster(dashboard_client):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        selected_cluster_id = ensure_default_cluster(session).id

    response = dashboard_client.post(
        "/api/ai-learning/learner/pause",
        data={
            "cluster_id": "another-cluster-id",
            "host": "10.20.1.153",
            "metric": "cpu",
            "reason": "scope regression test",
        },
    )

    assert response.status_code == 409
    assert selected_cluster_id != "another-cluster-id"


def test_ai_learning_empty_state_is_readable(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/ai-learning")
    assert response.status_code == 200
    assert "AI đang học gì?" in response.text
    assert "Chưa có model CPU/RAM" in response.text
    assert "AUDIT_ONLY" in response.text


def test_ai_learning_reports_large_omap_readiness_without_mutating_state(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/api/ai-learning")

    assert response.status_code == 200
    readiness = response.json()["large_omap_readiness"]
    assert readiness["fault_family"] == "LARGE_OMAP_OBJECTS"
    assert readiness["ready_for_autonomy"] is False
    assert "blockers" in readiness
    page = dashboard_client.get("/ai-learning")
    assert "Readiness LARGE_OMAP_OBJECTS" in page.text
    assert "Commissioning report chỉ đọc" in page.text


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


def test_remediation_feedback_reports_cluster_precision_and_unlabeled_queue(dashboard_client):
    with db.SessionLocal() as session:
        cluster = ensure_default_cluster(session)
        incident = Incident(id="feedback-inc", cluster_id=cluster.id, ceph_code="OSD_DOWN", status="RESOLVED", detected_at=NOW)
        session.add(incident); session.flush()
        actions = [
            Action(incident_id=incident.id, action_id=f"feedback-{index}", classification="RISKY", status="EXECUTED")
            for index in range(3)
        ]
        session.add_all(actions); session.flush()
        cases = [
            RemediationCase(incident_id=incident.id, action_id=action.id, cluster_id=cluster.id,
                fault_family="OSD_DOWN", evidence_fingerprint=str(index) * 64,
                prompt_version="v1", classification="RISKY", autonomy_decision="PENDING_APPROVAL",
                outcome="VERIFIED_SUCCESS", operator_verdict=verdict,
                operator_verdict_at=NOW if verdict else None)
            for index, (action, verdict) in enumerate(zip(actions, ("CORRECT", "INEFFECTIVE", None)), 1)
        ]
        session.add_all(cases); session.commit(); cluster_id = cluster.id
    _login(dashboard_client)

    payload = dashboard_client.get(f"/api/ai-learning?cluster={cluster_id}").json()["remediation_feedback"]

    assert payload["total_cases"] == 3
    assert payload["labeled"] == 2
    assert payload["unlabeled"] == 1
    assert payload["precision_percent"] == 50.0
    assert payload["by_fault_family"][0]["precision_percent"] == 50.0
    page = dashboard_client.get(f"/ai-learning?cluster={cluster_id}")
    assert "precision AI remediation" in page.text
    assert "/incidents/feedback-inc/timeline" in page.text


def test_twenty_good_outcomes_are_marked_reliable(dashboard_client):
    _seed_learning()
    with db.SessionLocal() as session:
        state = session.query(NodeResourceModelState).one()
        state.evaluated_count = 20
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.get("/api/ai-learning")

    assert response.json()["resource_learning"]["selected_models"][0]["quality_status"] == "RELIABLE"


def test_ai_learning_shows_selected_volume_baseline(dashboard_client):
    with db.SessionLocal() as session:
        cluster = ensure_default_cluster(session)
        state = VolumeModelState(
            cluster_id=cluster.id, pool="vms", image="vm-101-disk",
            metric="read_latency_ms", algorithm="seasonal_median",
            window_hours=72, evaluated_count=12, mean_absolute_error=.4,
            mean_percentage_error=8.0, last_absolute_error=.2, selected=True,
            updated_at=NOW,
        )
        run = VolumeForecastRun(
            cluster_id=cluster.id, pool="vms", image="vm-101-disk",
            metric="read_latency_ms", algorithm="seasonal_median",
            window_hours=72, predicted_at=NOW, target_at=NOW + timedelta(hours=1),
            current_value=2.1, predicted_value=2.3, confidence=.85,
            seasonal_scope="hour_of_day", training_samples=8, status="PENDING",
            idempotency_key="volume-dashboard-test",
        )
        session.add_all((state, run))
        session.commit()
        cluster_id = cluster.id
    _login(dashboard_client)

    response = dashboard_client.get(f"/ai-learning?cluster={cluster_id}")

    assert response.status_code == 200
    assert "vms/vm-101-disk" in response.text
    assert "hour_of_day" in response.text
    assert "92.0%" in response.text
    assert "2.3" in response.text


def test_ai_learning_shows_auditable_volume_early_warning(dashboard_client):
    with db.SessionLocal() as session:
        cluster = ensure_default_cluster(session)
        session.add(VolumeEarlyForecast(
            cluster_id=cluster.id, pool="vms", image="hot-disk",
            metric="write_latency_ms", horizon_hours=6,
            generated_at=NOW, target_at=NOW + timedelta(hours=6),
            source_latest_at=NOW, current_value=12.0, predicted_value=24.0,
            threshold_type="latency_slo_ms", threshold_value=20.0,
            confidence=.84, training_samples=72, training_window_hours=72,
            seasonal_scope="hour_of_day", model_version="seasonal-trend-v1",
            status="WARNING", reason="Dự báo có thể chạm latency SLO trong 6 giờ.",
            idempotency_key="dashboard-early-warning",
        ))
        session.commit()
        cluster_id = cluster.id
    _login(dashboard_client)

    response = dashboard_client.get(f"/ai-learning?cluster={cluster_id}")

    assert response.status_code == 200
    assert "vms/hot-disk" in response.text
    assert "Cảnh báo sớm" in response.text
    assert "seasonal-trend-v1" in response.text
    payload = dashboard_client.get(f"/api/ai-learning?cluster={cluster_id}").json()
    assert payload["volume_learning"]["forecast_warning_count"] == 1


def test_ai_learning_shows_volume_data_quality_reason(dashboard_client):
    with db.SessionLocal() as session:
        cluster = ensure_default_cluster(session)
        session.add(VolumeEarlyForecast(
            cluster_id=cluster.id, pool="vms", image="sparse-disk",
            metric="write_latency_ms", horizon_hours=6,
            generated_at=NOW, target_at=NOW + timedelta(hours=6),
            source_latest_at=NOW - timedelta(hours=3), current_value=12.0,
            predicted_value=12.0, threshold_type=None, threshold_value=None,
            confidence=0.0, training_samples=18, training_window_hours=24,
            seasonal_scope="none", model_version="seasonal-trend-v1",
            status="DATA_QUALITY",
            reason="GAP_DETECTED: longest gap is 25200.0s; limit is 21600.0s",
            idempotency_key="dashboard-data-quality",
        ))
        session.commit()
        cluster_id = cluster.id
    _login(dashboard_client)

    response = dashboard_client.get(f"/ai-learning?cluster={cluster_id}")

    assert response.status_code == 200
    assert "DATA_QUALITY" in response.text
    assert "GAP_DETECTED" in response.text


def test_ai_learning_explains_node_alert_with_quality_and_transition_history(dashboard_client):
    with db.SessionLocal() as session:
        cluster = ensure_default_cluster(session)
        cluster.name = "CS-EXPLAIN"
        alert = NodeResourceForecastAlert(
            cluster_name=cluster.name, host="10.3.53.69", metric="cpu",
            status="OPEN", lifecycle_state="WARNING", notification_state="SENT",
            first_detected_at=NOW, last_detected_at=NOW,
            current_percent=82.0, predicted_percent=94.0, hours_to_90=4.0,
            confidence=.91, samples=48, window_hours=24,
            consensus_status="CONSENSUS", consensus_ratio=.75,
            consensus_candidate_count=4, predicted_low=89.0, predicted_high=97.0,
            coverage_ratio=.96, max_gap_hours=.5, latest_observed_at=datetime.utcnow(),
            state_changed_at=NOW, state_reason="Upper bound vượt ngưỡng cảnh báo.",
            evidence_version="forecast-consensus:v1",
        )
        session.add(alert)
        session.flush()
        session.add(NodeResourceForecastTransition(
            alert_id=alert.id, previous_state="CANDIDATE", new_state="WARNING",
            reason="Đủ breach liên tiếp để mở warning.",
            evidence_version="forecast-consensus:v1", changed_at=NOW,
        ))
        session.commit()
        cluster_id = cluster.id
    _login(dashboard_client)

    payload = dashboard_client.get(f"/api/ai-learning?cluster={cluster_id}").json()
    explanation = payload["node_alerts"][0]
    assert explanation["state_reason"] == "Upper bound vượt ngưỡng cảnh báo."
    assert explanation["consensus_candidate_count"] == 4
    assert explanation["coverage_ratio"] == .96
    assert explanation["freshness_status"] == "FRESH"
    assert explanation["transitions"][0]["new_state"] == "WARNING"

    page = dashboard_client.get(f"/ai-learning?cluster={cluster_id}")
    assert "Giải thích alert CPU/RAM" in page.text
    assert "Đủ breach liên tiếp để mở warning." in page.text


def test_ai_learning_replay_is_read_only_and_compares_models(dashboard_client):
    with db.SessionLocal() as session:
        cluster = ensure_default_cluster(session)
        for index in range(40):
            session.add(HostMetricSample(
                cluster_id=cluster.id, host="10.3.53.69", node_name="node-1",
                cpu_percent=30.0 + index * .2, mem_percent=40.0 + index * .1,
                disk_read_iops=10.0, disk_write_iops=8.0, disk_latency_ms=1.0,
                network_rx_bytes_per_sec=100.0, network_tx_bytes_per_sec=80.0,
                collected_at=NOW + timedelta(hours=index),
            ))
        session.commit()
        cluster_id = cluster.id
    _login(dashboard_client)

    response = dashboard_client.post("/api/ai-learning/replay", json={
        "cluster_id": cluster_id, "host": "10.3.53.69", "metric": "cpu",
        "start_at": NOW.isoformat(),
        "end_at": (NOW + timedelta(hours=39)).isoformat(),
        "horizon_hours": 1, "window_hours": [6, 12, 24],
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert payload["remediation_executed"] is False
    assert payload["metrics"]["linear"]["evaluated"] > 0
    assert payload["comparison"]["active_algorithm"] == "linear"
    assert "rolling_quantile" in payload["metrics"]
    with db.SessionLocal() as session:
        assert session.query(NodeResourceForecastRun).count() == 0
        assert session.query(NodeResourceForecastAlert).count() == 0
