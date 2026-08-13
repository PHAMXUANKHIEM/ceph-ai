import json
from datetime import datetime

from shared import db as db_module
from shared.models import VitastorCluster, VitastorDiagnosticRun, VitastorMetricSample, VitastorOsdMetricSample
import dashboard.routes.vitastor as route


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin", "product": "vitastor"})


def _cluster(last_status_json=None, name="vita-lab"):
    with db_module.SessionLocal() as session:
        cluster = VitastorCluster(
            name=name, management_host="10.0.0.20", etcd_address="etcd:2379",
            etcd_prefix="/vitastor", config_path="", ssh_user="root",
            ssh_key_path="/key", exec_mode="none", container_name="",
            created_by="admin", last_status_json=last_status_json,
        )
        session.add(cluster)
        session.commit()
        return cluster.id


def _datasets():
    return {
        "status": {
            "etcd_alive": 1, "etcd_count": 1, "mon_count": 1,
            "osd_up": 2, "osd_count": 2, "total_raw": 1000, "free_raw": 250,
            "pool_count": 1, "active_pool_count": 1, "pg_states": {"active": 16},
            "op_stats": {"read": {"iops": 120, "bps": 4096, "latency_us": 800}, "write": {"iops": 80, "bps": 2048, "latency_us": 1200}},
        },
        "pools": [{"id": 1, "name": "pool-a"}],
        "osds": [{"type": "osd", "name": "1", "parent": "storage-1", "up": True}],
        "images": [{"name": "volume-a", "pool_name": "pool-a"}], "errors": {},
    }


def test_dashboard_page_contains_complete_monitor_sections(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/vitastor")
    assert response.status_code == 200
    assert "PG state distribution" in response.text
    assert "OSD inventory" in response.text
    assert "Images / volumes" in response.text
    assert "Servers" in response.text
    assert "Latency" in response.text
    assert "Bandwidth" in response.text
    assert "IOPS" in response.text
    assert "Performance history" in response.text


def test_overview_api_normalizes_and_caches_live_data(dashboard_client, monkeypatch):
    _login(dashboard_client)
    cluster_id = _cluster()
    monkeypatch.setattr(route, "query_dashboard", lambda *_: _datasets())

    response = dashboard_client.get(f"/vitastor/api/overview?cluster_id={cluster_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["health"] == "HEALTHY"
    assert payload["summary"]["capacity"]["used"] == 750
    assert payload["summary"]["servers"] == {"online": 1, "total": 1}
    assert payload["summary"]["metrics"] == {"latency_ms": 1.0, "bandwidth_bps": 6144.0, "iops": 200.0}
    assert payload["summary"]["placement_groups"] == "ACTIVE"
    assert payload["images"][0]["name"] == "volume-a"
    with db_module.SessionLocal() as session:
        cached = json.loads(session.get(VitastorCluster, cluster_id).last_status_json)
        assert cached["summary"]["capacity"]["used"] == 750


def test_overview_api_uses_legacy_status_cache_when_cluster_is_offline(dashboard_client, monkeypatch):
    _login(dashboard_client)
    cluster_id = _cluster(json.dumps(_datasets()["status"]))
    def fail(*_): raise route.VitastorConnectionError("SSH timeout")
    monkeypatch.setattr(route, "query_dashboard", fail)

    response = dashboard_client.get(f"/vitastor/api/overview?cluster_id={cluster_id}")

    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert response.json()["summary"]["osds"] == {"up": 2, "total": 2, "full": 0, "nearfull": 0, "primary_slow": [], "secondary_slow": []}


def test_metric_history_is_scoped_to_selected_cluster(dashboard_client):
    _login(dashboard_client)
    cluster_id = _cluster()
    other_id = _cluster(name="vita-other")
    with db_module.SessionLocal() as session:
        session.add(VitastorMetricSample(
            cluster_id=cluster_id, health="HEALTHY", osd_up=1, osd_total=1,
            used_bytes=10, free_bytes=90, used_percent=10, etcd_up=1, etcd_total=1,
            etcd_leader_count=1, read_iops=2, write_iops=3, read_bps=20, write_bps=30,
            recovery_bps=0, degraded_bytes=0, raw_json="{}", collected_at=datetime.utcnow(),
        ))
        session.add(VitastorOsdMetricSample(
            cluster_id=cluster_id, osd_id="1", host="node-a", is_up=True,
            size_bytes=100, used_bytes=10, used_percent=10, read_iops=2, write_iops=3,
            read_bps=20, write_bps=30, raw_json="{}", collected_at=datetime.utcnow(),
        ))
        session.add(VitastorMetricSample(
            cluster_id=other_id, health="CRITICAL", osd_up=0, osd_total=1,
            used_bytes=0, free_bytes=0, used_percent=0, etcd_up=0, etcd_total=1,
            etcd_leader_count=0, read_iops=0, write_iops=0, read_bps=0, write_bps=0,
            recovery_bps=0, degraded_bytes=0, raw_json="{}", collected_at=datetime.utcnow(),
        ))
        session.commit()
    payload = dashboard_client.get(f"/vitastor/api/metrics/history?cluster_id={cluster_id}&hours=24").json()
    assert len(payload["points"]) == 1 and payload["points"][0]["health"] == "HEALTHY"
    assert payload["osds"][0]["osd_id"] == "1"


def test_ai_diagnosis_collects_read_only_evidence_and_persists_result(dashboard_client, monkeypatch):
    _login(dashboard_client); cluster_id = _cluster()
    monkeypatch.setattr(route, "query_dashboard", lambda *_: _datasets())
    captured = {}
    async def fake_diagnose(name, evidence):
        captured.update({"name": name, "evidence": evidence})
        result = {"root_cause": "OSD unavailable", "impact": "Reduced redundancy", "confidence": "high", "evidence": ["OSD count"], "recommended_steps": ["Inspect logs"], "commands_preview": ["vitastor-cli osd-tree -l"], "safety_notes": ["Không tự xoá OSD"]}
        return json.dumps(result), result
    monkeypatch.setattr(route, "diagnose", fake_diagnose)
    response = dashboard_client.post("/vitastor/api/diagnostics", data={"cluster_id": cluster_id})
    assert response.status_code == 200
    assert response.json()["diagnostic"]["result"]["confidence"] == "high"
    assert "ssh_key_path" not in json.dumps(captured["evidence"])
    with db_module.SessionLocal() as session:
        assert session.query(VitastorDiagnosticRun).one().status == "COMPLETED"


def test_ai_failure_keeps_evidence_and_failed_state(dashboard_client, monkeypatch):
    _login(dashboard_client); cluster_id = _cluster()
    monkeypatch.setattr(route, "query_dashboard", lambda *_: _datasets())
    async def fail(*_): raise RuntimeError("provider unavailable")
    monkeypatch.setattr(route, "diagnose", fail)
    response = dashboard_client.post("/vitastor/api/diagnostics", data={"cluster_id": cluster_id})
    assert response.status_code == 502
    with db_module.SessionLocal() as session:
        row = session.query(VitastorDiagnosticRun).one()
        assert row.status == "FAILED"
        assert json.loads(row.evidence_json)["status"]["osd_count"] == 2
