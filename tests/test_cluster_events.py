import pytest

from shared import ceph_query_cache
from shared.cluster_events import publish_event, read_latest_event


def _isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})


def test_event_is_persistent_and_cluster_scoped(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    first = publish_event("cluster-a", "snapshot_changed", sections=["health"])
    publish_event("cluster-b", "snapshot_changed", sections=["pools"])

    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    current = read_latest_event("cluster-a")

    assert current["cluster_id"] == "cluster-a"
    assert current["sections"] == ["health"]
    assert current["generation"] == first["generation"]
    assert read_latest_event("cluster-b")["sections"] == ["pools"]


def test_event_rejects_unknown_event_and_filters_unknown_sections(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        publish_event("cluster-a", "arbitrary_command", sections=["health"])

    event = publish_event("cluster-a", "snapshot_changed", sections=["health", "secret"])
    assert event["sections"] == ["health"]
