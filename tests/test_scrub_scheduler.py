"""Tests for the advisory-only Smart Scrub Scheduler."""

from datetime import datetime

from watcher.scrub_scheduler import build_scrub_schedule


def _pg(scrub, deep, state="active+clean"):
    return {
        "pgid": "1.a",
        "pool": "data",
        "state": state,
        "last_scrub": scrub,
        "last_deep_scrub": deep,
    }


def test_schedules_deep_scrub_inside_maintenance_window():
    result = build_scrub_schedule(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pg_rows=[_pg("2026-09-10T00:00:00Z", "2026-09-01T00:00:00Z")],
        now=datetime(2026, 9, 21, 23, 0),
        maintenance_start_hour=22,
        maintenance_end_hour=6,
    )

    assert result["summary"]["due_count"] == 1
    item = result["schedule"][0]
    assert item["scrub_type"] == "deep_scrub"
    assert item["status"] == "scheduled_advisory"
    assert item["recommended_window"]["start"].startswith("2026-09-21T23:00")


def test_defers_busy_pg_but_never_defers_safety_overdue_pg():
    result = build_scrub_schedule(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pg_rows=[
            _pg("2026-09-10T00:00:00Z", "2026-09-10T00:00:00Z", "active+recovering"),
            _pg("2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z", "active+recovering"),
        ],
        now=datetime(2026, 9, 21),
        maintenance_start_hour=22,
        maintenance_end_hour=6,
    )

    statuses = {item["status"] for item in result["schedule"]}
    assert "deferred_busy" in statuses
    assert "safety_overdue" in statuses
    assert any(item["must_not_defer"] for item in result["schedule"])


def test_missing_timestamps_fail_closed():
    result = build_scrub_schedule(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pg_rows=[_pg("—", None)],
        now=datetime(2026, 9, 21),
    )

    assert result["schedule"] == []
    codes = {gap["code"] for gap in result["evidence_gaps"]}
    assert "LAST_SCRUB_TIMESTAMP_MISSING" in codes
    assert "LAST_DEEP_SCRUB_TIMESTAMP_MISSING" in codes


def test_scheduler_never_returns_commands_or_changes_flags():
    result = build_scrub_schedule(
        cluster_id="cluster-1",
        cluster_name="CS-LAB",
        pg_rows=[_pg("2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z")],
        now=datetime(2026, 9, 21),
    )

    assert result["limits"]["execution_commands_included"] is False
    assert result["limits"]["scrub_flags_changed"] is False
    assert "ceph pg" not in repr(result)
