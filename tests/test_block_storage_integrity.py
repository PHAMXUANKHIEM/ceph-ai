from watcher.block_storage_integrity import build_integrity_evidence


def _detail():
    return {"pool": "vms", "name": "vm-01", "watchers": [], "locks": []}


def _pgs(*, state="active+clean", with_timestamps=True):
    row = {"pgid": "3.a", "state": state, "acting": [1, 2, 3]}
    if with_timestamps:
        row.update({
            "last_scrub_stamp": "2026-09-19T01:00:00+0000",
            "last_deep_scrub_stamp": "2026-09-18T01:00:00+0000",
        })
    return {"pg_stats": [row]}


def _backup(*, checksum=True):
    return {"latest_success": {"job_type": "full", "sha256_present": checksum}}


def test_integrity_evidence_is_healthy_only_with_complete_read_only_evidence():
    result = build_integrity_evidence(
        _detail(),
        {"status": "HEALTH_OK", "checks": {}},
        _pgs(),
        _backup(),
    )

    assert result["status"] == "HEALTHY"
    assert result["scrub"]["pg_count"] == 1
    assert result["scrub"]["missing_last_deep_scrub"] == 0
    assert result["checksum"] == {
        "status": "AVAILABLE", "source": "full", "artifact_byte_level": True,
        "current_volume_verified": False,
    }
    assert result["read_only"] is True
    assert result["repair"]["automatic_repair"] is False
    assert result["repair"]["repair_supported"] is False


def test_integrity_evidence_marks_scrub_or_health_findings_critical():
    result = build_integrity_evidence(
        _detail(),
        {
            "status": "HEALTH_WARN",
            "checks": {"PG_DAMAGED": {"severity": "HEALTH_ERR", "summary": "inconsistent PG"}},
        },
        _pgs(state="active+inconsistent"),
        _backup(),
        incidents=[{"id": 7, "status": "NEW"}],
    )

    assert result["status"] == "CRITICAL"
    assert result["health"]["findings"][0]["code"] == "PG_DAMAGED"
    assert result["scrub"]["affected_count"] == 1
    assert result["incidents"]["open_count"] == 1


def test_integrity_evidence_fails_closed_when_evidence_is_missing():
    result = build_integrity_evidence(
        {},
        {"status": "HEALTH_OK", "checks": {}},
        {"pg_stats": []},
        _backup(checksum=False),
    )

    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["checksum"]["status"] == "NOT_AVAILABLE"
    assert result["read_path"]["full_export_performed"] is False
    assert result["evidence_gaps"]
