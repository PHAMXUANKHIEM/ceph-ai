"""Read-only data-integrity evidence for one RBD volume."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone


def _rows(payload: object, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _text(value: object) -> str:
    if isinstance(value, Mapping):
        return " ".join(str(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return str(value or "")


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _health_findings(payload: object) -> list[dict]:
    health = payload if isinstance(payload, Mapping) else {}
    findings = []
    for code, check in (health.get("checks") or {}).items() if isinstance(health.get("checks"), Mapping) else []:
        text = _text(check)
        lowered = f"{code} {text}".lower()
        if any(token in lowered for token in ("scrub", "inconsisten", "damaged", "unfound", "missing")):
            findings.append({
                "code": str(code),
                "severity": str(check.get("severity") if isinstance(check, Mapping) else "HEALTH_WARN"),
                "summary": text[:500],
            })
    return findings


def _pg_evidence(payload: object) -> dict:
    rows = _rows(payload, "pg_stats", "pgs")
    affected = []
    missing_scrub = 0
    missing_deep = 0
    latest_scrub: datetime | None = None
    latest_deep: datetime | None = None
    for row in rows:
        scrub = row.get("last_scrub_stamp") or row.get("last_scrub")
        deep = row.get("last_deep_scrub_stamp") or row.get("last_deep_scrub")
        scrub_at = _timestamp(scrub)
        deep_at = _timestamp(deep)
        latest_scrub = max((latest_scrub, scrub_at), key=lambda item: item or datetime.min.replace(tzinfo=timezone.utc))
        latest_deep = max((latest_deep, deep_at), key=lambda item: item or datetime.min.replace(tzinfo=timezone.utc))
        if scrub and scrub_at is None:
            missing_scrub += 1
        elif not scrub:
            missing_scrub += 1
        if deep and deep_at is None:
            missing_deep += 1
        elif not deep:
            missing_deep += 1
        state = str(row.get("state") or "unknown").lower()
        if any(token in state for token in ("inconsistent", "stale", "incomplete", "unknown")):
            affected.append({
                "pg_id": str(row.get("pgid") or row.get("pg_id") or "unknown"),
                "state": state,
            })
    return {
        "pg_count": len(rows),
        "affected_count": len(affected),
        "affected": affected[:50],
        "missing_last_scrub": missing_scrub,
        "missing_last_deep_scrub": missing_deep,
        "latest_scrub": latest_scrub.isoformat() if latest_scrub else None,
        "latest_deep_scrub": latest_deep.isoformat() if latest_deep else None,
    }


def build_integrity_evidence(
    image_detail: Mapping[str, object] | None,
    health_payload: object,
    pg_payload: object,
    backup_summary: Mapping[str, object] | None,
    *,
    incidents: Iterable[Mapping[str, object]] = (),
) -> dict:
    """Build a conservative integrity report; no repair or destructive action."""
    detail = image_detail if isinstance(image_detail, Mapping) else {}
    health = health_payload if isinstance(health_payload, Mapping) else {}
    health_findings = _health_findings(health)
    pg = _pg_evidence(pg_payload)
    backup = backup_summary if isinstance(backup_summary, Mapping) else {}
    latest_success = backup.get("latest_success") if isinstance(backup.get("latest_success"), Mapping) else {}
    checksum_available = bool(latest_success.get("sha256_present"))
    checksum_source = latest_success.get("job_type") if checksum_available else None
    gaps: list[str] = []
    if not detail:
        gaps.append("không đọc được rbd info/status")
    if not pg["pg_count"]:
        gaps.append("không có PG evidence cho pool")
    if pg["missing_last_scrub"]:
        gaps.append(f"{pg['missing_last_scrub']} PG thiếu last scrub timestamp")
    if pg["missing_last_deep_scrub"]:
        gaps.append(f"{pg['missing_last_deep_scrub']} PG thiếu last deep-scrub timestamp")
    if not checksum_available:
        gaps.append("chưa có checksum backup/restore artifact để xác minh byte-level")
    open_incidents = [dict(row) for row in incidents if isinstance(row, Mapping)]
    if health_findings or pg["affected_count"]:
        status = "CRITICAL"
    elif str(health.get("status") or "").upper() in {"HEALTH_ERR", "ERR"}:
        status = "CRITICAL"
    elif gaps or str(health.get("status") or "").upper() in {"HEALTH_WARN", "WARN"}:
        status = "WARNING"
    else:
        status = "HEALTHY"
    if not detail or not pg["pg_count"]:
        status = "INSUFFICIENT_EVIDENCE"
    return {
        "status": status,
        "health": {
            "status": health.get("status") or "UNKNOWN",
            "findings": health_findings,
        },
        "scrub": pg,
        "read_path": {
            "rbd_info_available": bool(detail),
            "watcher_count": len(detail.get("watchers") or []) if isinstance(detail.get("watchers"), list) else None,
            "lock_count": len(detail.get("locks") or []) if isinstance(detail.get("locks"), list) else None,
            "full_export_performed": False,
        },
        "checksum": {
            # This is a verified backup/restore artifact checksum, not a
            # fresh checksum of the live RBD image.  Do not overstate it as a
            # current-volume integrity proof without an explicit export.
            "status": "AVAILABLE" if checksum_available else "NOT_AVAILABLE",
            "source": checksum_source,
            "artifact_byte_level": checksum_available,
            "current_volume_verified": False,
        },
        "incidents": {"open_count": len(open_incidents), "items": open_incidents[:20]},
        "evidence_gaps": gaps,
        "repair": {
            "automatic_repair": False,
            "repair_supported": False,
            "recommendation": "Chỉ mở repair sau khi operator review evidence và approval; không tự chạy pg repair hoặc scrub repair.",
        },
        "read_only": True,
    }
