"""Validation and naming helpers for scheduled Cinder snapshots."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger


SNAPSHOT_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
DEFAULT_CRON = "0 2 * * *"
DEFAULT_TIMEZONE = "UTC"


def validate_snapshot_policy(
    *, cron_expression: str, timezone_name: str, snapshot_prefix: str,
    retention_count: int, capacity_guard_percent: float,
) -> tuple[str, str, str, int, float, ZoneInfo]:
    cron = str(cron_expression or "").strip()
    tz_name = str(timezone_name or "").strip()
    prefix = str(snapshot_prefix or "").strip()
    try:
        retention = int(retention_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("retention_count phải là số nguyên") from exc
    try:
        guard = float(capacity_guard_percent)
    except (TypeError, ValueError) as exc:
        raise ValueError("capacity_guard_percent phải là số") from exc
    if len(cron.split()) != 5:
        raise ValueError("cron_expression phải có đúng 5 trường cron")
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone không hợp lệ") from exc
    try:
        CronTrigger.from_crontab(cron, timezone=zone)
    except (TypeError, ValueError) as exc:
        raise ValueError("cron_expression không hợp lệ") from exc
    if not SNAPSHOT_PREFIX_RE.fullmatch(prefix):
        raise ValueError("snapshot_prefix không hợp lệ")
    if not 1 <= retention <= 365:
        raise ValueError("retention_count phải trong khoảng 1-365")
    if not 0 < guard < 100:
        raise ValueError("capacity_guard_percent phải trong khoảng 0-100")
    return cron, tz_name, prefix, retention, guard, zone


def next_run_at(cron_expression: str, timezone_name: str, now: datetime | None = None) -> datetime:
    cron, tz_name, _prefix, _retention, _guard, zone = validate_snapshot_policy(
        cron_expression=cron_expression,
        timezone_name=timezone_name,
        snapshot_prefix="scheduled",
        retention_count=1,
        capacity_guard_percent=50,
    )
    current = now or datetime.now(zone)
    return CronTrigger.from_crontab(cron, timezone=zone).get_next_fire_time(None, current)


def snapshot_name(prefix: str, when: datetime) -> str:
    return f"{prefix}-{when.strftime('%Y%m%d%H%M%S')}"
