from datetime import datetime, timedelta, timezone

import dashboard.routes.performance_rca as routes
import dashboard.routes.volumes as volumes_routes
from shared import db
from shared.models import Cluster
from watcher.block_storage_diagnosis import build_diagnosis


NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


def _report(at: datetime, *, cluster_id: str = "cluster-a", image: str = "vm-a") -> dict:
    return {
        "cluster_id": cluster_id,
        "analyses": [{
            "pool": "rbd", "image": image, "observed_at": at.isoformat(),
            "samples": 4, "current_latency_ms": 30, "baseline_latency_ms": 10,
            "iops": 100, "confidence": 0.9,
            "signals": {"volume": {"status": "elevated"}, "host": {"status": "not_available"}},
            "investigation_steps": [{"step": "Check workload", "read_only": True}],
        }],
        "_citations": [{"source_id": "volume_metrics", "row_count": 4}],
        "evidence_gaps": ["No live OSD metrics"],
    }


def test_diagnosis_reports_a_bounded_candidate_without_claiming_causality():
    result = build_diagnosis(_report(NOW - timedelta(minutes=2)), pool="rbd", image="vm-a", now=NOW)
    assert result["status"] == "candidate"
    assert result["hypothesis"] == "unattributed_volume_latency"
    assert result["confidence"] <= 0.35
    assert result["ai_generated"] is False
    assert result["action_id"] is None and result["read_only"] is True
    assert result["next_checks"] == ["Check workload"]


def test_stale_and_wrong_target_fail_closed():
    stale = build_diagnosis(_report(NOW - timedelta(minutes=16)), pool="rbd", image="vm-a", now=NOW)
    wrong = build_diagnosis(_report(NOW), pool="rbd", image="vm-b", now=NOW)
    assert stale["status"] == "insufficient_evidence" and stale["hypothesis"] is None
    assert stale["stale"] is True
    assert wrong["status"] == "insufficient_evidence" and wrong["signals"]["sample_count"] == 0


def test_future_timestamp_fails_closed_and_nonfinite_values_are_not_serialized():
    future = build_diagnosis(_report(NOW + timedelta(minutes=1)), pool="rbd", image="vm-a", now=NOW)
    assert future["status"] == "insufficient_evidence" and future["stale"] is True
    report = _report(NOW)
    report["analyses"][0]["current_latency_ms"] = float("nan")
    report["analyses"][0]["iops"] = float("inf")
    report["analyses"][0]["confidence"] = float("nan")
    result = build_diagnosis(report, pool="rbd", image="vm-a", now=NOW)
    assert result["signals"]["current_latency_ms"] is None
    assert result["signals"]["iops"] is None
    assert result["confidence"] == 0


def test_diagnosis_endpoint_is_authenticated_scoped_and_never_collects_live(dashboard_client, monkeypatch):
    calls = []

    def fake_report(_session, cluster_id, **kwargs):
        calls.append((cluster_id, kwargs))
        return _report(NOW, cluster_id=cluster_id)

    monkeypatch.setattr(routes, "build_report", fake_report)
    monkeypatch.setattr(
        "watcher.performance_rca.collect_live_osd_signals",
        lambda _cluster: (_ for _ in ()).throw(AssertionError("live SSH was requested")),
    )
    path = "/api/performance-rca/diagnosis?pool=rbd&image=vm-a"
    unauthenticated = dashboard_client.get(path, follow_redirects=False)
    assert unauthenticated.status_code in {302, 303, 401}
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get(path)
    assert response.status_code == 200
    assert response.json()["cluster_id"] == calls[0][0]
    assert calls[0][1]["live_signals"] == {"status": "unavailable"}
    assert calls[0][1]["pool"] == "rbd" and calls[0][1]["image"] == "vm-a"


def test_diagnosis_endpoint_rejects_oversized_scope(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/performance-rca/diagnosis", params={"pool": "x" * 65, "image": "vm-a"})
    assert response.status_code == 422


def test_diagnosis_endpoint_isolates_secondary_cluster_and_rejects_invalid_selection(dashboard_client, monkeypatch):
    with db.SessionLocal() as session:
        secondary = Cluster(
            name="diagnosis-secondary", ceph_mon_nodes="10.2.0.2",
            ceph_container_name="mon", ssh_user="ceph", ssh_key_path="/test/key",
            ceph_exec_mode="cephadm", is_default=False, is_active=True,
        )
        session.add(secondary)
        session.commit()
        secondary_id = secondary.id
    calls = []

    def fake_report(_session, cluster_id, **kwargs):
        calls.append(cluster_id)
        return _report(NOW, cluster_id=cluster_id)

    monkeypatch.setattr(routes, "build_report", fake_report)
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    path = "/api/performance-rca/diagnosis"
    response = dashboard_client.get(path, params={"cluster": secondary_id, "pool": "rbd", "image": "vm-a"})
    assert response.status_code == 200
    assert response.json()["cluster_id"] == secondary_id
    assert calls == [secondary_id]
    invalid = dashboard_client.get(path, params={"cluster": "no-such-cluster", "pool": "rbd", "image": "vm-a"})
    assert invalid.status_code == 404
    assert calls == [secondary_id]


def test_volume_detail_exposes_on_demand_diagnosis_without_a_write_control(dashboard_client, monkeypatch):
    monkeypatch.setattr(volumes_routes, "_rbd_pools_for_request", lambda _request: ["rbd"])
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/volumes/rbd/vm-a")
    assert response.status_code == 200
    assert 'id="detail-diagnosis-load"' in response.text
    assert "Phân tích dữ liệu đã lưu" in response.text
    assert "không phải kết luận AI hay hành động tự động" in response.text
