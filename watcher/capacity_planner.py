"""Deterministic Ceph/Vitastor capacity and topology planner."""

from __future__ import annotations

import math
from typing import Any, Mapping


def _number(payload: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, number) if math.isfinite(number) else default


def _positive_int(payload: Mapping[str, Any], key: str, default: int) -> int:
    return max(1, int(_number(payload, key, default)))


def _scenario(
    *,
    name: str,
    logical_gib: float,
    headroom: float,
    replica: int | None,
    ec_k: int | None,
    ec_m: int | None,
    osd_capacity_gib: float,
    osds_per_node: int,
    minimum_failure_domains: int,
) -> dict[str, Any]:
    if replica:
        efficiency = 1.0 / replica
        redundancy = f"replica-{replica}"
    else:
        efficiency = ec_k / (ec_k + ec_m)
        redundancy = f"ec-{ec_k}+{ec_m}"
    raw_gib = logical_gib / efficiency / max(0.01, 1.0 - headroom)
    capacity_osds = math.ceil(raw_gib / osd_capacity_gib)
    capacity_nodes = math.ceil(capacity_osds / osds_per_node)
    nodes = max(capacity_nodes, minimum_failure_domains)
    return {
        "name": name,
        "redundancy": redundancy,
        "logical_gib": round(logical_gib, 2),
        "raw_required_gib": round(raw_gib, 2),
        "efficiency": round(efficiency, 4),
        "osds_for_capacity": capacity_osds,
        "nodes_for_capacity": nodes,
        "osds_per_node": osds_per_node,
        "headroom_percent": round(headroom * 100, 2),
        "cost": "unknown",
        "performance": "requires benchmark evidence",
    }


def plan_capacity(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, Mapping) else {}
    platform = str(source.get("platform") or "ceph").strip().lower()
    if platform not in {"ceph", "vitastor"}:
        raise ValueError("platform phải là ceph hoặc vitastor")
    logical_gib = _number(source, "provisioned_gib")
    if logical_gib <= 0:
        raise ValueError("provisioned_gib phải lớn hơn 0")
    growth = _number(source, "growth_percent_year")
    headroom = min(0.8, _number(source, "headroom_percent", 20) / 100)
    growth_adjusted = logical_gib * (1.0 + growth / 100.0)
    osd_capacity = _number(source, "osd_capacity_gib", 4 * 1024)
    if osd_capacity <= 0:
        raise ValueError("osd_capacity_gib phải lớn hơn 0")
    osds_per_node = _positive_int(source, "osds_per_node", 4)
    failure_domains = _positive_int(source, "failure_domain_count", 1)
    replica = _positive_int(source, "replica", 3)
    ec_k = _positive_int(source, "ec_k", 4)
    ec_m = _positive_int(source, "ec_m", 2)
    if ec_k + ec_m <= 1:
        raise ValueError("EC k+m không hợp lệ")
    iops = _number(source, "iops")
    throughput = _number(source, "throughput_mbps")
    iops_per_osd = _number(source, "iops_per_osd", 5000)
    throughput_per_osd = _number(source, "throughput_per_osd_mbps", 250)
    perf_osds = max(
        math.ceil(iops / iops_per_osd) if iops_per_osd > 0 else 0,
        math.ceil(throughput / throughput_per_osd) if throughput_per_osd > 0 else 0,
    )
    scenarios = [
        _scenario(
            name="replica",
            logical_gib=growth_adjusted,
            headroom=headroom,
            replica=replica,
            ec_k=None,
            ec_m=None,
            osd_capacity_gib=osd_capacity,
            osds_per_node=osds_per_node,
            minimum_failure_domains=max(failure_domains, replica),
        ),
        _scenario(
            name="erasure_coding",
            logical_gib=growth_adjusted,
            headroom=headroom,
            replica=None,
            ec_k=ec_k,
            ec_m=ec_m,
            osd_capacity_gib=osd_capacity,
            osds_per_node=osds_per_node,
            minimum_failure_domains=max(failure_domains, ec_m + 1),
        ),
    ]
    for scenario in scenarios:
        scenario["osds_for_performance"] = perf_osds or None
        scenario["osds_required"] = max(scenario["osds_for_capacity"], perf_osds)
        scenario["nodes_required"] = max(
            scenario["nodes_for_capacity"],
            math.ceil(max(scenario["osds_required"], 1) / osds_per_node),
            failure_domains,
        )

    warnings = []
    if failure_domains < 2:
        warnings.append({
            "code": "FAILURE_DOMAIN_SINGLETON",
            "severity": "high",
            "reason": "Chỉ có một failure domain; không chứng minh được chịu lỗi host/rack.",
        })
    if iops and not iops_per_osd:
        warnings.append({
            "code": "IOPS_BASELINE_MISSING",
            "severity": "medium",
            "reason": "Thiếu benchmark IOPS trên mỗi OSD.",
        })
    if throughput and not throughput_per_osd:
        warnings.append({
            "code": "THROUGHPUT_BASELINE_MISSING",
            "severity": "medium",
            "reason": "Thiếu benchmark throughput trên mỗi OSD.",
        })
    return {
        "status": "observed",
        "platform": platform,
        "workload": {
            "provisioned_gib": round(logical_gib, 2),
            "growth_percent_year": round(growth, 2),
            "capacity_horizon_gib": round(growth_adjusted, 2),
            "vm_count": _positive_int(source, "vm_count", 1),
            "volume_count": _positive_int(source, "volume_count", 1),
            "iops": round(iops, 2),
            "throughput_mbps": round(throughput, 2),
            "rpo_minutes": _number(source, "rpo_minutes"),
            "rto_minutes": _number(source, "rto_minutes"),
        },
        "scenarios": scenarios,
        "comparison": {
            "cost": "unknown_without_price_catalog",
            "durability": "replica and EC require explicit failure-domain validation",
            "performance": "unknown_without_real_media/network benchmark",
        },
        "warnings": warnings,
        "explainability": {
            "formula": "raw = logical / efficiency / (1 - headroom); osds = ceil(max(capacity, performance))",
            "assumptions": {
                "osd_capacity_gib": osd_capacity,
                "osds_per_node": osds_per_node,
                "failure_domain_count": failure_domains,
                "headroom_percent": round(headroom * 100, 2),
                "iops_per_osd": iops_per_osd,
                "throughput_per_osd_mbps": throughput_per_osd,
            },
            "confidence": "medium",
            "deterministic": True,
        },
        "read_only": True,
        "recommendation_mode": "ADVISORY",
        "action_id": None,
        "limits": {"commands_included": False, "costs_invented": False},
    }
