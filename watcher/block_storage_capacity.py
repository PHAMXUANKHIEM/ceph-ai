"""Explainable, read-only capacity risk for the Block Storage overview.

The RBD inventory contains logical volume sizes while ``ceph df`` reports
physical pool capacity.  They answer different questions and must not be
silently added together.  This module keeps the distinction explicit and
fails closed when replica/EC overhead or fresh evidence is unavailable.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone


THRESHOLDS = {"warning": 80.0, "high": 90.0, "critical": 95.0}


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _percent(value: int, total: int) -> float:
    return round(value * 100.0 / total, 2) if total > 0 else 0.0


def _overhead(overview: Mapping[str, object]) -> dict:
    pool_type = str(overview.get("type") or overview.get("pool_type") or "").lower()
    if pool_type in {"replicated", "replica", "replication"}:
        replica_size = _int(overview.get("replica_size") or overview.get("size"))
        if replica_size > 0:
            return {
                "mode": "replicated",
                "known": True,
                "factor": float(replica_size),
                "label": f"Replica x{replica_size}",
                "reason": "Overhead raw được ước tính từ replica_size của pool.",
            }
        return {
            "mode": "replicated", "known": False, "factor": None,
            "label": "Replica chưa rõ", "reason": "Pool replicated nhưng thiếu replica_size.",
        }

    if pool_type in {"erasure", "erasure-coded", "erasure_coded", "ec"}:
        k = _int(
            overview.get("erasure_k") or overview.get("data_chunks")
            or overview.get("k")
        )
        m = _int(
            overview.get("erasure_m") or overview.get("coding_chunks")
            or overview.get("m")
        )
        if k > 0 and m >= 0:
            factor = (k + m) / k
            return {
                "mode": "erasure_coded", "known": True, "factor": round(factor, 4),
                "label": f"EC {k}+{m}",
                "reason": "Overhead raw được ước tính từ data/coding chunks.",
            }
        return {
            "mode": "erasure_coded", "known": False, "factor": None,
            "label": "EC overhead chưa rõ",
            "reason": "Có erasure_code_profile nhưng chưa có k/m; không suy đoán overhead.",
        }

    return {
        "mode": pool_type or "unknown", "known": False, "factor": None,
        "label": "Chưa xác định", "reason": "Không xác định được kiểu pool để tính overhead.",
    }


def _severity(physical_percent: float, overcommitted: bool, reserve_percent: float | None) -> str:
    if physical_percent >= THRESHOLDS["critical"]:
        return "CRITICAL"
    if physical_percent >= THRESHOLDS["high"]:
        return "HIGH"
    if physical_percent >= THRESHOLDS["warning"]:
        return "WATCH"
    if reserve_percent is not None:
        if reserve_percent <= 0:
            return "CRITICAL"
        if reserve_percent < 10:
            return "HIGH"
        if reserve_percent < 20:
            return "WATCH"
    if overcommitted:
        return "OVERCOMMITTED"
    return "HEALTHY"


def _failure_domain_reserve(
    failure_simulation: Mapping[str, object] | None,
    physical_total: int,
) -> dict:
    simulation = failure_simulation or {}
    scenarios = simulation.get("scenarios")
    if simulation.get("status") != "ready" or not isinstance(scenarios, list) or physical_total <= 0:
        return {
            "status": "INSUFFICIENT_EVIDENCE", "reserve_bytes": None,
            "reserve_percent": None, "worst_domain": None, "scenario_count": 0,
        }
    usable = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            continue
        remaining = _int(scenario.get("remaining_capacity_bytes"))
        usable.append((remaining, str(scenario.get("domain_name") or "unknown")))
    if not usable:
        return {
            "status": "INSUFFICIENT_EVIDENCE", "reserve_bytes": None,
            "reserve_percent": None, "worst_domain": None, "scenario_count": 0,
        }
    remaining, domain = min(usable, key=lambda item: item[0])
    reserve_percent = _percent(remaining, physical_total)
    return {
        "status": "CRITICAL" if reserve_percent <= 0 else "HIGH" if reserve_percent < 10 else "WATCH" if reserve_percent < 20 else "HEALTHY",
        "reserve_bytes": remaining,
        "reserve_percent": reserve_percent,
        "worst_domain": domain,
        "scenario_count": len(usable),
    }


def build_capacity_risk(
    pool: str,
    inventory_rows: Iterable[Mapping[str, object]],
    pool_overview: Mapping[str, object],
    *,
    inventory_state: Mapping[str, object] | None = None,
    forecast: Mapping[str, object] | None = None,
    failure_simulation: Mapping[str, object] | None = None,
    evidence_at: datetime | None = None,
) -> dict:
    """Build a bounded capacity contract without making any Ceph mutation."""
    overview = pool_overview if isinstance(pool_overview, Mapping) else {}
    rows = [row for row in inventory_rows if isinstance(row, Mapping)]
    physical_used = _int(overview.get("bytes_used") or overview.get("physical_used_bytes"))
    physical_available = _int(overview.get("max_available") or overview.get("max_avail"))
    physical_total = _int(overview.get("total_bytes")) or physical_used + physical_available
    physical_percent = _percent(physical_used, physical_total)
    logical_provisioned = sum(_int(row.get("provisioned_size") or row.get("size")) for row in rows)
    logical_used = sum(_int(row.get("used_size")) for row in rows)
    overhead = _overhead(overview)
    raw_equivalent = (
        int(logical_provisioned * float(overhead["factor"]))
        if overhead["known"] and overhead["factor"] is not None else None
    )
    comparable_provisioned = raw_equivalent if raw_equivalent is not None else logical_provisioned
    overcommitted = physical_total > 0 and comparable_provisioned > physical_total
    reserve = _failure_domain_reserve(failure_simulation, physical_total)
    status = (
        "INSUFFICIENT_EVIDENCE" if physical_total <= 0
        else _severity(physical_percent, overcommitted, reserve["reserve_percent"])
    )

    gaps: list[str] = []
    state = inventory_state or {}
    if not rows:
        gaps.append("RBD inventory chưa có volume nào hoặc chưa thu thập được")
    if state.get("stale"):
        gaps.append("inventory đang dùng snapshot cũ")
    if not overhead["known"]:
        gaps.append(overhead["reason"])
    if reserve["status"] == "INSUFFICIENT_EVIDENCE":
        gaps.append("chưa có đủ CRUSH/OSD evidence để tính reserve theo failure domain")
    if forecast is None:
        gaps.append("chưa đủ lịch sử capacity để dự báo 80/90/95%")

    recommendations: list[str] = []
    if physical_percent >= THRESHOLDS["critical"]:
        recommendations.append("Dừng tạo/resize volume mới và xử lý capacity trước khi pool chạm full.")
    elif physical_percent >= THRESHOLDS["high"]:
        recommendations.append("Ưu tiên giải phóng hoặc mở rộng raw capacity; pool đang ở mức cao.")
    elif physical_percent >= THRESHOLDS["warning"]:
        recommendations.append("Theo dõi tăng trưởng và chuẩn bị kế hoạch mở rộng trước ngưỡng 90%.")
    if overcommitted:
        recommendations.append("Logical provisioned vượt raw capacity tương đương; không cấp thêm volume nếu chưa có policy overcommit rõ ràng.")
    if reserve["status"] in {"HIGH", "CRITICAL"}:
        recommendations.append(f"Failure-domain reserve thấp nhất sau mô phỏng là {reserve['reserve_percent']:.1f}% tại {reserve['worst_domain']}.")
    if not recommendations:
        recommendations.append("Chưa phát hiện rủi ro capacity vượt ngưỡng từ evidence hiện có.")

    return {
        "pool": pool,
        "status": status,
        "thresholds": THRESHOLDS,
        "physical": {
            "used_bytes": physical_used, "available_bytes": physical_available,
            "total_bytes": physical_total, "used_percent": physical_percent,
        },
        "logical": {
            "image_count": len(rows), "provisioned_bytes": logical_provisioned,
            "used_bytes": logical_used, "used_percent": _percent(logical_used, logical_provisioned),
            "provisioned_percent_of_raw": _percent(logical_provisioned, physical_total),
            "raw_equivalent_provisioned_bytes": raw_equivalent,
            "raw_equivalent_provisioned_percent": _percent(comparable_provisioned, physical_total),
        },
        "overhead": overhead,
        "thin_provisioning": {
            "overcommitted": overcommitted,
            "ratio": round(logical_provisioned / physical_total, 4) if physical_total else None,
            "raw_equivalent_ratio": round(comparable_provisioned / physical_total, 4) if physical_total else None,
        },
        "failure_domain_reserve": reserve,
        "forecast": dict(forecast) if isinstance(forecast, Mapping) else None,
        "evidence": {
            "inventory_source": state.get("source") or "unknown",
            "inventory_age_seconds": state.get("age_seconds"),
            "inventory_stale": bool(state.get("stale")),
            "evidence_at": (evidence_at or datetime.now(timezone.utc).replace(tzinfo=None)).isoformat() + "Z",
            "gaps": gaps,
        },
        "recommendations": recommendations,
        "read_only": True,
    }
