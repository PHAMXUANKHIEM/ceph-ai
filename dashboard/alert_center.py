"""Presentation helpers for the grouped alert centre.

The watcher remains the source of truth for incident creation and Telegram
rate limiting.  This module only groups persisted rows for the operator UI;
it never changes an Incident or suppresses an alert at the source.
"""

from collections import OrderedDict
from math import ceil


OPEN_STATUSES = {
    "NEW", "DIAGNOSING", "PENDING_APPROVAL", "APPROVED", "EXECUTING",
    "GRACE_PENDING", "VERIFYING", "FAILED",
}


def build_alert_groups(incidents, *, max_groups: int | None = None) -> list[dict]:
    """Return one representative row per recurring alert group.

    A deterministic ``group_root_incident_id`` wins when available.  Legacy
    rows without grouping metadata fall back to ``cluster_id + ceph_code``;
    this is deliberately conservative and makes repeated alerts visible as a
    single history row without pretending that they are a new root cause.
    """
    groups = OrderedDict()
    for incident in incidents:
        root_id = getattr(incident, "group_root_incident_id", None)
        cluster_id = getattr(incident, "cluster_id", None) or "default"
        code = getattr(incident, "ceph_code", "")
        key = ("root", root_id) if root_id else ("code", cluster_id, code)
        group = groups.get(key)
        if group is None:
            group = {
                "key": ":".join(str(value) for value in key),
                "ceph_code": code,
                "representative": incident,
                "incidents": [],
                "occurrence_count": 0,
                "active_count": 0,
                "first_seen": incident.detected_at,
                "last_seen": incident.detected_at,
            }
            groups[key] = group
        group["incidents"].append(incident)
        group["occurrence_count"] += 1
        if incident.detected_at > group["representative"].detected_at:
            group["representative"] = incident
        if incident.status in OPEN_STATUSES:
            group["active_count"] += 1
        group["first_seen"] = min(group["first_seen"], incident.detected_at)
        group["last_seen"] = max(group["last_seen"], incident.detected_at)

    result = list(groups.values())
    for group in result:
        group["is_active"] = group["active_count"] > 0
        group["merged_count"] = max(group["occurrence_count"] - 1, 0)
    return result if max_groups is None else result[:max_groups]


def paginate_alert_groups(groups: list[dict], *, page: int = 1, page_size: int = 20) -> dict:
    """Return a bounded page without silently dropping any group."""
    page_size = max(1, page_size)
    total_groups = len(groups)
    total_pages = max(1, ceil(total_groups / page_size))
    page = min(max(1, page), total_pages)
    start = (page - 1) * page_size
    return {
        "items": groups[start:start + page_size],
        "page": page,
        "page_size": page_size,
        "total_groups": total_groups,
        "total_pages": total_pages,
    }
