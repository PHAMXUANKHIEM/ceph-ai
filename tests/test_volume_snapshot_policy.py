from datetime import datetime

import pytest

from shared.volume_snapshot_policy import next_run_at, snapshot_name, validate_snapshot_policy


def test_snapshot_policy_validation_accepts_timezone_and_cron():
    cron, timezone_name, prefix, retention, guard, _zone = validate_snapshot_policy(
        cron_expression="15 3 * * 1-5",
        timezone_name="Asia/Ho_Chi_Minh",
        snapshot_prefix="daily",
        retention_count=14,
        capacity_guard_percent=88,
    )

    assert cron == "15 3 * * 1-5"
    assert timezone_name == "Asia/Ho_Chi_Minh"
    assert prefix == "daily"
    assert retention == 14
    assert guard == 88


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cron_expression": "every day", "timezone_name": "UTC", "snapshot_prefix": "daily", "retention_count": 7, "capacity_guard_percent": 85},
        {"cron_expression": "0 2 * * *", "timezone_name": "No/Such_Zone", "snapshot_prefix": "daily", "retention_count": 7, "capacity_guard_percent": 85},
        {"cron_expression": "0 2 * * *", "timezone_name": "UTC", "snapshot_prefix": "../bad", "retention_count": 7, "capacity_guard_percent": 85},
        {"cron_expression": "0 2 * * *", "timezone_name": "UTC", "snapshot_prefix": "daily", "retention_count": 0, "capacity_guard_percent": 85},
        {"cron_expression": "0 2 * * *", "timezone_name": "UTC", "snapshot_prefix": "daily", "retention_count": 7, "capacity_guard_percent": 100},
    ],
)
def test_snapshot_policy_validation_rejects_unsafe_values(kwargs):
    with pytest.raises(ValueError):
        validate_snapshot_policy(**kwargs)


def test_snapshot_policy_next_run_and_name_are_deterministic():
    now = datetime(2026, 9, 18, 1, 0)
    next_run = next_run_at("0 2 * * *", "UTC", now)

    assert next_run.hour == 2
    assert snapshot_name("daily", now) == "daily-20260918010000"
