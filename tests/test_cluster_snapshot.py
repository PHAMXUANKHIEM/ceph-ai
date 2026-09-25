import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from time import time

import pytest

from shared import ceph_query_cache
from shared.request_context import reset_request_id, set_request_id
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

def test_first_section_error_is_unavailable(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    snapshot = cluster_snapshot.record_section_error(
        "cluster-a", "pools", RuntimeError("Ceph unavailable"), empty_data=[]
    )
    assert snapshot["section_available"] is False
    stored = cluster_snapshot.read_section_snapshot("cluster-a", "pools")
    assert stored["section_available"] is False


def test_section_error_retains_last_good_payload_and_generation(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    published = cluster_snapshot.publish_section_snapshot(
        "cluster-a",
        "pools",
        [{"name": "volumes", "bytes_used": 4096}],
    )

    failed = cluster_snapshot.record_section_error(
        "cluster-a",
        "pools",
        RuntimeError("MON query timed out"),
        empty_data=[],
    )
    stored = cluster_snapshot.read_section_snapshot("cluster-a", "pools")

    assert failed is not None
    assert failed["generation"] == published["generation"]
    assert stored is not None
    assert stored["pools"] == [{"name": "volumes", "bytes_used": 4096}]
    assert stored["section_available"] is True
    assert stored["last_error"] == "MON query timed out"
    assert stored["partial_errors"] == {"pools": "MON query timed out"}
    assert stored["collected_at"] == published["collected_at"]
    assert isinstance(stored["last_attempted_at"], str)


def test_refresh_lifecycle_is_persistent_and_invalidation_is_cluster_scoped(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_OK"})
    cluster_snapshot.publish_snapshot("cluster-b", {"health": "HEALTH_WARN"})

    assert cluster_snapshot.mark_refreshing("cluster-a") is True
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    assert cluster_snapshot.is_refreshing("cluster-a") is True
    assert cluster_snapshot.read_snapshot("cluster-a")["refreshing"] is True
    assert cluster_snapshot.is_refreshing("cluster-b") is False

    assert cluster_snapshot.mark_refreshing("cluster-a", False) is False
    assert cluster_snapshot.is_refreshing("cluster-a") is False
    cluster_snapshot.invalidate_snapshot("cluster-a")
    assert cluster_snapshot.read_snapshot("cluster-a") is None
    assert cluster_snapshot.read_snapshot("cluster-b")["health"] == "HEALTH_WARN"


def test_successful_publish_clears_refresh_marker(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    cluster_snapshot.mark_refreshing("cluster-a")
    assert cluster_snapshot.is_refreshing("cluster-a") is True
    cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_OK"})
    assert cluster_snapshot.is_refreshing("cluster-a") is False
    assert cluster_snapshot.read_snapshot("cluster-a")["refreshing"] is False


def test_refresh_claim_is_single_flight_and_cluster_scoped(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)

    assert cluster_snapshot.claim_refresh("cluster-a") is True
    assert cluster_snapshot.claim_refresh("cluster-a") is False
    assert cluster_snapshot.claim_refresh("cluster-b") is True

    cluster_snapshot.mark_refreshing("cluster-a", False)
    assert cluster_snapshot.claim_refresh("cluster-a") is True


def test_priority_refresh_marker_is_cluster_scoped_and_bounded(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)

    marker = cluster_snapshot.request_priority_refresh(
        "cluster-a", ["pools", "secret", "pools", "health"]
    )

    second_marker = cluster_snapshot.request_priority_refresh("cluster-a", ["nodes"])
    assert marker["request_id"] != second_marker["request_id"]
    assert marker["sections"] == ["pools", "health"]
    assert cluster_snapshot.read_priority_refresh("cluster-a")["request_id"] == second_marker["request_id"]
    assert cluster_snapshot.read_priority_refresh("cluster-b") is None

    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    assert cluster_snapshot.read_priority_refresh("cluster-a")["sections"] == ["nodes"]


def test_priority_refresh_preserves_request_correlation(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    token = set_request_id("browser-action-43")
    try:
        marker = cluster_snapshot.request_priority_refresh("cluster-a", ["health"])
    finally:
        reset_request_id(token)

    assert marker["request_id"] == "browser-action-43"


def test_persisted_snapshot_is_readable_after_process_restart(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    cluster_snapshot.publish_snapshot("cluster-a", {"health": "HEALTH_WARN"})
    child_env = os.environ.copy()
    child_env["CEPH_AI_CACHE_DIR"] = str(tmp_path)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from shared.cluster_snapshot import read_snapshot; "
            "print(read_snapshot('cluster-a', max_stale_seconds=300)['health'])",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=child_env,
    )
    assert child.stdout.strip() == "HEALTH_WARN"


def test_section_snapshot_reports_the_cluster_refresh_state(monkeypatch, tmp_path):
    """Cờ refresh được ghi theo cluster, không theo section. Trước đây
    `_read_snapshot_by_key` tra cứu bằng storage key "<cluster>:<section>"
    nên mọi section snapshot vĩnh viễn báo refreshing=False — chỉ báo "đang
    cập nhật" của Pools/PGs/CRUSH/Nodes không bao giờ sáng."""
    _isolate_cache(monkeypatch, tmp_path)
    cluster_snapshot.publish_section_snapshot("cluster-a", "pools", [{"name": "rbd"}])
    cluster_snapshot.mark_refreshing("cluster-a", True)

    assert cluster_snapshot.read_section_snapshot("cluster-a", "pools")["refreshing"] is True
    assert cluster_snapshot.read_snapshot("cluster-a") is None  # health chưa publish

    cluster_snapshot.mark_refreshing("cluster-a", False)
    assert cluster_snapshot.read_section_snapshot("cluster-a", "pools")["refreshing"] is False


def test_large_pg_section_is_bounded_before_cache_publish(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(cluster_snapshot.settings, "ceph_snapshot_max_payload_bytes", 220)
    rows = [{"pgid": f"1.{index}", "state": "active+clean", "pool": "rbd"} for index in range(30)]

    stored = cluster_snapshot.publish_section_snapshot("cluster-a", "pgs", rows)

    assert len(stored["pgs"]) < len(rows)
    assert stored["payload_limits"]["pgs"]["truncated"] is True
    assert stored["payload_limits"]["pgs"]["stored_bytes"] <= 220


def test_large_crush_section_is_bounded_before_cache_publish(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(cluster_snapshot.settings, "ceph_snapshot_max_payload_bytes", 300)
    tree = {"state": "ok", "roots": [{"id": -1, "type": "root", "children": [
        {"id": index, "type": "osd", "name": "osd." + str(index)} for index in range(50)
    ]}], "rules": []}

    stored = cluster_snapshot.publish_section_snapshot("cluster-a", "crush", tree)

    assert stored["crush"].get("payload_truncated") is True
    assert stored["payload_limits"]["crush"]["stored_bytes"] <= 300


def test_fingerprint_changes_on_publish_without_reading_the_payload(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    assert cluster_snapshot.snapshot_fingerprint("cluster-a") is None
    assert cluster_snapshot.section_snapshot_fingerprint("cluster-a", "pools") is None

    cluster_snapshot.publish_section_snapshot("cluster-a", "pools", [{"name": "rbd"}])
    before = cluster_snapshot.section_snapshot_fingerprint("cluster-a", "pools")
    assert before is not None

    cluster_snapshot.publish_section_snapshot("cluster-a", "pools", [{"name": "volumes"}])
    assert cluster_snapshot.section_snapshot_fingerprint("cluster-a", "pools") != before
    # Section khác không đổi theo.
    assert cluster_snapshot.section_snapshot_fingerprint("cluster-a", "pgs") is None


def test_fingerprint_expires_with_the_same_window_as_a_read(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    cluster_snapshot.publish_section_snapshot("cluster-a", "pools", [{"name": "rbd"}])

    assert cluster_snapshot.section_snapshot_fingerprint(
        "cluster-a", "pools", max_stale_seconds=0
    ) is None
