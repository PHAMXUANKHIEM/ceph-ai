import pytest

from shared import ceph_query_cache
from shared.cluster_events import (
    action_state_for_status,
    get_metrics,
    publish_action_state_event,
    publish_event,
    read_latest_event,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("PENDING", "queued"),
        ("PENDING_APPROVAL", "queued"),
        ("EXECUTING", "running"),
        ("INCONCLUSIVE", "verifying"),
        ("EXECUTED", "verifying"),
        ("AUTO_EXECUTED", "verifying"),
        ("FAILED", "failed"),
        ("REJECTED", "rejected"),
    ],
)
def test_action_status_is_normalized_for_realtime_consumers(status, expected):
    assert action_state_for_status(status) == expected


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
    assert get_metrics()["publish_success_total"] >= 2


def test_event_rejects_unknown_event_and_filters_unknown_sections(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        publish_event("cluster-a", "arbitrary_command", sections=["health"])

    event = publish_event("cluster-a", "snapshot_changed", sections=["health", "secret"])
    assert event["sections"] == ["health"]


def test_action_state_event_keeps_cluster_and_status_metadata(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)

    event = publish_action_state_event("cluster-a", "action-1", "EXECUTING")

    assert event["event"] == "action_state_changed"
    assert event["cluster_id"] == "cluster-a"
    assert event["action_id"] == "action-1"
    assert event["action_status"] == "EXECUTING"
    assert event["action_state"] == "running"
    assert read_latest_event("cluster-a")["action_status"] == "EXECUTING"
