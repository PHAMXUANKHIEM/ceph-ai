"""Deterministic capacity snapshots attached to Ceph health incidents."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable

from shared.models import Cluster
from watcher import ceph_client

logger = logging.getLogger(__name__)

CAPACITY_HEALTH_CODES = {
    "OSD_NEARFULL", "OSD_BACKFILLFULL", "OSD_FULL",
    "POOL_NEARFULL", "POOL_NEAR_FULL", "POOL_FULL", "BACKFILL_FULL",
}


def is_capacity_health_code(ceph_code: str) -> bool:
    return ceph_code in CAPACITY_HEALTH_CODES


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _query(cluster: Cluster | None, command: str):
    if cluster is None or cluster.is_default:
        return ceph_client.run_ceph_json_command(command)[1]
    nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    return ceph_client.run_ceph_json_command_with(
        nodes, cluster.ceph_container_name, cluster.ssh_user,
        cluster.ssh_key_path, cluster.ceph_exec_mode, command,
    )[1]


def _cluster_stats(payload) -> dict:
    stats = payload.get("stats", {}) if isinstance(payload, dict) else {}
    total = _number(stats.get("total_bytes"))
    used = _number(stats.get("total_used_bytes"))
    available = _number(stats.get("total_avail_bytes"))
    ratio = stats.get("total_used_raw_ratio")
    used_percent = _number(ratio) * 100 if ratio is not None else (used / total * 100 if total else 0)
    return {
        "total_bytes": int(total), "used_bytes": int(used),
        "available_bytes": int(available), "used_percent": round(used_percent, 3),
    }


def _pool_stats(payload) -> list[dict]:
    pools = payload.get("pools", []) if isinstance(payload, dict) else []
    result = []
    for row in pools:
        if not isinstance(row, dict):
            continue
        stats = row.get("stats", {}) if isinstance(row.get("stats"), dict) else {}
        used = _number(stats.get("bytes_used"))
        available = _number(stats.get("max_avail"))
        percent = stats.get("percent_used")
        used_percent = _number(percent) * (100 if _number(percent) <= 1 else 1)
        result.append({
            "pool": str(row.get("name") or row.get("pool_name") or "unknown"),
            "used_bytes": int(used), "max_available_bytes": int(available),
            "used_percent": round(used_percent, 3),
        })
    return sorted(result, key=lambda row: row["used_percent"], reverse=True)[:10]


def _osd_stats(payload) -> list[dict]:
    nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
    result = []
    for row in nodes:
        if not isinstance(row, dict):
            continue
        result.append({
            "osd_id": int(_number(row.get("id"), -1)),
            "used_percent": round(_number(row.get("utilization")), 3),
            "total_kb": int(_number(row.get("kb"))),
            "used_kb": int(_number(row.get("kb_used"))),
            "available_kb": int(_number(row.get("kb_avail"))),
        })
    return sorted(result, key=lambda row: row["used_percent"], reverse=True)[:10]


def collect_capacity_evidence(
    ceph_code: str, check_detail: dict, *, cluster: Cluster | None = None,
    query: Callable[[Cluster | None, str], object] = _query,
) -> str | None:
    """Return a credential-free JSON snapshot; telemetry failure is non-fatal."""
    if not is_capacity_health_code(ceph_code):
        return None
    evidence = {
        "source": "ceph_capacity_snapshot",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "ceph_code": ceph_code,
        "severity": check_detail.get("severity") if isinstance(check_detail, dict) else None,
    }
    try:
        df = query(cluster, "ceph df detail")
        evidence["cluster"] = _cluster_stats(df)
        evidence["pools"] = _pool_stats(df)
    except Exception as exc:
        logger.warning("capacity evidence: ceph df failed: %s", exc)
        evidence["df_available"] = False
    try:
        evidence["osds"] = _osd_stats(query(cluster, "ceph osd df"))
    except Exception as exc:
        logger.warning("capacity evidence: ceph osd df failed: %s", exc)
        evidence["osd_df_available"] = False
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True)
