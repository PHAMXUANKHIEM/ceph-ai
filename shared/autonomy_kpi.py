"""Read-only KPIs for autonomous operations (autonomy plan WP0).

One place computes the numbers the autonomy plan is judged by: incident
volume per fault family, how often a family reopens right after it was
resolved, how many proposals are the ``investigate_manually`` placeholder,
and how many operator verdicts the learning loop receives.  Everything is
read with portable ORM queries (SQLite in tests, PostgreSQL in production)
and nothing is written.

``Incident`` has no ``resolved_at`` column; for RESOLVED incidents
``updated_at`` is used as the resolution time, which the report states.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_

from shared.models import Action, Incident, RemediationCase
from shared.time import utc_now

PLACEHOLDER_ACTION = "investigate_manually"
REOPEN_WINDOW = timedelta(minutes=30)
def fault_family(ceph_code: str | None) -> str:
    """Codes with an entity suffix ("NODE_UNREACHABLE:10.0.0.1",
    "OSD_LATENCY_HIGH:3") belong to one fault family."""
    return str(ceph_code or "UNKNOWN").split(":", 1)[0] or "UNKNOWN"


def _week(value: datetime) -> str:
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def _scoped(column, cluster_id: str | None, include_unscoped: bool):
    """Legacy rows with a NULL cluster belong to the default cluster."""
    if include_unscoped:
        return or_(column == cluster_id, column.is_(None))
    return column == cluster_id


def _incident_rows(session, since: datetime, cluster_id: str | None, include_unscoped: bool) -> list[Any]:
    # Only the columns the KPIs need: incidents carry large text/JSON columns
    # (log excerpts, evidence, postmortems) that made a full-row read of 30
    # days take minutes against the production database.
    query = session.query(
        Incident.id, Incident.cluster_id, Incident.ceph_code, Incident.status,
        Incident.created_at, Incident.updated_at,
    ).filter(Incident.created_at >= since)
    if cluster_id:
        query = query.filter(_scoped(Incident.cluster_id, cluster_id, include_unscoped))
    return query.order_by(Incident.created_at).all()


def reopen_count(incidents: list[Any], window: timedelta = REOPEN_WINDOW) -> int:
    """Incidents opened within ``window`` of the same code resolving."""
    last_resolved: dict[tuple[str | None, str], datetime] = {}
    reopened = 0
    for incident in sorted(incidents, key=lambda row: row.created_at):
        key = (incident.cluster_id, incident.ceph_code)
        resolved_at = last_resolved.get(key)
        if resolved_at is not None and timedelta(0) <= incident.created_at - resolved_at <= window:
            reopened += 1
        if incident.status == "RESOLVED" and incident.updated_at is not None:
            last_resolved[key] = incident.updated_at
    return reopened


def collect(session, *, days: int = 30, cluster_id: str | None = None,
            include_unscoped: bool = False, now: datetime | None = None) -> dict[str, Any]:
    current = (now or utc_now()).replace(tzinfo=None)
    since = current - timedelta(days=days)
    incidents = _incident_rows(session, since, cluster_id, include_unscoped)

    families = Counter(fault_family(row.ceph_code) for row in incidents)
    weekly: dict[str, Counter] = defaultdict(Counter)
    for row in incidents:
        weekly[_week(row.created_at)][fault_family(row.ceph_code)] += 1

    actions = session.query(Action.action_id, Action.status).filter(Action.created_at >= since)
    if cluster_id:
        actions = actions.join(Incident, Incident.id == Action.incident_id).filter(
            _scoped(Incident.cluster_id, cluster_id, include_unscoped)
        )
    action_rows = actions.all()
    placeholder = sum(1 for action_id, _status in action_rows if action_id == PLACEHOLDER_ACTION)

    cases = session.query(
        RemediationCase.created_at, RemediationCase.operator_verdict,
        RemediationCase.operator_verdict_at, RemediationCase.outcome,
    ).filter(RemediationCase.created_at >= since)
    if cluster_id:
        cases = cases.filter(_scoped(RemediationCase.cluster_id, cluster_id, include_unscoped))
    case_rows = cases.all()
    verdicts = Counter(row.operator_verdict for row in case_rows if row.operator_verdict)
    verdicts_weekly = Counter(
        _week(row.operator_verdict_at) for row in case_rows if row.operator_verdict and row.operator_verdict_at
    )
    latencies = sorted(
        (row.operator_verdict_at - row.created_at).total_seconds() / 3600
        for row in case_rows
        if row.operator_verdict_at and row.created_at and row.operator_verdict_at >= row.created_at
    )
    reopened = reopen_count(incidents)

    return {
        "schema": "ceph-ai.autonomy-kpi.v1",
        "generated_at": current.isoformat() + "Z",
        "window_days": days,
        "cluster_id": cluster_id,
        "incidents": {
            "total": len(incidents),
            "per_week": round(len(incidents) * 7 / days, 1) if days else None,
            "by_family": dict(families.most_common()),
            "weekly": {week: dict(counts.most_common()) for week, counts in sorted(weekly.items())},
            "reopened_within_30m": reopened,
            "reopen_rate": round(reopened / len(incidents), 4) if incidents else None,
            "resolution_time_note": "updated_at of RESOLVED incidents is used as the resolution time",
        },
        "actions": {
            "total": len(action_rows),
            "by_action": dict(Counter(action_id for action_id, _ in action_rows).most_common(15)),
            "by_status": dict(Counter(status for _, status in action_rows).most_common()),
            "investigate_manually": placeholder,
            "investigate_manually_rate": round(placeholder / len(action_rows), 4) if action_rows else None,
        },
        "verdicts": {
            "cases": len(case_rows),
            "labelled": sum(verdicts.values()),
            "by_verdict": dict(verdicts.most_common()),
            "weekly": dict(sorted(verdicts_weekly.items())),
            "median_hours_to_verdict": round(latencies[len(latencies) // 2], 2) if latencies else None,
            "verified_success": sum(1 for row in case_rows if row.outcome == "VERIFIED_SUCCESS"),
        },
    }
