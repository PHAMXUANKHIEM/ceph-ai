"""Read-only, cacheable RGW topology and health evidence.

This module deliberately returns evidence sections independently.  RGW
deployments differ between cephadm and legacy systemd, and an unavailable
``sync status`` command must not hide a usable realm/zone configuration.
Every section is bounded, cluster-scoped and safe to render as stale data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from shared.time import utc_now

from config.settings import settings
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.object_storage_cache import get_or_load, state as cache_state
from watcher import ceph_client
from watcher.ceph_client import CephQueryError

logger = logging.getLogger(__name__)

RGW_EVIDENCE_TTL_SECONDS = 60
RGW_EVIDENCE_STALE_TTL_SECONDS = 15 * 60
RGW_COMMAND_TIMEOUT_SECONDS = 10


def _iso_now() -> str:
    return utc_now().isoformat(timespec="seconds") + "Z"


def _rgw_hosts(cluster) -> list[str]:
    return [
        str(node["host"])
        for node in configured_nodes(cluster)
        if "RGW" in node.get("roles", [])
    ]


def _connection(cluster) -> tuple[list[str], str, str, str, str]:
    mon_nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    ssh_user, ssh_key_path, exec_mode, mon_container = resolve_ssh_creds(cluster)
    return mon_nodes, mon_container, ssh_user, ssh_key_path, exec_mode


def _rgw_command(cluster, host: str, inner_command: str) -> dict | list:
    ssh_user, ssh_key_path, exec_mode, _mon_container = resolve_ssh_creds(cluster)
    container = cluster.ceph_rgw_container_name
    if exec_mode not in ("cephadm", "none") and not container:
        raise ValueError("RGW container is not configured")
    command = ceph_client.build_exec_command(exec_mode, container, inner_command)
    output = ceph_client.run_command_on_node_with(
        host,
        command,
        ssh_user,
        ssh_key_path,
        timeout=RGW_COMMAND_TIMEOUT_SECONDS,
    )
    payload = json.loads(output)
    if not isinstance(payload, (dict, list)):
        raise ValueError("RGW returned an unexpected JSON shape")
    return payload


def _mon_command(cluster, inner_command: str) -> dict | list:
    connection = _connection(cluster)
    if not connection[0]:
        raise CephQueryError("no MON nodes configured")
    _stdout, payload = ceph_client.run_ceph_json_command_with(
        *connection, inner_command,
    )
    if not isinstance(payload, (dict, list)):
        raise ValueError("Ceph returned an unexpected JSON shape")
    return payload


def _section_unavailable(source: str, reason: str) -> dict:
    return {
        "status": "not_available",
        "source": source,
        "reason": reason,
        "items": [],
    }


def _read_rgw_command(cluster, hosts: list[str], command: str, source: str) -> dict:
    errors = []
    for host in hosts:
        try:
            payload = _rgw_command(cluster, host, command)
            return {
                "status": "observed",
                "source": source,
                "host": host,
                "payload": payload,
            }
        except Exception as exc:
            errors.append(type(exc).__name__)
    return _section_unavailable(source, "Không đọc được RGW evidence từ node đã cấu hình.") | {
        "errors": sorted(set(errors)),
    }


def _read_mon_command(cluster, command: str, source: str) -> dict:
    try:
        return {
            "status": "observed",
            "source": source,
            "payload": _mon_command(cluster, command),
        }
    except Exception as exc:
        return _section_unavailable(source, "Không đọc được Ceph evidence từ MON.") | {
            "errors": [type(exc).__name__],
        }


def _normalize_daemons(section: dict) -> dict:
    payload = section.get("payload")
    rows = payload if isinstance(payload, list) else (payload or {}).get("daemons", [])
    if isinstance(payload, dict):
        rows = payload.get("daemons") or payload.get("services") or payload.get("nodes") or []
    if not isinstance(rows, list):
        rows = []
    items = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        items.append({
            key: row.get(key)
            for key in ("daemon_name", "daemon_type", "hostname", "status_desc", "version", "service_name")
            if row.get(key) is not None
        })
    return {
        "status": section.get("status", "not_available") if items else "not_available",
        "source": section.get("source"),
        "items": items,
        "errors": section.get("errors", []),
    }


def _normalize_endpoints(section: dict, hosts: list[str]) -> dict:
    payload = section.get("payload")
    raw = payload.get("rgw") if isinstance(payload, dict) else None
    endpoints = []
    if isinstance(raw, str):
        endpoints = [raw]
    elif isinstance(raw, list):
        endpoints = [str(item) for item in raw if isinstance(item, (str, int))]
    endpoints = sorted({item.strip() for item in endpoints if item.strip()})
    inferred = False
    if not endpoints and hosts:
        endpoints = [f"http://{host}:7480" for host in hosts]
        inferred = True
    return {
        "status": "inferred" if inferred else "observed" if endpoints else "not_available",
        "source": section.get("source", "ceph_mgr_services"),
        "items": endpoints,
        "inferred": inferred,
        "errors": section.get("errors", []),
    }


def _normalize_frontend(section: dict) -> dict:
    payload = section.get("payload")
    rows = payload if isinstance(payload, list) else []
    values = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("name") or row.get("key") or "")
        if key in {"rgw_frontends", "rgw_frontend_type", "rgw_frontend_port"}:
            values.append({"key": key, "value": row.get("value")})
    return {
        "status": "observed" if values else "not_available",
        "source": section.get("source", "ceph_config_dump"),
        "items": values,
        "errors": section.get("errors", []),
    }


def _normalize_sync(section: dict) -> dict:
    payload = section.get("payload")
    status = "observed" if isinstance(payload, (dict, list)) else "not_available"
    return {
        "status": status,
        "source": section.get("source", "radosgw-admin-sync-status"),
        "details": payload if status == "observed" else {},
        "errors": section.get("errors", []),
    }


def _zone_pool_names(zone: dict) -> list[str]:
    names = []
    for key in ("domain_root", "control_pool", "gc_pool", "lc_pool", "log_pool", "intent_log_pool", "usage_log_pool", "roles_pool", "reshard_pool"):
        value = zone.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    for placement in zone.get("placement_pools", []) or []:
        if not isinstance(placement, dict):
            continue
        value = placement.get("val") if isinstance(placement.get("val"), dict) else placement
        for key in ("index_pool", "data_pool", "data_extra_pool"):
            pool = value.get(key)
            if isinstance(pool, str) and pool.strip():
                names.append(pool.strip())
    return sorted(set(names))


def _normalize_capacity(section: dict, zone_section: dict) -> dict:
    payload = section.get("payload")
    pools = payload.get("pools") if isinstance(payload, dict) else []
    zone = zone_section.get("payload") if isinstance(zone_section.get("payload"), dict) else {}
    wanted = set(_zone_pool_names(zone))
    items = []
    for row in pools if isinstance(pools, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if name not in wanted:
            continue
        stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        items.append({
            "pool": name,
            "bytes_used": stats.get("bytes_used"),
            "max_avail": stats.get("max_avail"),
            "objects": stats.get("objects"),
        })
    return {
        "status": "observed" if items else "not_available",
        "source": section.get("source", "ceph_df"),
        "items": items,
        "placement_pools": sorted(wanted),
        "errors": section.get("errors", []),
    }


def collect_rgw_evidence(cluster) -> dict:
    """Collect one bounded read-only RGW evidence snapshot."""
    captured_at = _iso_now()
    hosts = _rgw_hosts(cluster)
    orch = _read_mon_command(cluster, "ceph orch ps --service_type rgw", "ceph_orch_ps")
    services = _read_mon_command(cluster, "ceph mgr services", "ceph_mgr_services")
    config = _read_mon_command(cluster, "ceph config dump", "ceph_config_dump")
    realm = _read_rgw_command(cluster, hosts, "radosgw-admin realm get --format json", "rgw_realm") if hosts else _section_unavailable("rgw_realm", "Chưa cấu hình node RGW.")
    zonegroup = _read_rgw_command(cluster, hosts, "radosgw-admin zonegroup get --format json", "rgw_zonegroup") if hosts else _section_unavailable("rgw_zonegroup", "Chưa cấu hình node RGW.")
    zone = _read_rgw_command(cluster, hosts, "radosgw-admin zone get --format json", "rgw_zone") if hosts else _section_unavailable("rgw_zone", "Chưa cấu hình node RGW.")
    sync = _read_rgw_command(cluster, hosts, "radosgw-admin sync status --format json", "rgw_sync_status") if hosts else _section_unavailable("rgw_sync_status", "Chưa cấu hình node RGW.")
    sync_errors = _read_rgw_command(cluster, hosts, "radosgw-admin sync error list --format json", "rgw_sync_errors") if hosts else _section_unavailable("rgw_sync_errors", "Chưa cấu hình node RGW.")
    period = _read_rgw_command(cluster, hosts, "radosgw-admin period get --format json", "rgw_period") if hosts else _section_unavailable("rgw_period", "Chưa cấu hình node RGW.")
    df = _read_mon_command(cluster, "ceph df", "ceph_df")

    daemons = _normalize_daemons(orch)
    endpoints = _normalize_endpoints(services, hosts)
    frontend = _normalize_frontend(config)
    sync_view = _normalize_sync(sync)
    capacity = _normalize_capacity(df, zone)
    topology = {
        "realm": realm,
        "zonegroup": zonegroup,
        "zone": zone,
    }
    observed = [daemons, endpoints, frontend, sync_view, capacity, realm, zonegroup, zone, period]
    observed_count = sum(section.get("status") in {"observed", "inferred"} for section in observed)
    status = "ready" if observed_count >= 4 else "partial" if observed_count else "unavailable"
    gaps = []
    if not hosts:
        gaps.append("Chưa cấu hình node RGW để đọc radosgw-admin.")
    if daemons["status"] == "not_available":
        gaps.append("Chưa đọc được danh sách RGW daemon từ ceph orch; legacy systemd có thể cần adapter riêng.")
    if endpoints["inferred"]:
        gaps.append("Endpoint được suy ra từ node RGW và port mặc định 7480, chưa xác nhận bằng mgr service.")
    if sync_view["status"] == "not_available":
        gaps.append("Chưa đọc được sync status; không suy luận RGW multisite đang đồng bộ.")
    if period.get("status") == "not_available":
        gaps.append("Chưa đọc được period get; không xác nhận được epoch/master state multisite.")
    if sync_errors.get("status") == "not_available":
        gaps.append("Chưa đọc được sync error list; shard error có thể chưa đầy đủ.")
    if capacity["status"] == "not_available":
        gaps.append("Chưa map được RGW placement pool với ceph df để tính capacity dependency.")
    return {
        "status": status,
        "cluster_id": cluster.id,
        "captured_at": captured_at,
        "read_only": True,
        "recommendation_mode": "EVIDENCE_ONLY",
        "action_id": None,
        "rgw_hosts": hosts,
        "daemons": daemons,
        "endpoints": endpoints,
        "frontend": frontend,
        "topology": topology,
        "sync": sync_view,
        "sync_errors": {
            "status": sync_errors.get("status"),
            "source": sync_errors.get("source"),
            "details": sync_errors.get("payload", {}),
            "errors": sync_errors.get("errors", []),
        },
        "period": {
            "status": period.get("status"),
            "source": period.get("source"),
            "details": period.get("payload", {}),
            "errors": period.get("errors", []),
        },
        "capacity": capacity,
        "evidence_gaps": gaps,
    }


def get_rgw_evidence(cluster) -> dict:
    """Return fresh evidence or bounded stale evidence for one cluster."""
    key = f"{cluster.id}:snapshot"
    result = get_or_load(
        "rgw-evidence",
        key,
        lambda: collect_rgw_evidence(cluster),
        ttl_seconds=RGW_EVIDENCE_TTL_SECONDS,
        stale_ttl_seconds=RGW_EVIDENCE_STALE_TTL_SECONDS,
    )
    cache = cache_state("rgw-evidence", key)
    age = cache.get("age_seconds")
    result["cache"] = {
        "age_seconds": round(float(age), 1) if age is not None else None,
        "stale": bool(age is not None and age >= RGW_EVIDENCE_TTL_SECONDS),
        "refreshing": bool(cache.get("refreshing")),
        "refresh_error": bool(cache.get("error")),
        "ttl_seconds": RGW_EVIDENCE_TTL_SECONDS,
        "stale_ttl_seconds": RGW_EVIDENCE_STALE_TTL_SECONDS,
    }
    if result["cache"]["stale"]:
        result["evidence_gaps"] = list(result.get("evidence_gaps") or [])
        result["evidence_gaps"].append("Dữ liệu RGW đang stale; cần chờ refresh thành công.")
    return result
