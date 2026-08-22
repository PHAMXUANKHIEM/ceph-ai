"""Read-only Ceph capacity simulation for loss of an OSD/host/rack."""
from __future__ import annotations

import json
from datetime import timezone

from shared import db
from shared.models import CephCapacitySample, CrushOsdDistribution, CrushStructureSnapshot

THRESHOLDS = (80, 90, 95)


def _descendant_osds(node: dict) -> set[int]:
    if node.get("type") == "osd":
        try:
            return {int(node.get("id"))}
        except (TypeError, ValueError):
            return set()
    result: set[int] = set()
    for child in node.get("children") or []:
        result.update(_descendant_osds(child))
    return result


def _failure_domains(tree: dict) -> list[tuple[str, str, set[int]]]:
    domains: list[tuple[str, str, set[int]]] = []

    def walk(node: dict) -> None:
        kind = str(node.get("type") or "")
        osds = _descendant_osds(node)
        if kind in {"osd", "host", "rack"} and osds:
            name = str(node.get("name") or (f"osd.{next(iter(osds))}" if kind == "osd" else "unknown"))
            domains.append((kind, name, osds))
        for child in node.get("children") or []:
            walk(child)

    for root in tree.get("roots") or []:
        walk(root)
    return domains


def _latest_pool_samples(session, cluster_id: str) -> list[CephCapacitySample]:
    rows = session.query(CephCapacitySample).filter_by(
        cluster_id=cluster_id, entity_type="pool",
    ).order_by(CephCapacitySample.captured_at.desc()).all()
    latest = {}
    for row in rows:
        latest.setdefault(row.entity_name, row)
    return list(latest.values())


def _scenario(kind: str, name: str, lost_ids: set[int], rows: list, pools: list) -> dict | None:
    lost = [row for row in rows if row.osd_id in lost_ids]
    survivors = [row for row in rows if row.osd_id not in lost_ids]
    if not lost or any(row.bytes_total is None or row.bytes_used is None for row in rows):
        return None
    total_bytes = sum(row.bytes_total for row in rows)
    total_used = sum(row.bytes_used for row in rows)
    remaining_bytes = sum(row.bytes_total for row in survivors)
    lost_used = sum(row.bytes_used for row in lost)
    if not survivors:
        return {
            "domain_type": kind, "domain_name": name, "lost_osd_ids": sorted(lost_ids),
            "lost_capacity_bytes": sum(row.bytes_total for row in lost),
            "remaining_capacity_bytes": 0, "cluster_projected_percent": 100.0,
            "max_osd_projected_percent": 100.0, "highest_threshold": 95,
            "additional_bytes_for_80_percent": int(total_used / .8), "osds_at_risk": [],
            "pools_at_risk": [{"pool": row.entity_name, "current_percent": round(row.used_percent, 3),
                               "projected_percent": 100.0} for row in pools],
            "catastrophic": True,
        }
    free_by_osd = {row.osd_id: max(0, row.bytes_total - row.bytes_used) for row in survivors}
    total_free = sum(free_by_osd.values())
    projected_osds = []
    for row in survivors:
        recovery = lost_used * free_by_osd[row.osd_id] / total_free if total_free else lost_used / len(survivors)
        percent = (row.bytes_used + recovery) / row.bytes_total * 100 if row.bytes_total else 100.0
        projected_osds.append({"osd_id": row.osd_id, "projected_percent": round(percent, 3)})
    post_percent = total_used / remaining_bytes * 100 if remaining_bytes else 100.0
    current_percent = total_used / total_bytes * 100 if total_bytes else 0.0
    multiplier = post_percent / current_percent if current_percent else 1.0
    projected_pools = [{
        "pool": row.entity_name,
        "current_percent": round(row.used_percent, 3),
        "projected_percent": round(min(100.0, row.used_percent * multiplier), 3),
    } for row in pools if row.used_percent * multiplier >= 80]
    max_osd = max(item["projected_percent"] for item in projected_osds)
    return {
        "domain_type": kind, "domain_name": name, "lost_osd_ids": sorted(lost_ids),
        "lost_capacity_bytes": sum(row.bytes_total for row in lost),
        "remaining_capacity_bytes": remaining_bytes,
        "cluster_projected_percent": round(post_percent, 3),
        "max_osd_projected_percent": round(max_osd, 3),
        "highest_threshold": max((value for value in THRESHOLDS if max(post_percent, max_osd) >= value), default=None),
        "additional_bytes_for_80_percent": max(0, int(total_used / .8 - remaining_bytes)),
        "osds_at_risk": sorted(
            [item for item in projected_osds if item["projected_percent"] >= 80],
            key=lambda item: item["projected_percent"], reverse=True,
        ),
        "pools_at_risk": sorted(projected_pools, key=lambda item: item["projected_percent"], reverse=True),
    }


def simulate(cluster_id: str) -> dict:
    with db.SessionLocal() as session:
        snapshot = session.query(CrushStructureSnapshot).filter_by(cluster_id=cluster_id).order_by(
            CrushStructureSnapshot.created_at.desc()).first()
        rows = session.query(CrushOsdDistribution).filter_by(cluster_id=cluster_id).all()
        pools = _latest_pool_samples(session, cluster_id)
        if snapshot is None or not rows:
            return {"status": "unavailable", "reason": "missing_crush_or_osd_distribution", "scenarios": []}
        tree = json.loads(snapshot.tree_json)
        scenarios = [value for kind, name, ids in _failure_domains(tree)
                     if (value := _scenario(kind, name, ids, rows, pools)) is not None]
        scenarios.sort(key=lambda item: (
            item["max_osd_projected_percent"], item["cluster_projected_percent"]
        ), reverse=True)
        captured = snapshot.created_at.replace(tzinfo=timezone.utc).isoformat()
        distribution_at = max(row.updated_at for row in rows).replace(tzinfo=timezone.utc).isoformat()
        return {
            "status": "ready", "captured_at": captured, "distribution_at": distribution_at,
            "assumption": "lost raw replicas are rebuilt across surviving OSDs proportional to free space",
            "data_availability_verified": False,
            "limitations": "Capacity-only simulation; pool size/min_size and PG acting sets are not proof of data availability.",
            "scenario_count": len(scenarios), "scenarios": scenarios,
            "_citations": [{"source_id": f"crush-snapshot:{snapshot.id}", "observed_at": captured,
                            "confidence": 1.0, "source_type": "crush_topology"},
                           {"source_id": f"osd-distribution:{cluster_id}", "observed_at": distribution_at,
                            "confidence": 1.0, "source_type": "osd_capacity"}],
        }
