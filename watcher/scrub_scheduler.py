"""Read-only Smart Scrub Scheduler.

The scheduler ranks scrub work from PG scrub timestamps and current PG state.
It never invokes a Ceph scrub command or changes scrub flags. Missing evidence,
busy recovery state, and absent maintenance windows are explicit outcomes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_SCRUB_INTERVAL_DAYS = 7
DEFAULT_DEEP_SCRUB_INTERVAL_DAYS = 14
MAX_ITEMS = 500


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or value.strip() in {"—", "-"}:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _hours_old(value: object, now: datetime) -> float | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 3600)


def _busy_state(state: object) -> bool:
    normalized = str(state or "").lower()
    return any(token in normalized for token in ("recover", "backfill", "peering"))


def _window_for(
    now: datetime,
    start_hour: int | None,
    end_hour: int | None,
) -> dict[str, str] | None:
    if start_hour is None or end_hour is None:
        return None
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23) or start_hour == end_hour:
        return None
    start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    if now >= end:
        start += timedelta(days=1)
        end += timedelta(days=1)
    elif now < start:
        pass
    else:
        start = now.replace(second=0, microsecond=0)
    return {
        "start": start.isoformat(timespec="minutes") + "Z",
        "end": end.isoformat(timespec="minutes") + "Z",
    }


def _item(
    row: dict[str, Any],
    *,
    kind: str,
    age_hours: float,
    interval_days: int,
    hard_limit_days: int,
    busy: bool,
    window: dict[str, str] | None,
) -> dict[str, Any]:
    pgid = str(row.get("pgid") or "unknown")
    pool = str(row.get("pool") or "unknown")
    state = str(row.get("state") or "unknown")
    hard_limit_hours = hard_limit_days * 24
    overdue_safety = age_hours >= hard_limit_hours
    if busy and not overdue_safety:
        status = "deferred_busy"
        reason = "PG đang recovery/backfill/peering; tạm hoãn để không tăng tải."
        must_not_defer = False
    elif overdue_safety:
        status = "safety_overdue"
        reason = "PG đã vượt giới hạn an toàn; không được trì hoãn thêm chỉ vì tải."
        must_not_defer = True
    elif window is None:
        status = "window_unavailable"
        reason = "Đã đến hạn nhưng chưa có maintenance window hợp lệ."
        must_not_defer = False
    else:
        status = "scheduled_advisory"
        reason = "Đã đến hạn và phù hợp để đưa vào maintenance window."
        must_not_defer = False
    return {
        "pgid": pgid,
        "pool": pool,
        "state": state,
        "scrub_type": kind,
        "age_hours": round(age_hours, 2),
        "interval_days": interval_days,
        "hard_limit_days": hard_limit_days,
        "status": status,
        "reason": reason,
        "recommended_window": window if status == "scheduled_advisory" else None,
        "must_not_defer": must_not_defer,
        "read_only": True,
        "action_id": None,
    }


def build_scrub_schedule(
    *,
    cluster_id: str,
    cluster_name: str,
    pg_rows: list[dict[str, Any]] | None,
    now: datetime | None = None,
    scrub_interval_days: int = DEFAULT_SCRUB_INTERVAL_DAYS,
    deep_scrub_interval_days: int = DEFAULT_DEEP_SCRUB_INTERVAL_DAYS,
    maintenance_start_hour: int | None = None,
    maintenance_end_hour: int | None = None,
) -> dict[str, Any]:
    """Return bounded scrub scheduling advice without an execution command."""
    observed_at = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if observed_at.tzinfo is not None:
        observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    scrub_days = max(1, int(scrub_interval_days))
    deep_days = max(scrub_days, int(deep_scrub_interval_days))
    window = _window_for(observed_at, maintenance_start_hour, maintenance_end_hour)
    rows = [row for row in (pg_rows or []) if isinstance(row, dict)]
    schedule: list[dict[str, Any]] = []
    evidence_gaps: list[dict[str, str]] = []
    missing_scrub = 0
    missing_deep = 0

    for row in rows[:MAX_ITEMS]:
        scrub_age = _hours_old(row.get("last_scrub"), observed_at)
        deep_age = _hours_old(row.get("last_deep_scrub"), observed_at)
        if scrub_age is None:
            missing_scrub += 1
        if deep_age is None:
            missing_deep += 1
        if scrub_age is None or deep_age is None:
            continue

        if deep_age >= deep_days * 24:
            schedule.append(_item(
                row,
                kind="deep_scrub",
                age_hours=deep_age,
                interval_days=deep_days,
                hard_limit_days=deep_days * 2,
                busy=_busy_state(row.get("state")),
                window=window,
            ))
        elif scrub_age >= scrub_days * 24:
            schedule.append(_item(
                row,
                kind="scrub",
                age_hours=scrub_age,
                interval_days=scrub_days,
                hard_limit_days=scrub_days * 2,
                busy=_busy_state(row.get("state")),
                window=window,
            ))

    if not rows:
        evidence_gaps.append({
            "code": "PG_SCRUB_INVENTORY_UNAVAILABLE",
            "reason": "Chưa có PG inventory để lập lịch scrub.",
        })
    if missing_scrub:
        evidence_gaps.append({
            "code": "LAST_SCRUB_TIMESTAMP_MISSING",
            "reason": f"{missing_scrub} PG thiếu timestamp last scrub.",
        })
    if missing_deep:
        evidence_gaps.append({
            "code": "LAST_DEEP_SCRUB_TIMESTAMP_MISSING",
            "reason": f"{missing_deep} PG thiếu timestamp last deep-scrub.",
        })
    if maintenance_start_hour is None or maintenance_end_hour is None:
        evidence_gaps.append({
            "code": "MAINTENANCE_WINDOW_UNCONFIGURED",
            "reason": "Chưa cấu hình maintenance window; chỉ trả due/defer advice.",
        })
    if any(_busy_state(row.get("state")) for row in rows):
        evidence_gaps.append({
            "code": "RECOVERY_OR_BACKFILL_ACTIVITY_PRESENT",
            "reason": "Có PG đang recovery/backfill/peering; scrub cần tránh tạo thêm tải.",
        })

    schedule.sort(key=lambda item: (
        0 if item["must_not_defer"] else 1 if item["status"] == "scheduled_advisory" else 2,
        -item["age_hours"],
        item["pgid"],
    ))
    return {
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "status": "observed" if rows else "not_available",
        "generated_at": observed_at.isoformat(timespec="seconds") + "Z",
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
        "schedule": schedule[:MAX_ITEMS],
        "summary": {
            "pgs_scanned": min(len(rows), MAX_ITEMS),
            "due_count": len(schedule),
            "safety_overdue_count": sum(
                item["status"] == "safety_overdue" for item in schedule
            ),
            "deferred_busy_count": sum(
                item["status"] == "deferred_busy" for item in schedule
            ),
        },
        "maintenance_window": window,
        "evidence_gaps": evidence_gaps,
        "limits": {
            "max_items": MAX_ITEMS,
            "scrub_interval_days": scrub_days,
            "deep_scrub_interval_days": deep_days,
            "execution_commands_included": False,
            "scrub_flags_changed": False,
        },
    }
