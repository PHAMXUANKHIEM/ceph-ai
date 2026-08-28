from datetime import datetime, timedelta
from types import SimpleNamespace

from shared import db
from shared.models import Cluster, CrushOsdDistribution, HostMetricSample, VolumeMetric, VolumeOsdMapping
from watcher import volume_topology
from watcher.volume_topology import normalize_osd_map_payload
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
        + [VolumeOsdMapping(
            cluster_id="c1", pool="rbd", image="vm-a", image_id="abc",
            object_name="rbd_header.abc", pgid="1.2a", acting_osds_json="[3,4,5]",
            primary_osd=3, captured_at=now,
        )]
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
    assert result["analyses"][0]["hypothesis"] == "sampled_data_osd_latency_candidate"
    assert result["analyses"][0]["topology"]["pgid"] == "1.2a"
    assert result["analyses"][0]["signals"]["mapped_outlier_osds"] == [3]
    assert result["chain"][2]["status"] == "mapped"
    assert result["chain"][4]["status"] == "proxy_only"  # OSD latency proxy, not diskstats
    assert "latest snapshot" in " ".join(result["evidence_gaps"])


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


def test_normalize_osd_map_requires_acting_set():
    assert normalize_osd_map_payload({"pgid": "2.4", "acting": [4, 7]}) == {
        "pgid": "2.4", "acting_osds": [4, 7], "primary_osd": 4,
    }


def test_map_volume_uses_rbd_header_object(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        command = args[-1]
        calls.append(command)
        if command.startswith("rbd info"):
            return "mon", {"id": "abc123", "size": 12, "object_size": 4, "block_name_prefix": "rbd_data.abc123"}
        return "mon", {"pgid": "1.2a", "acting": [3, 4, 5]}

    monkeypatch.setattr(volume_topology, "_connection", lambda cluster: ([], "container", "user", "key", "docker"))
    monkeypatch.setattr(volume_topology.ceph_client, "run_ceph_json_command_with", fake_run)
    result = volume_topology.map_volume(SimpleNamespace(), "rbd", "vm-a")

    assert result["object_name"] == "rbd_data.abc123.0000000000000000"
    assert result["acting_osds"] == [3, 4, 5]
    assert result["data_object_count"] == 3
    assert calls == [
        "rbd info rbd/vm-a",
        "ceph osd map rbd rbd_data.abc123.0000000000000000",
        "ceph osd map rbd rbd_data.abc123.0000000000000001",
        "ceph osd map rbd rbd_data.abc123.0000000000000002",
    ]


def test_report_does_not_use_stale_mapping_for_osd_correlation(db_session):
    now = datetime(2026, 8, 28, 12, 0)
    db_session.add(Cluster(id="c1", name="cluster-1", ceph_mon_nodes="", ssh_user="test", ssh_key_path="test"))
    db_session.add_all(_volume("c1", "rbd", "vm-a", [10, 10, 10, 30], now - timedelta(minutes=3)))
    db_session.add(VolumeOsdMapping(
        cluster_id="c1", pool="rbd", image="vm-a", image_id="abc",
        object_name="rbd_header.abc", pgid="1.2a", acting_osds_json="[3,4,5]",
        primary_osd=3, captured_at=now - timedelta(hours=2), mapping_scope="header_legacy",
    ))
    db_session.commit()

    result = build_report(db_session, "c1", now=now, live_signals={
        "status": "ready", "measured_osds": 4, "median_commit_latency_ms": 2,
        "outliers": [{"osd_id": 3, "ratio": 5, "commit_latency_ms": 10}],
        "freshness": {"observed_at": "2026-08-28T12:00:00Z", "age_seconds": 0, "status": "fresh"},
    })

    assert result["analyses"][0]["hypothesis"] != "sampled_data_osd_latency_candidate"
    assert "stale/legacy" in " ".join(result["evidence_gaps"])


def test_report_uses_fresh_host_disk_signal_for_mapped_volume(db_session):
    now = datetime(2026, 8, 28, 12, 0)
    db_session.add(Cluster(id="c1", name="cluster-1", ceph_mon_nodes="", ssh_user="test", ssh_key_path="test"))
    db_session.add_all(_volume("c1", "rbd", "vm-a", [10, 10, 10, 30], now - timedelta(minutes=3)))
    db_session.add(CrushOsdDistribution(
        cluster_id="c1", osd_id=3, host="ceph-1", pgs=20, updated_at=now,
    ))
    db_session.add(VolumeOsdMapping(
        cluster_id="c1", pool="rbd", image="vm-a", image_id="abc",
        object_name="rbd_data.abc.0000000000000000", pgid="1.2a", acting_osds_json="[3]",
        primary_osd=3, pgids_json="[\"1.2a\"]", sampled_objects_json="[\"rbd_data.abc.0000000000000000\"]",
        data_object_count=1, mapping_scope="data_sample", captured_at=now,
    ))
    db_session.add(HostMetricSample(
        cluster_id="c1", host="10.0.0.3", node_name="ceph-1", cpu_percent=40,
        mem_percent=40, disk_read_iops=100, disk_write_iops=100, disk_latency_ms=30,
        network_rx_bytes_per_sec=1000, network_tx_bytes_per_sec=2000, collected_at=now,
    ))
    db_session.commit()

    result = build_report(db_session, "c1", now=now, live_signals={"status": "unavailable"})

    assert result["analyses"][0]["hypothesis"] == "host_resource_candidate"
    assert result["analyses"][0]["host_evidence"][0]["flags"] == ["disk_latency_high"]
    assert result["chain"][4]["status"] == "observed"
    assert result["chain"][5]["status"] == "observed"


def test_collector_refreshes_only_recent_volume_mappings(dashboard_client, default_cluster_id, monkeypatch):
    now = datetime(2026, 8, 28, 12, 0)
    with db.SessionLocal() as session:
        session.add(VolumeMetric(
            cluster_id=default_cluster_id, pool="rbd", image="vm-a", iops=10,
            read_latency_ms=2, write_latency_ms=3, saturated=False, polled_at=now,
        ))
        session.commit()

    monkeypatch.setattr(volume_topology, "map_volume", lambda cluster, pool, image: {
        "pool": pool, "image": image, "image_id": "abc", "object_name": "rbd_header.abc",
        "pgid": "1.2a", "acting_osds": [1, 2, 3], "primary_osd": 1,
        "pgids": ["1.2a"], "sampled_objects": ["rbd_data.abc.0000000000000000"],
        "data_object_count": 1, "mapping_scope": "data_sample",
    })
    cluster = SimpleNamespace()
    assert volume_topology.collect_and_store(default_cluster_id, cluster, now=now) == 1

    with db.SessionLocal() as session:
        row = session.get(VolumeOsdMapping, (default_cluster_id, "rbd", "vm-a"))
        assert row.pgid == "1.2a"
        assert row.acting_osds_json == "[1,2,3]"


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
