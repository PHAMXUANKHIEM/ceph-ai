"""Read-only shadow/canary soak acceptance checks."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone


def _get(row, name, default=None):
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def evaluate_shadow_soak(
    comparisons: Iterable[object], *, minimum_evaluations: int = 20,
    minimum_duration_hours: float = 24.0, now: datetime | None = None,
) -> dict:
    """Return PASS/HOLD evidence; never mutate registry or runtime state."""

    rows = list(comparisons)
    reference = now or datetime.now(timezone.utc)
    targets = [_get(row, "latest_target_at") for row in rows if _get(row, "latest_target_at") is not None]
    oldest = min(targets) if targets else None
    newest = max(targets) if targets else None
    if oldest and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    if newest and newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    duration_hours = ((newest - oldest).total_seconds() / 3600.0) if oldest and newest else 0.0
    evaluations = sum(min(int(_get(row, "active_evaluated", 0) or 0), int(_get(row, "candidate_evaluated", 0) or 0)) for row in rows)
    drifted = sum(_get(row, "candidate_drift_status") == "DRIFT" for row in rows)
    resource_failures = sum(_get(row, "resource_budget_ok", True) is False for row in rows)
    statuses = {str(_get(row, "status", "UNKNOWN")) for row in rows}
    checks = {
        "minimum_evaluations": evaluations >= max(1, int(minimum_evaluations)),
        "minimum_duration": duration_hours >= max(0.0, float(minimum_duration_hours)),
        "no_drift": drifted == 0,
        "resource_budget": resource_failures == 0,
        "shadow_only": all(str(_get(row, "execution_mode", "SHADOW_ONLY")) == "SHADOW_ONLY" for row in rows),
    }
    return {
        "status": "PASS" if rows and all(checks.values()) else "HOLD",
        "checks": checks,
        "comparison_count": len(rows),
        "evaluations": evaluations,
        "duration_hours": round(duration_hours, 3),
        "drifted_comparisons": drifted,
        "resource_budget_failures": resource_failures,
        "statuses": sorted(statuses),
        "side_effects": "read-only; no promotion, notification, alert or remediation",
        "checked_at": reference.isoformat(),
    }
