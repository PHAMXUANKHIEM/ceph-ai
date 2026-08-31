"""Shared RBD trash TTL parsing.

Used by both the Dashboard's trash-view eligibility display
(``dashboard/routes/volumes.py``) and the Worker's live purge preflight
(``worker/llm/router_client.py``) so the two never drift apart on what
counts as "past its retention window".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def trash_entry_ttl_status(entry: dict, *, ttl_days: int, now: datetime | None = None) -> dict:
    """Classify one ``rbd trash ls`` entry against the configured TTL.

    Ceph's own status is authoritative for deferment (``protected until`` /
    ``expired``); its timestamp text is not ISO 8601, so eligibility is only
    derived from ``deletion_time``/``deleted_at`` when Ceph's status does not
    already answer the question.

    Returns a dict with:
      - ``kind``: ``"ceph_expired"``, ``"ceph_protected"``, ``"unknown"``, or ``"ttl_computed"``
      - ``purge_eligible``: bool
      - ``expires_at``: ``datetime | None`` (UTC, naive; only set for ``"ttl_computed"``)
      - ``detail``: Ceph's own protection detail text, only for ``"ceph_protected"``
      - ``remaining_seconds``: only set for ``"ttl_computed"``
    """
    status = str(entry.get("status") or "").strip()
    status_lower = status.lower()
    if status_lower.startswith("expired at") or status_lower == "expired":
        return {"kind": "ceph_expired", "purge_eligible": True, "expires_at": None, "detail": None}
    if status_lower.startswith("protected until"):
        return {
            "kind": "ceph_protected",
            "purge_eligible": False,
            "expires_at": None,
            "detail": status[len("protected until"):].strip(),
        }
    raw = str(entry.get("deletion_time") or entry.get("deleted_at") or "").strip()
    try:
        deleted_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if deleted_at.tzinfo is not None:
            deleted_at = deleted_at.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return {"kind": "unknown", "purge_eligible": False, "expires_at": None, "detail": None}
    expires_at = deleted_at + timedelta(days=ttl_days)
    remaining_seconds = (expires_at - (now or datetime.utcnow())).total_seconds()
    return {
        "kind": "ttl_computed",
        "purge_eligible": remaining_seconds <= 0,
        "expires_at": expires_at,
        "detail": None,
        "remaining_seconds": remaining_seconds,
    }
