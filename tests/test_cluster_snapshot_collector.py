import pytest

from shared import ceph_query_cache
from watcher import cluster_snapshot_collector


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})


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


def test_health_error_does_not_create_empty_snapshot_for_first_failure():
    assert cluster_snapshot_collector.publish_health_error("cluster-a", "MON unreachable") is None
