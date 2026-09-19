from watcher.block_storage_pool_lifecycle import build_pool_lifecycle_inventory


def _overview(**overrides):
    value = {
        "rbd_enabled": True,
        "application_metadata": {"rbd": {}},
        "pg_num": 32,
        "pgp_num": 32,
        "pg_autoscale_mode": "on",
        "quota_max_bytes": None,
        "quota_max_objects": None,
    }
    value.update(overrides)
    return value


def test_pool_lifecycle_reports_read_only_posture_and_dependencies():
    result = build_pool_lifecycle_inventory(
        "vms", _overview(), volume_count=3,
        dependency={"status": "HEALTHY"},
    )

    assert result["status"] == "HAS_DEPENDENCIES"
    assert result["application"] == {"rbd_enabled": True, "tags": ["rbd"]}
    assert result["pg"]["autoscale_mode"] == "on"
    assert result["dependencies"]["volume_count"] == 3
    assert result["operation_guard"]["delete"] == "blocked_until_empty_and_approved"
    assert result["capabilities"]["direct_mutation_supported"] is False
    assert result["read_only"] is True


def test_pool_lifecycle_fails_closed_for_missing_capability_evidence():
    result = build_pool_lifecycle_inventory(
        "vms", _overview(rbd_enabled=False, application_metadata={}, pg_num=0),
        volume_count=None,
        dependency={"status": "INSUFFICIENT_EVIDENCE"},
    )

    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["evidence"]["gaps"]
    assert result["operation_guard"]["configure"] == "approval_required"


def test_pool_lifecycle_marks_critical_dependency_as_blocked():
    result = build_pool_lifecycle_inventory(
        "vms", _overview(quota_max_bytes=1024, quota_max_objects=10),
        volume_count=0,
        dependency={"status": "CRITICAL"},
    )

    assert result["status"] == "BLOCKED"
    assert "dependency health: CRITICAL" in result["blockers"]
    assert result["quota"]["configured"] is True
