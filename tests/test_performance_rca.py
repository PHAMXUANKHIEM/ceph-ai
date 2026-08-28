from datetime import datetime, timedelta

from shared.models import Cluster, CrushOsdDistribution, VolumeMetric
from watcher.performance_rca import build_report


def _volume(cluster_id, pool, image, values, start):
    return [VolumeMetric(
        cluster_id=cluster_id,
        pool=pool,
        image=image,
        iops=100,
        read_latency_ms=value,
        write_latency_ms=value,
        saturated=value >= 25,
        polled_at=start + timedelta(minutes=index),
    ) for index, value in enumerate(values)]


def test_report_ranks_pool_contention_and_marks_missing_layers(db_session):
    now = datetime(2026, 8, 28, 12, 0)
    db_session.add(Cluster(id="c1", name="cluster-1", ceph_mon_nodes="", ssh_user="test", ssh_key_path="test"))
    db_session.add_all(
        _volume("c1", "rbd", "vm-a", [10, 10, 10, 30], now - timedelta(minutes=3))
        + _volume("c1", "rbd", "vm-b", [10, 10, 10, 25], now - timedelta(minutes=3))
        + _volume("c1", "cold", "vm-c", [5, 5, 5, 6], now - timedelta(minutes=3))
        + _volume("c1", "cold", "vm-d", [5, 5, 5, 6], now - timedelta(minutes=3))
        + _volume("c1", "cold", "vm-e", [5, 5, 5, 6], now - timedelta(minutes=3))
        + [CrushOsdDistribution(cluster_id="c1", osd_id=1, host="osd-a", pgs=20, updated_at=now)]
    )
    db_session.commit()

    result = build_report(
        db_session,
        "c1",
        now=now,
        live_signals={
            "status": "ready",
            "measured_osds": 4,
            "median_commit_latency_ms": 2,
            "outliers": [{"osd_id": 3, "host": "osd-b", "ratio": 5, "commit_latency_ms": 10}],
            "freshness": {"observed_at": "2026-08-28T12:00:00Z", "age_seconds": 0, "status": "fresh"},
        },
    )

    assert result["status"] == "ready"
    assert result["analyses"][0]["hypothesis"] == "pool_contention_candidate"
    assert result["chain"][2]["status"] == "observed"  # PG distribution metadata
    assert result["chain"][4]["status"] == "proxy_only"  # OSD latency proxy, not diskstats
    assert "acting-set mapping" in " ".join(result["evidence_gaps"])


def test_report_is_cluster_isolated_and_fails_closed_without_history(db_session):
    now = datetime(2026, 8, 28, 12, 0)
    db_session.add(Cluster(id="c1", name="cluster-1", ceph_mon_nodes="", ssh_user="test", ssh_key_path="test"))
    db_session.add(Cluster(id="other", name="other", ceph_mon_nodes="", ssh_user="test", ssh_key_path="test"))
    db_session.add_all(_volume("other", "rbd", "secret", [1, 1, 1, 50], now - timedelta(minutes=3)))
    db_session.commit()

    result = build_report(db_session, "c1", now=now, live_signals={"status": "unavailable"})

    assert result["status"] == "insufficient_evidence"
    assert result["analyses"] == []
    assert all(item["cluster_id"] == "c1" for item in [result])


def test_dashboard_route_is_read_only_and_cluster_scoped(dashboard_client, default_cluster_id, monkeypatch):
    monkeypatch.setattr(
        "dashboard.routes.performance_rca.report",
        lambda cluster, **kwargs: {
            "status": "insufficient_evidence",
            "conclusion": "correlation_only",
            "cluster_id": cluster.id,
            "captured_at": "2026-08-28T12:00:00Z",
            "window": {"hours": 1},
            "scope": {},
            "analyses": [],
            "chain": [],
            "evidence_gaps": ["test"],
            "_citations": [],
        },
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    page = dashboard_client.get(f"/performance-rca?cluster={default_cluster_id}")
    api = dashboard_client.get(f"/api/performance-rca?cluster={default_cluster_id}")

    assert page.status_code == 200
    assert "Cross-layer Performance RCA" in page.text
    assert api.status_code == 200
    assert api.json()["cluster_id"] == default_cluster_id
