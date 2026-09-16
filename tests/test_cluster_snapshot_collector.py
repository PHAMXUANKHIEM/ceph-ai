from contextlib import contextmanager
import threading

import pytest

from shared import ceph_query_cache
from shared.request_context import get_request_id, reset_request_id, set_request_id
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


def test_health_collection_lock_has_bounded_acquisition(monkeypatch):
    captured = {}

    @contextmanager
    def unavailable_lock(namespace, key, *, timeout_seconds=None):
        captured.update(
            namespace=namespace,
            key=key,
            timeout_seconds=timeout_seconds,
        )
        raise ceph_query_cache.CacheLockError("lock is busy")
        yield

    monkeypatch.setattr(ceph_query_cache, "key_lock", unavailable_lock)
    monkeypatch.setattr(cluster_snapshot_collector.settings, "ceph_health_timeout", 8)

    with pytest.raises(TimeoutError, match="health collection lock acquisition"):
        with cluster_snapshot_collector.health_collection_lock("cluster-a"):
            pass

    assert captured == {
        "namespace": cluster_snapshot_collector.COLLECTION_LOCK_NAMESPACE,
        "key": "cluster-a",
        "timeout_seconds": 8.0,
    }


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
    assert metrics["last_duration_ms"] is not None
    assert metrics["last_cluster_id"] == "cluster-a"
    assert metrics["last_completed_at"]


def test_inventory_sections_run_in_parallel_with_a_bounded_worker_count(monkeypatch):
    cluster = type("ClusterConfig", (), {"id": "cluster-a", "name": "lab"})()
    barrier = threading.Barrier(2)
    started = []

    observed_request_ids = []

    def loader(section, result):
        def collect(_cluster):
            started.append(section)
            observed_request_ids.append(get_request_id())
            barrier.wait(timeout=2)
            return result

        return collect

    monkeypatch.setattr(cluster_snapshot_collector, "_collect_pool_rows", loader("pools", []))
    monkeypatch.setattr(cluster_snapshot_collector, "_collect_pg_rows", loader("pgs", []))
    monkeypatch.setattr(
        cluster_snapshot_collector,
        "_collect_crush_tree",
        lambda _cluster: {"state": "no_snapshot_yet"},
    )
    monkeypatch.setattr(
        cluster_snapshot_collector,
        "_collect_node_summary",
        lambda _cluster: {"nodes": [], "total": 0},
    )

    token = set_request_id("inventory-trace")
    try:
        result = cluster_snapshot_collector.CephSnapshotCollector(max_workers=2).collect_inventory(cluster)
    finally:
        reset_request_id(token)

    assert set(started) == {"pools", "pgs"}
    assert set(result) == {"pools", "pgs", "crush", "nodes"}
    assert observed_request_ids == ["inventory-trace", "inventory-trace"]
