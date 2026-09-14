"""Deterministic capacity snapshots attached to Ceph health incidents."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Callable

from shared.models import Cluster
from watcher import ceph_client

logger = logging.getLogger(__name__)

CAPACITY_HEALTH_CODES = {
    "OSD_NEARFULL", "OSD_BACKFILLFULL", "OSD_FULL",
    "POOL_NEARFULL", "POOL_NEAR_FULL", "POOL_FULL", "BACKFILL_FULL",
}
_POOL_DETAIL_PATTERNS = (
    re.compile(r"\bpool ['\"]([^'\"]+)['\"]"),
    re.compile(r"\bpool ([A-Za-z0-9_.:-]+)\b"),
)


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


def _pool_stats(payload, limit: int | None = 10) -> list[dict]:
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
    ordered = sorted(result, key=lambda row: row["used_percent"], reverse=True)
    return ordered[:limit] if limit is not None else ordered


def _osd_host_map(payload) -> dict[int, str]:
    """Extract osd_id -> CRUSH host name from ``ceph osd tree`` JSON."""
    nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
    osd_to_host: dict[int, str] = {}
    for row in nodes:
        if not isinstance(row, dict) or row.get("type") != "host":
            continue
        host = str(row.get("name") or "").strip()
        if not host:
            continue
        for child in row.get("children") or []:
            osd_id = int(_number(child, -1))
            if osd_id >= 0:
                osd_to_host[osd_id] = host
    return osd_to_host


def _osd_stats(payload, limit: int | None = 10, osd_hosts: dict[int, str] | None = None) -> list[dict]:
    nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
    result = []
    for row in nodes:
        if not isinstance(row, dict):
            continue
        osd_id = int(_number(row.get("id"), -1))
        result.append({
            "osd_id": osd_id,
            "host": (osd_hosts or {}).get(osd_id),
            "used_percent": round(_number(row.get("utilization")), 3),
            "total_kb": int(_number(row.get("kb"))),
            "used_kb": int(_number(row.get("kb_used"))),
            "available_kb": int(_number(row.get("kb_avail"))),
        })
    ordered = sorted(result, key=lambda row: row["used_percent"], reverse=True)
    return ordered[:limit] if limit is not None else ordered


def _pool_names_in_detail(check_detail: dict) -> set[str]:
    if not isinstance(check_detail, dict):
        return set()
    names: set[str] = set()
    entries = check_detail.get("detail") or []
    messages = [d.get("message", "") for d in entries if isinstance(d, dict)]
    summary = check_detail.get("summary")
    if isinstance(summary, dict):
        messages.append(summary.get("message", ""))
    for message in messages:
        for pattern in _POOL_DETAIL_PATTERNS:
            names.update(match.strip() for match in pattern.findall(message) if match.strip())
    return names


def format_capacity_alert_context(evidence_json: str | None, *, max_pools: int = 5, max_osds: int = 5) -> str | None:
    """Human-facing summary for full/nearfull alerts.

    The stored JSON remains the auditable source; this short text is appended
    to Telegram/log excerpts so operators immediately see which pool is under
    pressure and which OSD is on which node.
    """
    if not evidence_json:
        return None
    try:
        evidence = json.loads(evidence_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(evidence, dict) or evidence.get("source") != "ceph_capacity_snapshot":
        return None

    lines = ["Dung lượng chi tiết:"]
    cluster = evidence.get("cluster")
    if isinstance(cluster, dict) and "used_percent" in cluster:
        lines.append(f"- Toàn cụm: {float(cluster.get('used_percent') or 0):.2f}% đã dùng")

    pools = [row for row in evidence.get("pools") or [] if isinstance(row, dict)]
    if pools:
        parts = []
        for row in pools[:max_pools]:
            parts.append(f"{row.get('pool', 'unknown')} {float(row.get('used_percent') or 0):.2f}%")
        lines.append(f"- Pool áp lực: {', '.join(parts)}")

    osds = [row for row in evidence.get("osds") or [] if isinstance(row, dict)]
    if osds:
        parts = []
        for row in osds[:max_osds]:
            osd_id = row.get("osd_id", "unknown")
            host = row.get("host") or "chưa xác định node"
            parts.append(f"osd.{osd_id} trên {host} {float(row.get('used_percent') or 0):.2f}%")
        lines.append(f"- OSD áp lực: {', '.join(parts)}")

    if len(lines) == 1:
        return None
    return "\n".join(lines)


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
        pools = _pool_stats(df)
        named_pools = _pool_names_in_detail(check_detail)
        if named_pools:
            pools = sorted(
                pools,
                key=lambda row: (row["pool"] not in named_pools, -row["used_percent"]),
            )
        evidence["pools"] = pools
    except Exception as exc:
        logger.warning("capacity evidence: ceph df failed: %s", exc)
        evidence["df_available"] = False
    try:
        osd_hosts: dict[int, str] = {}
        try:
            osd_hosts = _osd_host_map(query(cluster, "ceph osd tree"))
        except Exception as exc:
            logger.warning("capacity evidence: ceph osd tree failed: %s", exc)
            evidence["osd_tree_available"] = False
        evidence["osds"] = _osd_stats(query(cluster, "ceph osd df"), osd_hosts=osd_hosts)
    except Exception as exc:
        logger.warning("capacity evidence: ceph osd df failed: %s", exc)
        evidence["osd_df_available"] = False
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True)
