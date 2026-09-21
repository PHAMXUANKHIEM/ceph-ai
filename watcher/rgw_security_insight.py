"""Deterministic, secret-safe RGW security insights.

This is the first read-only slice of the AI Security Insight roadmap. It
does not execute actions and deliberately reports evidence gaps instead of
guessing about bucket ACLs, policies, quotas, or audit coverage that are not
present in the current inventory contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.time import utc_now

DEFAULT_KEY_ROTATION_DAYS = 90
MAX_FINDINGS = 200


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _age_days(created_at: object, now: datetime) -> int | None:
    parsed = _parse_datetime(created_at)
    if parsed is None:
        return None
    age = (now - parsed).total_seconds()
    if age < 0:
        return 0
    return int(age // 86400)


def _finding(uid: str, age_days: int, threshold_days: int, active_key_count: int) -> dict[str, Any]:
    return {
        "code": "ACCESS_KEY_ROTATION_GAP",
        "severity": "medium",
        "title": "Access key quá hạn xoay vòng",
        "reason": (
            f"User có access key active đã {age_days} ngày, vượt ngưỡng "
            f"{threshold_days} ngày."
        ),
        "target": {"type": "s3_user", "uid": uid},
        "evidence": {
            "key_age_days": age_days,
            "rotation_threshold_days": threshold_days,
            "active_key_count": active_key_count,
        },
        "confidence": "high",
        "recommendation": (
            "Kiểm tra client đang dùng key, tạo key mới, xác nhận client hoạt động "
            "rồi mới revoke key cũ."
        ),
        "read_only": True,
        "action_id": None,
    }


def build_security_insights(
    *,
    cluster_id: str,
    cluster_name: str,
    user_snapshot: dict[str, Any] | None,
    now: datetime | None = None,
    key_rotation_days: int = DEFAULT_KEY_ROTATION_DAYS,
    stale: bool = False,
    evidence_age_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Build bounded advisory findings from the existing S3 user snapshot.

    Only masked inventory metadata is consumed. Raw access keys, secret keys,
    policies, ACLs and command output are intentionally outside this contract.
    """
    observed_at = now or utc_now()
    if observed_at.tzinfo is not None:
        observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    threshold = max(1, int(key_rotation_days))
    snapshot = user_snapshot if isinstance(user_snapshot, dict) else {}
    raw_items = snapshot.get("items")
    items = raw_items if isinstance(raw_items, list) else []

    findings: list[dict[str, Any]] = []
    evidence_gaps: list[dict[str, str]] = []
    unavailable_users = 0
    keys_scanned = 0
    active_keys_scanned = 0
    missing_key_timestamps = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid") or "").strip()
        if not uid:
            continue
        if item.get("unavailable"):
            unavailable_users += 1
            continue

        keys = item.get("access_keys")
        keys = keys if isinstance(keys, list) else []
        active_keys = []
        for key in keys:
            if not isinstance(key, dict):
                continue
            keys_scanned += 1
            status = str(key.get("status") or "active").strip().lower()
            if status not in {"revoked", "disabled", "inactive"}:
                active_keys.append(key)
        active_keys_scanned += len(active_keys)
        for key in active_keys:
            age_days = _age_days(key.get("created_at"), observed_at)
            if age_days is None:
                missing_key_timestamps += 1
                continue
            if age_days >= threshold and len(findings) < MAX_FINDINGS:
                findings.append(_finding(uid, age_days, threshold, len(active_keys)))

    if not items:
        evidence_gaps.append({
            "code": "S3_USER_INVENTORY_UNAVAILABLE",
            "reason": "Chưa có user inventory đã xác thực để phân tích.",
        })
    if bool(snapshot.get("refreshing")):
        evidence_gaps.append({
            "code": "S3_USER_INVENTORY_REFRESHING",
            "reason": "RGW user inventory đang được làm mới; kết quả có thể chưa đầy đủ.",
        })
    if unavailable_users:
        evidence_gaps.append({
            "code": "S3_USER_METADATA_UNAVAILABLE",
            "reason": f"Không đọc được metadata của {unavailable_users} user.",
        })
    if missing_key_timestamps:
        evidence_gaps.append({
            "code": "ACCESS_KEY_CREATED_AT_MISSING",
            "reason": (
                f"Không có created_at của {missing_key_timestamps} active key; "
                "không kết luận được tuổi key."
            ),
        })
    evidence_gaps.append({
        "code": "RGW_SECURITY_SURFACE_NOT_YET_COLLECTED",
        "reason": (
            "ACL/policy public, quota bất thường, audit coverage và unused-key "
            "analysis chưa có evidence trong collector hiện tại."
        ),
    })
    if stale:
        evidence_gaps.append({
            "code": "S3_USER_INVENTORY_STALE",
            "reason": "Snapshot đang cũ hơn TTL; cần refresh trước khi hành động.",
        })

    findings.sort(key=lambda row: (
        {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(
            row.get("severity"), 9
        ),
        str(row.get("target", {}).get("uid", "")),
        -int(row.get("evidence", {}).get("key_age_days", 0)),
    ))
    return {
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "status": "observed" if items else "not_available",
        "generated_at": observed_at.isoformat(timespec="seconds") + "Z",
        "evidence_age_seconds": evidence_age_seconds,
        "stale": bool(stale),
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
        "summary": {
            "users_scanned": len(items) - unavailable_users,
            "users_unavailable": unavailable_users,
            "keys_scanned": keys_scanned,
            "active_keys_scanned": active_keys_scanned,
            "rotation_gap_count": sum(
                1 for finding in findings
                if finding["code"] == "ACCESS_KEY_ROTATION_GAP"
            ),
        },
        "findings": findings,
        "evidence_gaps": evidence_gaps,
        "limits": {
            "max_findings": MAX_FINDINGS,
            "key_rotation_days": threshold,
            "secrets_included": False,
        },
    }
