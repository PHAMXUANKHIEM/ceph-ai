"""Read-only Pool/PG advisor built from normalized inventory snapshots."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

MAX_FINDINGS = 200
DEFAULT_TARGET_PGS_PER_OSD = 100
UNSAFE_STATE_TOKENS = {
    "degraded",
    "undersized",
    "stale",
    "peering",
    "incomplete",
    "inconsistent",
    "recovering",
    "backfilling",
    "remapped",
}


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _pool_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("pool") or "").strip()


def _state_is_unsafe(state: object) -> bool:
    normalized = str(state or "").strip().lower().replace(",", " ")
    return any(token in normalized for token in UNSAFE_STATE_TOKENS)


def _finding(
    *,
    code: str,
    severity: str,
    title: str,
    reason: str,
    pool: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "reason": reason,
        "target": {"type": "pool", "pool": pool},
        "evidence": evidence,
        "confidence": "high" if code == "POOL_PG_HEALTH_BLOCKS_TUNING" else "medium",
        "read_only": True,
        "action_id": None,
        "recommendation_mode": "ADVISORY",
        "recommendation": (
            "Ổn định PG và kiểm tra topology trước khi thay đổi PG/autoscaler."
            if code == "POOL_PG_HEALTH_BLOCKS_TUNING"
            else "Preview lại sau khi collector có đủ OSD, device class và failure-domain evidence."
        ),
    }


def build_pool_pg_advisor(
    *,
    cluster_id: str,
    cluster_name: str,
    pool_rows: list[dict[str, Any]] | None,
    pg_rows: list[dict[str, Any]] | None,
    target_pgs_per_osd: int = DEFAULT_TARGET_PGS_PER_OSD,
    osd_count: int | None = None,
    autoscaler_modes: dict[str, str] | None = None,
    device_classes: list[str] | None = None,
    failure_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Return bounded advisory findings; never emits a Ceph mutation command."""
    pools = [row for row in (pool_rows or []) if isinstance(row, dict)]
    pgs = [row for row in (pg_rows or []) if isinstance(row, dict)]
    target_density = max(1, int(target_pgs_per_osd))
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    observed_osds: set[str] = set()

    for row in pgs:
        name = str(row.get("pool") or "").strip()
        if name:
            by_pool[name].append(row)
        acting = row.get("acting")
        if isinstance(acting, list):
            observed_osds.update(str(osd) for osd in acting if osd is not None)

    effective_osd_count = _int(osd_count) or len(observed_osds)
    findings: list[dict[str, Any]] = []
    evidence_gaps: list[dict[str, str]] = []
    pool_results = []

    for row in pools:
        name = _pool_name(row)
        if not name:
            continue
        rows = by_pool.get(name, [])
        states = sorted({
            str(pg.get("state") or "unknown")
            for pg in rows
            if isinstance(pg, dict)
        })
        unsafe_states = [state for state in states if _state_is_unsafe(state)]
        configured_pg_num = _int(row.get("pgs"))
        replica_count = _int(row.get("size"))
        is_ec = str(row.get("redundancy") or "").strip().upper().startswith("EC")
        recommended_pg_num = None
        if configured_pg_num and effective_osd_count and replica_count and not is_ec:
            recommended_pg_num = max(
                1,
                round(effective_osd_count * target_density / replica_count),
            )
        result = {
            "pool": name,
            "configured_pg_num": configured_pg_num,
            "observed_pg_count": len(rows),
            "replica_count": replica_count,
            "erasure_coded": is_ec,
            "autoscaler_mode": (autoscaler_modes or {}).get(name),
            "unsafe_states": unsafe_states,
            "recommended_pg_num": recommended_pg_num,
            "read_only": True,
            "action_id": None,
        }
        pool_results.append(result)

        if unsafe_states:
            findings.append(_finding(
                code="POOL_PG_HEALTH_BLOCKS_TUNING",
                severity="high",
                title="PG đang không ổn định; chưa nên tối ưu PG",
                reason=f"Pool có PG state bất thường: {', '.join(unsafe_states)}.",
                pool=name,
                evidence={
                    "states": states,
                    "unsafe_states": unsafe_states,
                    "observed_pg_count": len(rows),
                },
            ))
        elif (
            configured_pg_num
            and recommended_pg_num
            and (
                configured_pg_num >= recommended_pg_num * 2
                or configured_pg_num * 2 <= recommended_pg_num
            )
        ):
            findings.append(_finding(
                code="POOL_PG_DENSITY_OUTLIER",
                severity="medium",
                title="PG count lệch đáng kể so với mật độ ước tính",
                reason=(
                    f"PG hiện tại {configured_pg_num}, ước tính "
                    f"{recommended_pg_num} theo {effective_osd_count} OSD và "
                    f"{replica_count} replica."
                ),
                pool=name,
                evidence={
                    "configured_pg_num": configured_pg_num,
                    "recommended_pg_num": recommended_pg_num,
                    "target_pgs_per_osd": target_density,
                    "osd_count": effective_osd_count,
                    "replica_count": replica_count,
                },
            ))

    if not pools and not pgs:
        evidence_gaps.append({
            "code": "POOL_PG_INVENTORY_UNAVAILABLE",
            "reason": "Chưa có pool/PG inventory để tư vấn.",
        })
    elif not pgs:
        evidence_gaps.append({
            "code": "PG_STATE_INVENTORY_UNAVAILABLE",
            "reason": "Có pool metadata nhưng chưa có PG state/acting evidence.",
        })
    if not effective_osd_count:
        evidence_gaps.append({
            "code": "OSD_TOPOLOGY_UNAVAILABLE",
            "reason": "Không có OSD topology; không thể ước tính mật độ PG.",
        })
    if not autoscaler_modes:
        evidence_gaps.append({
            "code": "AUTOSCALER_MODE_UNAVAILABLE",
            "reason": "Inventory hiện tại chưa cung cấp autoscaler mode theo pool.",
        })
    if not device_classes:
        evidence_gaps.append({
            "code": "DEVICE_CLASS_UNAVAILABLE",
            "reason": "Inventory hiện tại chưa cung cấp device class.",
        })
    if not failure_domains:
        evidence_gaps.append({
            "code": "FAILURE_DOMAIN_UNAVAILABLE",
            "reason": "Inventory hiện tại chưa cung cấp failure-domain topology.",
        })
    if any(result["erasure_coded"] for result in pool_results):
        evidence_gaps.append({
            "code": "EC_PROFILE_SIMULATION_UNAVAILABLE",
            "reason": "EC k/m, profile và data-movement evidence chưa đủ để đề xuất thay đổi.",
        })

    findings.sort(key=lambda item: (
        {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(item["severity"], 9),
        item["target"]["pool"],
    ))
    findings = findings[:MAX_FINDINGS]
    return {
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "status": "observed" if pools or pgs else "not_available",
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
        "assumptions": {
            "target_pgs_per_osd": target_density,
            "movement_estimate": "unknown",
            "failure_domain_risk": "unknown",
        },
        "summary": {
            "pools_scanned": len(pool_results),
            "pgs_scanned": len(pgs),
            "osd_count": effective_osd_count or None,
            "finding_count": len(findings),
        },
        "pools": pool_results,
        "findings": findings,
        "evidence_gaps": evidence_gaps,
        "limits": {
            "max_findings": MAX_FINDINGS,
            "mutation_commands_included": False,
        },
    }
