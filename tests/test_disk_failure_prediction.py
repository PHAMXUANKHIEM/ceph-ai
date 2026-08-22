import json
from datetime import datetime, timedelta, timezone

from shared import db
from shared.models import Action, CephCapacitySample, CrushOsdDistribution, Incident
from watcher.disk_failure_prediction import predict


def test_disk_risk_combines_device_latency_and_restart_evidence(dashboard_client, default_cluster_id):
    now = datetime.utcnow()
    life_min = (now.replace(tzinfo=timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.%f%z")
    with db.SessionLocal() as session:
        session.add_all([
            CrushOsdDistribution(cluster_id=default_cluster_id, osd_id=0, host="host-a", bytes_used=50, bytes_total=100, pgs=10, updated_at=now),
            CrushOsdDistribution(cluster_id=default_cluster_id, osd_id=1, host="host-b", bytes_used=40, bytes_total=100, pgs=10, updated_at=now),
        ])
        device = Incident(
            id="device-1", cluster_id=default_cluster_id, ceph_code="DEVICE_HEALTH_EVACUATE:0",
            status="PENDING_APPROVAL", severity="HEALTH_WARN", detected_at=now,
            signal_evidence_json=json.dumps({"osd_id": 0, "life_expectancy_min": life_min}),
        )
        latency = Incident(
            id="latency-1", cluster_id=default_cluster_id, ceph_code="OSD_LATENCY_HIGH:0",
            status="NEW", severity="HEALTH_WARN", detected_at=now,
            signal_evidence_json=json.dumps({"osd_id": 0, "commit_latency_ms": 42}),
        )
        session.add_all([device, latency])
        session.flush()
        session.add(Action(
            incident_id=latency.id, action_id="restart_osd_daemon", classification="RISKY",
            status="EXECUTED", rationale="test", created_at=now,
        ))
        session.commit()

    result = predict(default_cluster_id, now=now)
    risky = next(row for row in result["predictions"] if row["osd_id"] == 0)
    quiet = next(row for row in result["predictions"] if row["osd_id"] == 1)
    assert risky["risk_score"] == 100
    assert risky["risk_level"] == "CRITICAL"
    assert len(risky["signals"]) == 3
    assert risky["smart_metrics_available"] is True
    assert quiet["risk_score"] == 0
    assert quiet["confidence"] == .45
    assert "not proof" in quiet["recommendation"]
    assert {row["source_id"] for row in result["_citations"]} >= {
        "incident:device-1", "incident:latency-1",
    }

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    page = dashboard_client.get("/disk-risk")
    assert page.status_code == 200
    assert "Disk Failure Prediction" in page.text
    assert "100/100 · CRITICAL" in page.text
    api = dashboard_client.get("/api/disk-risk")
    assert api.status_code == 200
    assert api.json()["high_risk_count"] == 1


def test_disk_risk_is_unavailable_without_osd_inventory(dashboard_client, default_cluster_id):
    result = predict(default_cluster_id)
    assert result["status"] == "unavailable"
    assert result["predictions"] == []
