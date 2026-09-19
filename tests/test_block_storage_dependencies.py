from watcher.block_storage_dependencies import build_pool_dependency_health


def test_pool_dependency_health_links_pool_pg_osd_and_failure_domain():
    result = build_pool_dependency_health(
        "vms",
        {"status": "HEALTH_WARN"},
        {"pg_stats": [
            {"pgid": "3.a", "state": "active+clean", "acting": [1, 2, 3], "up": [1, 2, 3]},
            {"pgid": "3.b", "state": "active+undersized+degraded", "acting": [1, 2], "up": [1, 2, 3]},
            {"pgid": "3.c", "state": "stale", "acting": [2], "up": [2]},
        ]},
        {"nodes": [
            {"id": -1, "type": "root", "name": "default", "children": [-2, -3]},
            {"id": -2, "type": "host", "name": "node-a", "children": [1, 2]},
            {"id": -3, "type": "host", "name": "node-b", "children": [3]},
            {"id": 1, "type": "osd", "status": "up"},
            {"id": 2, "type": "osd", "status": "down"},
            {"id": 3, "type": "osd", "status": "up"},
        ]},
        volume_count=4,
    )

    assert result["status"] == "CRITICAL"
    assert result["volume_count"] == 4
    assert result["pg"]["total"] == 3
    assert result["pg"]["affected_count"] == 2
    assert result["pg"]["bad_count"] == 1
    assert result["osd"]["down_osd_ids"] == [2]
    assert result["failure_domains"]["down_hosts"] == ["node-a"]
    assert result["dependency_scope"]["exact_volume_to_pg"] is False
    assert result["read_only"] is True


def test_pool_dependency_health_fails_closed_when_topology_is_missing():
    result = build_pool_dependency_health(
        "vms", {"status": "HEALTH_OK"},
        {"pg_stats": [{"pgid": "3.a", "state": "active+clean", "acting": [1]}]},
        {},
    )

    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert "không đọc được CRUSH/OSD topology" in result["evidence"]["gaps"]
