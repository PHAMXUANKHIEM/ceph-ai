import pytest

from shared import ceph_query_cache
from watcher import cluster_snapshot_collector


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    monkeypatch.setattr(
        cluster_snapshot_collector,
        "_METRICS",
        {
            "success_total": 0,
            "failure_total": 0,
            "collector_success_total": 0,
            "collector_failure_total": 0,
            "commands": {},
        },
    )


def test_publish_health_snapshot_reuses_health_result_without_extra_query():
    health = {
        "status": "HEALTH_WARN",
        "checks": {"OSD_DOWN": {"severity": "HEALTH_WARN"}},
    }

    snapshot = cluster_snapshot_collector.publish_health_snapshot(
        "cluster-a", health, collection_started_monotonic=0
    )

    assert snapshot is not None
    assert snapshot["cluster_id"] == "cluster-a"
    assert snapshot["health"] == health
    assert snapshot["health_status"] == "HEALTH_WARN"
    assert snapshot["health_checks"] == health["checks"]
    assert snapshot["collection"]["tier"] == "critical"
    assert snapshot["generation"] == 1


def test_publish_health_error_keeps_last_good_payload_and_marks_error():
    first = cluster_snapshot_collector.publish_health_snapshot(
        "cluster-a", {"status": "HEALTH_OK", "checks": {}},
    )

    second = cluster_snapshot_collector.publish_health_error("cluster-a", "MON unreachable")

    assert first is not None and second is not None
    assert second["health"]["status"] == "HEALTH_OK"
    assert second["last_error"] == "MON unreachable"
    assert second["partial_errors"] == {"health": "MON unreachable"}
    assert second["generation"] == 1
    assert second["collected_at"] == first["collected_at"]
    assert second["published_at"] == first["published_at"]
    assert second["last_attempted_at"] != first["last_attempted_at"]


def test_health_error_records_first_failure_as_unknown_snapshot():
    snapshot = cluster_snapshot_collector.publish_health_error("cluster-a", "MON unreachable")

    assert snapshot is not None
    assert snapshot["health"]["status"] == "UNKNOWN"
    assert snapshot["health_available"] is False
    assert snapshot["last_error"] == "MON unreachable"
    assert snapshot["partial_errors"] == {"health": "MON unreachable"}


def test_collect_and_publish_health_uses_explicit_cluster_connection(monkeypatch):
    cluster = type("ClusterConfig", (), {
        "id": "cluster-a",
        "ceph_mon_nodes": "10.0.0.1, 10.0.0.2",
        "ceph_container_name": "ceph-mon",
        "ssh_user": "ceph",
        "ssh_key_path": "/tmp/key",
        "ceph_exec_mode": "cephadm",
    })()
    calls = []

    def fake_query(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "HEALTH_OK", "checks": {}}

    monkeypatch.setattr(cluster_snapshot_collector, "query_cluster_health_with", fake_query)

    health = cluster_snapshot_collector.collect_and_publish_health(
        cluster,
        mon_nodes=["10.0.0.2", "10.0.0.1"],
    )

    assert health["status"] == "HEALTH_OK"
    assert calls == [(
        (
            ["10.0.0.2", "10.0.0.1"],
            "ceph-mon",
            "ceph",
            "/tmp/key",
            "cephadm",
        ),
        {"update_sticky_fallback": False},
    )]
    assert cluster_snapshot_collector.read_snapshot("cluster-a")["health"] == health


def test_collector_metrics_include_success_failure_and_command_breakdown():
    cluster_snapshot_collector.publish_health_snapshot(
        "cluster-a", {"status": "HEALTH_OK", "checks": {}}
    )
    cluster_snapshot_collector.publish_health_error("cluster-a", "MON unreachable")

    metrics = cluster_snapshot_collector.get_metrics()
    assert metrics["collector_success_total"] == 1
    assert metrics["collector_failure_total"] == 1
    assert metrics["commands"]["health"] == {"success_total": 1, "failure_total": 1}


def test_status_and_inventory_failures_have_named_metrics(monkeypatch):
    cluster = type("ClusterConfig", (), {
        "id": "cluster-a", "ceph_mon_nodes": "10.0.0.1",
        "ceph_container_name": "ceph-mon", "ssh_user": "ceph",
        "ssh_key_path": "/tmp/key", "ceph_exec_mode": "cephadm",
        "name": "lab",
    })()
    monkeypatch.setattr(
        cluster_snapshot_collector,
        "query_cluster_status_with",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("status down")),
    )
    monkeypatch.setattr(
        cluster_snapshot_collector,
        "_collect_pool_rows",
        lambda cluster: (_ for _ in ()).throw(RuntimeError("pool down")),
    )

    with pytest.raises(RuntimeError):
        cluster_snapshot_collector.collect_and_publish_status(cluster)
    cluster_snapshot_collector.collect_and_publish_inventory(cluster)

    metrics = cluster_snapshot_collector.get_metrics()
    assert metrics["commands"]["status"]["failure_total"] == 1
    assert metrics["commands"]["pools"]["failure_total"] == 1
