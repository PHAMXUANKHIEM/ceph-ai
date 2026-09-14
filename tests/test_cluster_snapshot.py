from datetime import datetime, timedelta, timezone
from time import time

import pytest

from shared import ceph_query_cache
from shared import cluster_snapshot


def _isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})


def test_publish_assigns_monotonic_generation_and_survives_memory_reset(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    first = cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_OK"})
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    second = cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_WARN"})

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert cluster_snapshot.read_snapshot("cluster-a")["health"] == "HEALTH_WARN"


def test_read_snapshot_derives_age_and_stale_without_changing_payload(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    collected_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_OK"}, collected_at=collected_at)

    value = cluster_snapshot.read_snapshot("cluster-a", stale_after_seconds=30, max_stale_seconds=300)

    assert value is not None
    assert value["stale"] is True
    assert value["age_seconds"] >= 120
    assert value["generation"] == 1


def test_read_snapshot_expires_by_collected_at(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    collected_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_OK"}, collected_at=collected_at)

    assert cluster_snapshot.read_snapshot("cluster-a", max_stale_seconds=60) is None


def test_read_snapshot_prefers_new_value_written_by_another_process(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_OK"})
    newer = cluster_snapshot.make_snapshot("cluster-a", {"health": "HEALTH_WARN"})
    newer["generation"] = 2
    assert ceph_query_cache._write(
        cluster_snapshot.SNAPSHOT_NAMESPACE,
        "cluster-a",
        time(),
        newer,
    )

    value = cluster_snapshot.read_snapshot("cluster-a")

    assert value is not None
    assert value["health"] == "HEALTH_WARN"


def test_snapshot_isolated_by_cluster(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_OK"})
    cluster_snapshot.publish_snapshot("cluster-b", {"health": "HEALTH_ERR"})

    assert cluster_snapshot.read_snapshot("cluster-a")["health"] == "HEALTH_OK"
    assert cluster_snapshot.read_snapshot("cluster-b")["health"] == "HEALTH_ERR"


def test_snapshot_rejects_missing_cluster_or_invalid_threshold(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        cluster_snapshot.publish_snapshot("", {"health": "HEALTH_OK"})
    with pytest.raises(ValueError):
        cluster_snapshot.read_snapshot("cluster-a", stale_after_seconds=20, max_stale_seconds=10)


def test_snapshot_rejects_invalid_collected_at(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_OK"}, collected_at="yesterday")
