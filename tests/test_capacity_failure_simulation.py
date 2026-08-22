import json
from datetime import datetime

from shared import db
from shared.models import CephCapacitySample, CrushOsdDistribution, CrushStructureSnapshot
from watcher.capacity_failure_simulation import simulate


def _tree():
    return {"roots": [{"type": "root", "name": "default", "children": [{
        "type": "rack", "name": "rack-a", "children": [
            {"type": "host", "name": "host-a", "children": [
                {"type": "osd", "name": "osd.0", "id": 0},
                {"type": "osd", "name": "osd.1", "id": 1},
            ]},
            {"type": "host", "name": "host-b", "children": [
                {"type": "osd", "name": "osd.2", "id": 2},
            ]},
        ],
    }]}]}


def test_simulates_osd_host_and_catastrophic_rack_loss(dashboard_client, default_cluster_id):
    now = datetime(2026, 8, 22, 12, 0)
    with db.SessionLocal() as session:
        session.add(CrushStructureSnapshot(
            id="snap-1", cluster_id=default_cluster_id, tree_json=json.dumps(_tree()), created_at=now,
        ))
        session.add_all([
            CrushOsdDistribution(cluster_id=default_cluster_id, osd_id=0, host="host-a", bytes_used=80, bytes_total=100, pgs=10, updated_at=now),
            CrushOsdDistribution(cluster_id=default_cluster_id, osd_id=1, host="host-a", bytes_used=20, bytes_total=100, pgs=10, updated_at=now),
            CrushOsdDistribution(cluster_id=default_cluster_id, osd_id=2, host="host-b", bytes_used=20, bytes_total=100, pgs=10, updated_at=now),
            CephCapacitySample(cluster_id=default_cluster_id, entity_type="pool", entity_name="volumes", used_bytes=75, total_bytes=100, used_percent=75, captured_at=now),
        ])
        session.commit()

    result = simulate(default_cluster_id)
    assert result["status"] == "ready"
    assert result["scenario_count"] == 6
    host = next(row for row in result["scenarios"] if row["domain_type"] == "host" and row["domain_name"] == "host-a")
    assert host["cluster_projected_percent"] == 120
    assert host["max_osd_projected_percent"] == 120
    assert host["additional_bytes_for_80_percent"] == 50
    assert host["pools_at_risk"][0]["pool"] == "volumes"
    rack = next(row for row in result["scenarios"] if row["domain_type"] == "rack")
    assert rack["catastrophic"] is True
    assert rack["remaining_capacity_bytes"] == 0
    assert result["data_availability_verified"] is False
    assert result["_citations"][0]["source_id"] == "crush-snapshot:snap-1"

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    page = dashboard_client.get("/capacity-forecast")
    assert page.status_code == 200
    assert "Mô phỏng mất OSD / failure domain" in page.text
    assert "host: host-a" in page.text
    assert "crush-snapshot:snap-1" in page.text


def test_returns_unavailable_without_crush_data(dashboard_client, default_cluster_id):
    assert simulate(default_cluster_id)["status"] == "unavailable"
