from datetime import datetime, timedelta, timezone

from shared import ceph_query_cache
from shared.cluster_snapshot import (
    mark_refreshing,
    publish_section_snapshot,
    publish_snapshot,
)
from shared.natural_language import SnapshotQueryRunner


def _isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})


def test_runner_reads_cached_status_without_an_ssh_fallback(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    publish_snapshot(
        "cluster-a",
        {"health": {"status": "HEALTH_OK", "checks": {}}},
    )
    publish_section_snapshot(
        "cluster-a",
        "status",
        {"osdmap": {"num_osds": 3, "num_up_osds": 3}},
    )
    runner = SnapshotQueryRunner(stale_after_seconds=30)

    result = runner("get_osd_stat", {}, "cluster-a")

    assert result["data"] == {"osdmap": {"num_osds": 3, "num_up_osds": 3}}
    assert result["meta"]["cluster_id"] == "cluster-a"
    assert result["meta"]["available"] is True
    assert result["meta"]["stale"] is False


def test_runner_marks_old_but_usable_snapshot_stale(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    collected_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    publish_snapshot(
        "cluster-a",
        {"health": {"status": "HEALTH_WARN", "checks": {"OSD_DOWN": {}}}},
        collected_at=collected_at,
    )
    runner = SnapshotQueryRunner(stale_after_seconds=30, max_stale_seconds=300)

    result = runner("get_health_detail", {}, "cluster-a")

    assert result["data"]["status"] == "HEALTH_WARN"
    assert result["meta"]["stale"] is True
    assert result["meta"]["age_seconds"] >= 120


def test_runner_preserves_partial_section_errors(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    publish_section_snapshot(
        "cluster-a",
        "pools",
        [{"name": "rbd", "used_percent": 42}],
        partial_errors={"pools": "one pool query timed out"},
    )
    runner = SnapshotQueryRunner()

    result = runner("get_pool_list", {}, "cluster-a")

    assert result["data"] == [{"name": "rbd", "used_percent": 42}]
    assert result["meta"]["partial"] is True
    assert result["meta"]["partial_errors"] == {"pools": "one pool query timed out"}


def test_runner_isolates_multiple_clusters(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    publish_snapshot("cluster-a", {"health": {"status": "HEALTH_OK"}})
    publish_snapshot("cluster-b", {"health": {"status": "HEALTH_ERR"}})
    runner = SnapshotQueryRunner()

    assert runner("get_health_detail", {}, "cluster-a")["data"]["status"] == "HEALTH_OK"
    assert runner("get_health_detail", {}, "cluster-b")["data"]["status"] == "HEALTH_ERR"


def test_runner_reports_refreshing_and_missing_without_querying_ceph(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    mark_refreshing("cluster-a", True)
    runner = SnapshotQueryRunner()

    result = runner("get_cluster_status", {}, "cluster-a")

    assert result["data"] is None
    assert result["meta"]["available"] is False
    assert result["meta"]["refreshing"] is True
    assert result["meta"]["stale"] is True


def test_runner_fails_closed_on_cluster_scope_mismatch(monkeypatch):
    runner = SnapshotQueryRunner()
    monkeypatch.setattr(
        runner,
        "_read_main",
        lambda _cluster_id: {"cluster_id": "cluster-b", "health": {"status": "HEALTH_OK"}},
    )

    result = runner("get_health_detail", {}, "cluster-a")

    assert result["data"] is None
    assert result["meta"]["partial_errors"] == {"snapshot": "cluster_scope_mismatch"}

