"""Fail-closed, read-only pool lifecycle posture for Block Storage."""
from __future__ import annotations

from collections.abc import Mapping


def _int_or_none(value: object) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _positive_or_none(value: object) -> int | None:
    parsed = _int_or_none(value)
    return parsed if parsed and parsed > 0 else None


def _application_tags(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    return sorted(str(key) for key in value if key)


def build_pool_lifecycle_inventory(
    pool: str,
    overview: Mapping[str, object] | None,
    *,
    volume_count: int | None = None,
    dependency: Mapping[str, object] | None = None,
    inventory_stale: bool = False,
) -> dict:
    """Build pool lifecycle evidence without enabling a mutation path.

    The result intentionally describes what an operator must review.  It does
    not claim that create/configure/delete is currently executable and it never
    recommends deleting a non-empty pool.
    """
    data = overview if isinstance(overview, Mapping) else {}
    applications = data.get("application_metadata")
    tags = _application_tags(applications)
    rbd_enabled = bool(data.get("rbd_enabled")) or "rbd" in tags
    pg_num = _int_or_none(data.get("pg_num"))
    pgp_num = _int_or_none(data.get("pgp_num"))
    # Ceph represents an unset pool quota as zero; expose that as unlimited.
    quota_max_bytes = _positive_or_none(data.get("quota_max_bytes"))
    quota_max_objects = _positive_or_none(data.get("quota_max_objects"))
    autoscale_mode = str(data.get("pg_autoscale_mode") or "UNKNOWN").lower()
    dependency_status = str((dependency or {}).get("status") or "UNKNOWN")
    gaps: list[str] = []
    blockers: list[str] = []

    if not data:
        gaps.append("không đọc được pool detail")
    if not rbd_enabled:
        gaps.append("pool chưa xác nhận application rbd")
    if pg_num is None or pg_num < 1:
        gaps.append("thiếu pg_num hợp lệ")
    if pgp_num is None or pgp_num < 1:
        gaps.append("thiếu pgp_num hợp lệ")
    if volume_count is None:
        gaps.append("chưa xác định được số volume phụ thuộc")
    elif volume_count > 0:
        blockers.append(f"pool còn {volume_count} volume; không được xóa trực tiếp")
    if dependency_status in {"CRITICAL", "INSUFFICIENT_EVIDENCE"}:
        blockers.append(f"dependency health: {dependency_status}")
    if dependency_status == "INSUFFICIENT_EVIDENCE":
        gaps.append("dependency evidence chưa đủ để đánh giá pool lifecycle")
    if inventory_stale:
        gaps.append("inventory volume đang stale; dependency count cần refresh")
    if autoscale_mode == "unknown":
        gaps.append("chưa xác định được pg autoscaler mode")

    if dependency_status == "CRITICAL":
        status = "BLOCKED"
    elif gaps:
        status = "INSUFFICIENT_EVIDENCE"
    elif blockers:
        status = "HAS_DEPENDENCIES"
    else:
        status = "REVIEWABLE"

    return {
        "pool": pool,
        "status": status,
        "application": {
            "rbd_enabled": rbd_enabled,
            "tags": tags,
        },
        "pg": {
            "pg_num": pg_num,
            "pgp_num": pgp_num,
            "autoscale_mode": autoscale_mode,
            "target_size_ratio": data.get("target_size_ratio"),
            "target_size_bytes": _int_or_none(data.get("target_size_bytes")),
        },
        "quota": {
            "max_bytes": quota_max_bytes,
            "max_objects": quota_max_objects,
            "configured": quota_max_bytes is not None or quota_max_objects is not None,
        },
        "dependencies": {
            "volume_count": volume_count,
            "health_status": dependency_status,
            "inventory_stale": inventory_stale,
            "exact_volume_to_pg": False,
        },
        "capabilities": {
            "read_only_inventory": True,
            "application_tag_observed": bool(tags),
            "quota_observed": quota_max_bytes is not None or quota_max_objects is not None,
            "autoscaler_observed": autoscale_mode != "unknown",
            "direct_mutation_supported": False,
            "worker_approval_required": True,
        },
        "operation_guard": {
            "create": "not_available_in_read_only_phase",
            "configure": "approval_required",
            "delete": "blocked_until_empty_and_approved" if volume_count else "approval_required",
        },
        "blockers": blockers,
        "evidence": {"gaps": gaps},
        "read_only": True,
    }
