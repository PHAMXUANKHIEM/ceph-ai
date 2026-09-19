"""Fail-closed normalization for the Block Storage health dependency view."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping


_BAD_STATE_TOKENS = ("down", "stale", "incomplete", "inconsistent", "peering")
_WARN_STATE_TOKENS = ("degraded", "undersized", "backfill", "recover")


def _rows(payload: object, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _osd_id(value: object) -> int | None:
    token = str(value or "")
    if token.startswith("osd."):
        token = token[4:]
    try:
        return int(token)
    except (TypeError, ValueError):
        return None


def _osd_topology(payload: object) -> dict[int, dict]:
    nodes = payload.get("nodes") if isinstance(payload, Mapping) else None
    if not isinstance(nodes, list):
        return {}
    by_id = {row.get("id"): row for row in nodes if isinstance(row, dict) and row.get("id") is not None}
    result: dict[int, dict] = {}
    for row in nodes:
        if not isinstance(row, dict) or row.get("type") != "host":
            continue
        host = str(row.get("name") or "unknown")
        for child_id in row.get("children") or []:
            child = by_id.get(child_id)
            osd_id = _osd_id(child.get("id") if isinstance(child, dict) else None)
            if osd_id is not None and isinstance(child, dict):
                result[osd_id] = {
                    "osd_id": osd_id, "host": host,
                    "status": str(child.get("status") or "unknown").lower(),
                }
    return result


def _pg_ids(row: Mapping[str, object], key: str) -> list[int]:
    value = row.get(key) or []
    if not isinstance(value, list):
        return []
    return [osd_id for value in value if (osd_id := _osd_id(value)) is not None]


def build_pool_dependency_health(
    pool: str,
    health_payload: object,
    pg_payload: object,
    osd_tree_payload: object,
    *,
    volume_count: int | None = None,
) -> dict:
    """Build pool-scoped dependency evidence; never claims exact volume→PG mapping."""
    health = health_payload if isinstance(health_payload, Mapping) else {}
    cluster_status = str(health.get("status") or health.get("overall_status") or "UNKNOWN")
    pg_rows = _rows(pg_payload, "pg_stats", "pgs")
    state_counts = Counter()
    affected: list[dict] = []
    involved_osds: set[int] = set()
    for row in pg_rows:
        state = str(row.get("state") or row.get("state_name") or "unknown").lower()
        state_counts[state] += 1
        acting = _pg_ids(row, "acting")
        up = _pg_ids(row, "up")
        involved_osds.update(acting or up)
        if any(token in state for token in _BAD_STATE_TOKENS + _WARN_STATE_TOKENS):
            affected.append({
                "pg_id": str(row.get("pgid") or row.get("pg_id") or "unknown"),
                "state": state, "acting": acting, "up": up,
                "primary": acting[0] if acting else (up[0] if up else None),
            })
    affected.sort(key=lambda row: ("down" not in row["state"], row["pg_id"]))
    topology = _osd_topology(osd_tree_payload)
    osd_rows = [topology[osd_id] for osd_id in sorted(involved_osds) if osd_id in topology]
    unknown_osds = sorted(involved_osds - set(topology))
    down_osds = [row["osd_id"] for row in osd_rows if row["status"] not in {"up", "in"}]
    hosts = sorted({row["host"] for row in osd_rows})
    down_hosts = sorted({row["host"] for row in osd_rows if row["osd_id"] in down_osds})
    bad_pg_count = sum(
        count for state, count in state_counts.items()
        if any(token in state for token in _BAD_STATE_TOKENS)
    )
    warn_pg_count = sum(
        count for state, count in state_counts.items()
        if any(token in state for token in _WARN_STATE_TOKENS)
    )
    if cluster_status.upper() in {"HEALTH_ERR", "ERR"} or bad_pg_count or down_osds:
        status = "CRITICAL"
    elif not pg_rows or not topology or unknown_osds:
        status = "INSUFFICIENT_EVIDENCE"
    elif cluster_status.upper() in {"HEALTH_WARN", "WARN"} or warn_pg_count or unknown_osds:
        status = "WARNING"
    elif pg_rows and topology:
        status = "HEALTHY"
    else:
        status = "INSUFFICIENT_EVIDENCE"
    gaps = []
    if not pg_rows:
        gaps.append("không đọc được danh sách PG của pool")
    if not topology:
        gaps.append("không đọc được CRUSH/OSD topology")
    if unknown_osds:
        gaps.append(f"chưa map được OSD: {', '.join(f'osd.{item}' for item in unknown_osds)}")
    return {
        "pool": pool,
        "status": status,
        "cluster_health": cluster_status,
        "volume_count": max(0, int(volume_count)) if volume_count is not None else None,
        "pg": {
            "total": len(pg_rows), "state_counts": dict(sorted(state_counts.items())),
            "affected_count": len(affected), "bad_count": bad_pg_count,
            "warning_count": warn_pg_count, "affected": affected[:50],
        },
        "osd": {
            "acting_set_osd_count": len(involved_osds),
            "acting_set_osd_ids": sorted(involved_osds),
            "down_osd_ids": sorted(down_osds), "hosts": hosts,
            "down_hosts": down_hosts,
        },
        "failure_domains": {"host_count": len(hosts), "hosts": hosts, "down_hosts": down_hosts},
        "dependency_scope": {
            "volume_to_pool": True,
            "pool_to_pg_osd": True,
            "exact_volume_to_pg": False,
            "note": "PG/OSD evidence is pool-scoped; exact volume→PG mapping requires object-level inspection and is not inferred here.",
        },
        "evidence": {"gaps": gaps},
        "read_only": True,
    }
