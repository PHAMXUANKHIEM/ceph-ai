"""Read model for the Ceph cluster view of the Stream page.

Builds one cluster's service topology (clients, MON, MGR, MDS, RGW, OSD,
pools/PG, hosts and the Ceph networks) with a status per element, only from
snapshots the watcher already stored (shared/cluster_snapshot.py): opening
the page never runs a Ceph command.

* status: the health snapshot (checks with details, muted ones ignored) and
  the ``status`` section (``ceph -s``);
* hosts: the ``nodes`` section (roles, addresses, configured public/cluster
  networks, MON/OSD daemon addresses from ``mon dump``/``osd dump``) and the
  ``crush`` section (OSDs per host, grouped by host);
* clients: the ``pools`` section's applications (rbd, rgw, cephfs).

A snapshot older than its stale limit turns every status into ``unknown``
rather than drawing an old picture as healthy.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any, Callable

from shared import cluster_snapshot
from shared.time import utc_now

SCHEMA = "ceph-ai.ceph-topology.v1"
OK, WARN, ERROR, UNKNOWN = "ok", "warn", "error", "unknown"
_RANK = {OK: 0, UNKNOWN: 1, WARN: 2, ERROR: 3}
_OSD_RE = re.compile(r"\bosd\.(\d+)\b")

GROUPS = [
    {"id": "clients", "title": "CLIENT"},
    {"id": "control", "title": "CONTROL PLANE"},
    {"id": "data", "title": "GATEWAY · DATA"},
    {"id": "hosts", "title": "HOST (OSD gom theo host)"},
    {"id": "networks", "title": "MẠNG"},
]

# Health checks that make a component red rather than amber.
_ERROR_CHECKS = {
    "mon": {"MON_DOWN"},
    "mgr": {"MGR_DOWN"},
    "osd": {"OSD_DOWN", "OSD_HOST_DOWN", "OSD_FULL"},
    "pools": {"PG_AVAILABILITY", "PG_DAMAGED", "OBJECT_UNFOUND", "POOL_FULL"},
}
_COMPONENT_CHECKS: dict[str, Callable[[str], bool]] = {
    "mon": lambda code: code.startswith("MON_") or code.startswith("AUTH_"),
    "mgr": lambda code: code.startswith("MGR_"),
    "osd": lambda code: code.startswith(("OSD_", "BLUESTORE_", "SLOW_OPS")) and not code.startswith("OSD_SLOW_PING"),
    "pools": lambda code: code.startswith(("PG_", "POOL_", "OBJECT_", "LARGE_OMAP", "TOO_MANY_PGS", "TOO_FEW_PGS")),
    "mds": lambda code: code.startswith(("MDS_", "FS_")),
    "rgw": lambda code: code.startswith("RGW_"),
}


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda status: _RANK[status]) if statuses else OK


def _short_host(name: Any) -> str:
    return str(name or "").split("~", 1)[0].split(".", 1)[0]


def _active_checks(health: dict) -> dict[str, dict]:
    checks = (health or {}).get("checks") or {}
    return {code: check for code, check in checks.items() if isinstance(check, dict) and not check.get("muted")}


def _messages(check: dict) -> list[str]:
    detail = [str(item.get("message", "")).strip() for item in check.get("detail") or [] if isinstance(item, dict)]
    summary = str((check.get("summary") or {}).get("message", "")).strip()
    return [message for message in detail if message] or ([summary] if summary else [])


def _check_view(code: str, check: dict) -> dict:
    return {
        "code": code,
        "severity": check.get("severity"),
        "message": str((check.get("summary") or {}).get("message", "")),
    }


def _component_status(name: str, checks: dict[str, dict]) -> tuple[str, list[dict]]:
    matched = {code: check for code, check in checks.items() if _COMPONENT_CHECKS[name](code)}
    status = OK
    for code, check in matched.items():
        error = code in _ERROR_CHECKS.get(name, set()) or check.get("severity") == "HEALTH_ERR"
        status = _worst(status, ERROR if error else WARN)
    return status, [_check_view(code, check) for code, check in matched.items()]


def _payload(section: dict | None, key: str) -> Any:
    return (section or {}).get(key) if isinstance(section, dict) else None


def _read(cluster_id: str) -> dict[str, dict | None]:
    sections: dict[str, dict | None] = {"health": cluster_snapshot.read_snapshot(cluster_id)}
    for name in ("status", "nodes", "crush", "pools"):
        try:
            sections[name] = cluster_snapshot.read_section_snapshot(cluster_id, name)
        except ValueError:
            sections[name] = None
    return sections


def _osds_by_host(crush: dict | None) -> dict[str, list[int]]:
    hosts: dict[str, list[int]] = {}

    def walk(node: dict) -> None:
        if node.get("type") == "host":
            ids = sorted({int(child["id"]) for child in node.get("children") or [] if child.get("type") == "osd"})
            hosts.setdefault(_short_host(node.get("name")), [])
            hosts[_short_host(node.get("name"))] = sorted(set(hosts[_short_host(node.get("name"))]) | set(ids))
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child)

    for root in (crush or {}).get("roots") or []:
        if isinstance(root, dict):
            walk(root)
    return hosts


def _networks(nodes_payload: dict | None) -> tuple[list[str], list[str]]:
    networks = (nodes_payload or {}).get("networks") or {}
    return list(networks.get("public") or []), list(networks.get("cluster") or [])


def _in_networks(address: str, cidrs: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def _ip_of(addr: str) -> str:
    """``10.0.0.1:6800`` or ``[fd00::1]:6800`` -> the bare IP."""
    host = addr.rsplit(":", 1)[0]
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host


def _addr_text(addrvec: list[dict]) -> str:
    return " · ".join(f"{item['type']} {item['addr']}" for item in addrvec) or "—"


def _daemons(nodes_payload: dict) -> tuple[list[dict], list[dict]] | None:
    daemons = nodes_payload.get("daemons")
    if not isinstance(daemons, dict):
        return None
    mons = [mon for mon in daemons.get("mons") or [] if isinstance(mon, dict)]
    osds = [osd for osd in daemons.get("osds") or [] if isinstance(osd, dict) and isinstance(osd.get("id"), int)]
    return mons, osds


def _outside(addrvecs: list[list[dict]], cidrs: list[str]) -> list[str]:
    """Addresses not inside any of ``cidrs`` (empty when no CIDR is configured)."""
    if not cidrs:
        return []
    return [item["addr"] for addrvec in addrvecs for item in addrvec if not _in_networks(_ip_of(item["addr"]), cidrs)]


def _slow_ping_facts(check: dict | None, limit: int = 5) -> list[str]:
    """The OSD pairs of an ``OSD_SLOW_PING_TIME_*`` check (detail lines)."""
    detail = [str(item.get("message", "")).strip() for item in (check or {}).get("detail") or [] if isinstance(item, dict)]
    detail = [line for line in detail if line]
    facts = [f"Heartbeat chậm: {line}" for line in detail[:limit]]
    if len(detail) > limit:
        facts.append(f"… và {len(detail) - limit} cặp OSD khác")
    return facts


def _mon_address_facts(mon_daemons: list[dict], quorum: list[str]) -> list[str]:
    in_quorum = {_short_host(name) for name in quorum}
    facts = []
    for mon in mon_daemons:
        name = _short_host(mon.get("name"))
        suffix = "" if name in in_quorum else " (ngoài quorum)"
        facts.append(f"mon.{name}: {_addr_text(mon.get('public') or [])}{suffix}")
    return facts


def _host_daemon_facts(name: str, mon_daemons: list[dict], osd_ids: list[int], osd_addrs: dict[int, dict]) -> list[str]:
    facts = [f"mon.{name}: {_addr_text(mon.get('public') or [])}"
             for mon in mon_daemons if _short_host(mon.get("name")) == name]
    for osd in osd_ids:
        if osd in osd_addrs:
            public_text = _addr_text(osd_addrs[osd].get("public") or [])
            cluster_text = _addr_text(osd_addrs[osd].get("cluster") or [])
            facts.append(f"osd.{osd}: public {public_text} | cluster {cluster_text}")
    return facts


def _network_nodes(public_nets: list[str], cluster_nets: list[str],
                   daemons: tuple[list[dict], list[dict]] | None) -> list[dict]:
    """Public/cluster network nodes; flags daemon addresses outside the configured CIDRs."""
    mon_daemons, osd_daemons = daemons or ([], [])
    outside_public = _outside([mon.get("public") or [] for mon in mon_daemons]
                              + [osd.get("public") or [] for osd in osd_daemons], public_nets)
    outside_cluster = _outside([osd.get("cluster") or [] for osd in osd_daemons], cluster_nets)
    nodes = []
    if public_nets:
        facts = ["Client ↔ MON/OSD/RGW", "Theo cấu hình Ceph public_network"]
        if daemons:
            facts.append(f"Daemon lắng nghe: MON ×{len(mon_daemons)}, OSD ×{len(osd_daemons)}")
        if outside_public:
            facts.append(f"Địa chỉ ngoài public_network: {', '.join(outside_public)}")
        if not cluster_nets:
            facts.append("Không có cluster_network: replication đi chung public network")
        nodes.append(_node("net_public", "networks", "network", "Public network", ", ".join(public_nets),
                           WARN if outside_public else OK, facts))
    if cluster_nets:
        facts = ["OSD ↔ OSD: replication, recovery, heartbeat", "Theo cấu hình Ceph cluster_network"]
        if daemons:
            facts.append(f"Daemon lắng nghe: OSD ×{len(osd_daemons)}")
        if outside_cluster:
            # OSDs that fell back to the public address replicate there instead.
            facts.append(f"Địa chỉ cluster ngoài cluster_network: {', '.join(outside_cluster)}")
        nodes.append(_node("net_cluster", "networks", "network", "Cluster network", ", ".join(cluster_nets),
                           WARN if outside_cluster else OK, facts))
    return nodes


def _down_osds(checks: dict[str, dict]) -> set[int]:
    down: set[int] = set()
    for code in ("OSD_DOWN", "OSD_HOST_DOWN"):
        for message in _messages(checks.get(code) or {}):
            if "down" in message.lower():
                down.update(int(match) for match in _OSD_RE.findall(message))
    return down


def _host_mentions(checks: dict[str, dict], host: str, osd_ids: list[int]) -> list[dict]:
    mentioned = []
    for code, check in checks.items():
        text = " ".join(_messages(check))
        ids = {int(match) for match in _OSD_RE.findall(text)}
        if host and (re.search(rf"\b{re.escape(host)}\b", text) or ids & set(osd_ids)):
            mentioned.append(_check_view(code, check))
    return mentioned


def _pool_applications(pools: Any) -> set[str]:
    apps: set[str] = set()
    for row in pools if isinstance(pools, list) else []:
        for key in ("application", "applications", "application_metadata", "apps"):
            value = row.get(key) if isinstance(row, dict) else None
            if isinstance(value, dict):
                apps.update(str(name) for name in value)
            elif isinstance(value, (list, tuple)):
                apps.update(str(name) for name in value)
            elif isinstance(value, str) and value:
                apps.update(part.strip() for part in value.split(",") if part.strip())
    return apps


def _rgw_daemons(status: dict) -> list[dict]:
    daemons = (((status.get("servicemap") or {}).get("services") or {}).get("rgw") or {}).get("daemons") or {}
    result = []
    for key, daemon in daemons.items():
        if isinstance(daemon, dict):
            metadata = daemon.get("metadata") or {}
            result.append({"id": str(key), "host": _short_host(metadata.get("hostname")),
                           "frontend": metadata.get("frontend_config#0"), "zonegroup": metadata.get("zonegroup_name")})
    return result


def _node(node_id: str, group: str, kind: str, title: str, subtitle: str, status: str,
          facts: list[str], checks: list[dict] | None = None, href: str | None = None) -> dict:
    return {"id": node_id, "group": group, "kind": kind, "title": title, "subtitle": subtitle,
            "status": status, "facts": facts, "checks": checks or [], "href": href}


def build(cluster, *, now: datetime | None = None, sections: dict[str, dict | None] | None = None) -> dict:
    """The topology of one cluster from its stored snapshots."""
    now = now or utc_now()
    sections = sections if sections is not None else _read(str(cluster.id))
    health_section = sections.get("health") or {}
    status = _payload(sections.get("status"), "status") or {}
    nodes_payload = _payload(sections.get("nodes"), "nodes") or {}
    crush = _payload(sections.get("crush"), "crush") or {}
    pools = _payload(sections.get("pools"), "pools")
    checks = _active_checks(health_section.get("health") or status.get("health") or {})
    stale = bool(not health_section or health_section.get("stale") or not status
                 or (sections.get("status") or {}).get("stale"))

    monmap, osdmap, pgmap = status.get("monmap") or {}, status.get("osdmap") or {}, status.get("pgmap") or {}
    mgrmap, fsmap = status.get("mgrmap") or {}, status.get("fsmap") or {}
    quorum = list(status.get("quorum_names") or [])
    num_mons = int(monmap.get("num_mons") or len(quorum))
    osd_total, osd_up, osd_in = (int(osdmap.get(key) or 0) for key in ("num_osds", "num_up_osds", "num_in_osds"))
    pg_states = {row.get("state_name"): int(row.get("count") or 0) for row in pgmap.get("pgs_by_state") or [] if isinstance(row, dict)}
    pg_total = int(pgmap.get("num_pgs") or sum(pg_states.values()))
    active_clean = pg_states.get("active+clean", 0)
    used_pct = (round(100 * pgmap["bytes_used"] / pgmap["bytes_total"], 1)
                if pgmap.get("bytes_total") else None)
    public_nets, cluster_nets = _networks(nodes_payload)
    osds_by_host = _osds_by_host(crush)
    down_osds = _down_osds(checks)
    daemons = _daemons(nodes_payload)
    mon_daemons, osd_daemons = daemons or ([], [])
    osd_addrs = {osd["id"]: osd for osd in osd_daemons}
    # Health (refreshed every minute) stays the primary source for down OSDs;
    # the inventory's osd dump only names the ones a truncated detail left out.
    if osd_total and len(down_osds) < osd_total - osd_up:
        down_osds |= {osd["id"] for osd in osd_daemons if not osd.get("up")}
    out_osds = sorted(osd["id"] for osd in osd_daemons if not osd.get("in"))
    rgw = _rgw_daemons(status)
    apps = _pool_applications(pools)
    mds_up = [rank for rank in fsmap.get("by_rank") or [] if isinstance(rank, dict)]

    out_nodes: list[dict] = []
    edges: list[dict] = []

    def edge(source: str, target: str, label: str, kind: str) -> None:
        edges.append({"from": source, "to": target, "label": label, "kind": kind})

    # Control plane.
    mon_status, mon_checks = _component_status("mon", checks)
    if quorum and len(quorum) < num_mons:
        mon_status = ERROR
    out_nodes.append(_node("mon", "control", "mon", "MON quorum", f"{len(quorum)}/{num_mons} trong quorum", mon_status, [
        f"Quorum: {', '.join(quorum) or 'chưa rõ'}",
        f"Monmap epoch {monmap.get('epoch', '?')} · {monmap.get('min_mon_release_name', '?')}",
        *_mon_address_facts(mon_daemons, quorum),
    ], mon_checks, "/nodes"))
    mgr_status, mgr_checks = _component_status("mgr", checks)
    if mgrmap and not mgrmap.get("available"):
        mgr_status = ERROR
    services = mgrmap.get("services") or {}
    out_nodes.append(_node("mgr", "control", "mgr", "MGR", f"active + {mgrmap.get('num_standbys', 0)} standby", mgr_status, [
        f"Module: {', '.join(mgrmap.get('modules') or []) or 'chưa rõ'}",
        f"Dịch vụ: {', '.join(sorted(services)) or 'không có'}",
    ], mgr_checks))
    edge("mgr", "mon", "trạng thái · module", "control")
    if mds_up or "cephfs" in apps:
        mds_status, mds_checks = _component_status("mds", checks)
        out_nodes.append(_node("mds", "control", "mds", "MDS (CephFS)", f"{len(mds_up)} rank đang chạy",
                               mds_status if mds_up else _worst(mds_status, WARN),
                               [f"FSMap epoch {fsmap.get('epoch', '?')}"], mds_checks))
        edge("mds", "mon", "fsmap", "control")
        edge("mds", "osd", "metadata pool", "data")

    # Gateway and data.
    osd_status, osd_checks = _component_status("osd", checks)
    if osd_total and osd_up < osd_total:
        osd_status = ERROR
    out_nodes.append(_node("osd", "data", "osd", "OSD", f"{osd_up}/{osd_total} up · {osd_in}/{osd_total} in", osd_status, [
        f"Osdmap epoch {osdmap.get('epoch', '?')}",
        f"OSD down: {', '.join(f'osd.{osd}' for osd in sorted(down_osds)) or 'không có'}",
        *([f"OSD out: {', '.join(f'osd.{osd}' for osd in out_osds) or 'không có'}"] if daemons else []),
    ], osd_checks, "/crush-map"))
    pool_status, pool_checks = _component_status("pools", checks)
    if pg_total and active_clean < pg_total:
        pool_status = _worst(pool_status, WARN)
    out_nodes.append(_node("pools", "data", "pools", "Pool · PG",
                           f"{active_clean}/{pg_total} PG active+clean · {pgmap.get('num_pools', '?')} pool",
                           pool_status, [
                               "PG theo trạng thái: " + (", ".join(f"{state} {count}" for state, count in pg_states.items()) or "chưa rõ"),
                               f"Dung lượng đã dùng: {used_pct}%" if used_pct is not None else "Dung lượng: chưa rõ",
                               f"Object: {pgmap.get('num_objects', '?')}",
                           ], pool_checks, "/pools"))
    edge("osd", "pools", "lưu PG", "data")
    edge("osd", "mon", "osdmap · heartbeat", "control")
    if rgw:
        rgw_status, rgw_checks = _component_status("rgw", checks)
        for code, check in checks.items():
            if code == "CEPHADM_FAILED_DAEMON" and any("rgw" in message for message in _messages(check)):
                rgw_status = ERROR
                rgw_checks.append(_check_view(code, check))
        out_nodes.append(_node("rgw", "data", "rgw", "RGW (S3)", f"{len(rgw)} daemon", rgw_status, [
            f"{daemon['host'] or '?'} · {daemon['frontend'] or '?'} · zonegroup {daemon['zonegroup'] or '?'}" for daemon in rgw
        ], rgw_checks, "/object-storage/buckets"))
        edge("rgw", "osd", "object data", "data")
        edge("rgw", "mon", "cluster map", "control")

    # Clients (from pool applications) and Ceph AI itself.
    out_nodes.append(_node("ceph_ai", "clients", "client", "Ceph AI", "watcher · worker", OK, [
        "Đọc health/telemetry qua SSH chỉ-đọc", "Lệnh đã duyệt chạy bằng khóa riêng",
    ]))
    edge("ceph_ai", "mon", "ceph status (SSH)", "control")
    client_specs = (
        ("rbd", "rbd_clients", "RBD client", "OpenStack / Kubernetes / VM"),
        ("rgw", "s3_clients", "S3 client", "ứng dụng qua RGW"),
        ("cephfs", "cephfs_clients", "CephFS client", "mount CephFS"),
    )
    for app, node_id, title, subtitle in client_specs:
        if app not in apps:
            continue
        out_nodes.append(_node(node_id, "clients", "client", title, subtitle, OK,
                               [f"Có pool với ứng dụng '{app}'"]))
        if app == "rgw":
            if rgw:
                edge(node_id, "rgw", "S3 / HTTP", "data")
        else:
            edge(node_id, "mon", "cluster map", "control")
            edge(node_id, "osd", "đọc / ghi dữ liệu", "data")

    # Networks.
    out_nodes.extend(_network_nodes(public_nets, cluster_nets, daemons))
    front = checks.get("OSD_SLOW_PING_TIME_FRONT")
    back = checks.get("OSD_SLOW_PING_TIME_BACK")
    for node_id, slow_check, code in (("net_public", front, "OSD_SLOW_PING_TIME_FRONT"),
                                      ("net_cluster" if cluster_nets else "net_public", back, "OSD_SLOW_PING_TIME_BACK")):
        target = next((node for node in out_nodes if node["id"] == node_id), None)
        if target is not None and slow_check:
            target["status"] = _worst(target["status"], WARN)
            target["checks"].append(_check_view(code, slow_check))
            target["facts"].extend(_slow_ping_facts(slow_check))
    mon_ports = sorted({item["addr"].rsplit(":", 1)[1] for mon in mon_daemons for item in mon.get("public") or []})
    if public_nets:
        edge("osd", "net_public", "client I/O" if cluster_nets else "client I/O · replication", "network")
        edge("mon", "net_public", f"lắng nghe :{', :'.join(mon_ports)}" if mon_ports else "lắng nghe", "network")
    if cluster_nets:
        edge("osd", "net_cluster", "replication · recovery", "network")

    # Hosts, OSDs grouped by host.
    management_hosts = []
    for entry in nodes_payload.get("nodes") or []:
        if not isinstance(entry, dict):
            continue
        name = _short_host(entry.get("node_name") or entry.get("host"))
        addresses = [str(address) for address in entry.get("aliases") or [entry.get("host")] if address]
        osd_ids = osds_by_host.get(name, [])
        host_checks = _host_mentions(checks, name, osd_ids)
        down = sorted(set(osd_ids) & down_osds)
        host_status = OK
        if down or any(check["code"] == "OSD_HOST_DOWN" for check in host_checks):
            host_status = ERROR
        elif host_checks:
            host_status = WARN
        node_id = f"host_{re.sub(r'[^A-Za-z0-9_]', '_', name)}"
        public_addr = [address for address in addresses if _in_networks(address, public_nets)]
        cluster_addr = [address for address in addresses if _in_networks(address, cluster_nets)]
        other_addr = [address for address in addresses if address not in public_addr and address not in cluster_addr]
        out_nodes.append(_node(node_id, "hosts", "host", name, " · ".join(entry.get("roles") or []) or "chưa rõ vai trò",
                               host_status, [
                                   "OSD: " + (", ".join(f"osd.{osd}{' (down)' if osd in down else ''}" for osd in osd_ids) or "không có"),
                                   "Địa chỉ public: " + (", ".join(public_addr) or "—"),
                                   *(["Địa chỉ cluster: " + ", ".join(cluster_addr)] if cluster_nets else []),
                                   "Địa chỉ khác (quản trị): " + (", ".join(other_addr) or "—"),
                                   *_host_daemon_facts(name, mon_daemons, osd_ids, osd_addrs),
                               ], host_checks, "/nodes"))
        if osd_ids:
            edge(node_id, "osd", f"{len(osd_ids)} OSD", "data")
        if public_addr:
            edge(node_id, "net_public", "public", "network")
        if cluster_addr:
            edge(node_id, "net_cluster", "cluster", "network")
        if other_addr:
            management_hosts.append(node_id)
    if management_hosts:
        out_nodes.append(_node("net_management", "networks", "network", "Mạng quản trị",
                               "địa chỉ ngoài public/cluster network", OK,
                               ["SSH của Ceph AI và operator", "Không mang dữ liệu Ceph"]))
        edge("ceph_ai", "net_management", "SSH", "management")
        for node_id in management_hosts:
            edge(node_id, "net_management", "SSH", "management")

    if stale:
        for node in out_nodes:
            node["status"] = UNKNOWN
    snapshot_times = {name: (sections.get(name) or {}).get("collected_at") for name in ("health", "status", "nodes", "crush")}
    return {
        "schema": SCHEMA,
        "cluster": {"id": str(cluster.id), "name": getattr(cluster, "name", "")},
        "generated_at": now.isoformat(),
        "stale": stale,
        "snapshots": snapshot_times,
        "summary": {
            "health": (health_section.get("health") or {}).get("status") or (status.get("health") or {}).get("status"),
            "mons_in_quorum": len(quorum), "mons": num_mons,
            "osds_up": osd_up, "osds_in": osd_in, "osds": osd_total,
            "pgs_active_clean": active_clean, "pgs": pg_total,
            "used_percent": used_pct,
            "public_network": public_nets, "cluster_network": cluster_nets,
        },
        "groups": GROUPS,
        "nodes": out_nodes,
        "edges": [item for item in edges if {item["from"], item["to"]} <= {node["id"] for node in out_nodes}],
    }
