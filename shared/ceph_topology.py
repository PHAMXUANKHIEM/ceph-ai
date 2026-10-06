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
from dataclasses import dataclass, field
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


@dataclass
class _Inputs:
    """The snapshot values the graph is drawn from."""

    checks: dict[str, dict]
    status: dict
    nodes_payload: dict
    quorum: list[str]
    num_mons: int
    osd_total: int
    osd_up: int
    osd_in: int
    pg_states: dict
    pg_total: int
    active_clean: int
    used_pct: float | None
    public_nets: list[str]
    cluster_nets: list[str]
    osds_by_host: dict[str, list[int]]
    down_osds: set[int]
    daemons: tuple[list[dict], list[dict]] | None
    out_osds: list[int]
    rgw: list[dict]
    apps: set[str]

    @property
    def mon_daemons(self) -> list[dict]:
        return self.daemons[0] if self.daemons else []

    @property
    def osd_daemons(self) -> list[dict]:
        return self.daemons[1] if self.daemons else []


@dataclass
class _Graph:
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)

    def edge(self, source: str, target: str, label: str, kind: str) -> None:
        self.edges.append({"from": source, "to": target, "label": label, "kind": kind})

    def find(self, node_id: str) -> dict | None:
        return next((node for node in self.nodes if node["id"] == node_id), None)


def _inputs(sections: dict[str, dict | None]) -> _Inputs:
    health_section = sections.get("health") or {}
    status = _payload(sections.get("status"), "status") or {}
    nodes_payload = _payload(sections.get("nodes"), "nodes") or {}
    checks = _active_checks(health_section.get("health") or status.get("health") or {})
    monmap, osdmap, pgmap = status.get("monmap") or {}, status.get("osdmap") or {}, status.get("pgmap") or {}
    quorum = list(status.get("quorum_names") or [])
    osd_total, osd_up, osd_in = (int(osdmap.get(key) or 0) for key in ("num_osds", "num_up_osds", "num_in_osds"))
    pg_states = {row.get("state_name"): int(row.get("count") or 0) for row in pgmap.get("pgs_by_state") or [] if isinstance(row, dict)}
    public_nets, cluster_nets = _networks(nodes_payload)
    daemons = _daemons(nodes_payload)
    osd_daemons = daemons[1] if daemons else []
    down_osds = _down_osds(checks)
    # Health (refreshed every minute) stays the primary source for down OSDs;
    # the inventory's osd dump only names the ones a truncated detail left out.
    if osd_total and len(down_osds) < osd_total - osd_up:
        down_osds |= {osd["id"] for osd in osd_daemons if not osd.get("up")}
    return _Inputs(
        checks=checks, status=status, nodes_payload=nodes_payload, quorum=quorum,
        num_mons=int(monmap.get("num_mons") or len(quorum)),
        osd_total=osd_total, osd_up=osd_up, osd_in=osd_in,
        pg_states=pg_states, pg_total=int(pgmap.get("num_pgs") or sum(pg_states.values())),
        active_clean=pg_states.get("active+clean", 0),
        used_pct=round(100 * pgmap["bytes_used"] / pgmap["bytes_total"], 1) if pgmap.get("bytes_total") else None,
        public_nets=public_nets, cluster_nets=cluster_nets,
        osds_by_host=_osds_by_host(_payload(sections.get("crush"), "crush") or {}),
        down_osds=down_osds, daemons=daemons,
        out_osds=sorted(osd["id"] for osd in osd_daemons if not osd.get("in")),
        rgw=_rgw_daemons(status), apps=_pool_applications(_payload(sections.get("pools"), "pools")),
    )


def _control_plane(graph: _Graph, inputs: _Inputs) -> None:
    status, checks, quorum = inputs.status, inputs.checks, inputs.quorum
    monmap, mgrmap, fsmap = status.get("monmap") or {}, status.get("mgrmap") or {}, status.get("fsmap") or {}
    mon_status, mon_checks = _component_status("mon", checks)
    if quorum and len(quorum) < inputs.num_mons:
        mon_status = ERROR
    graph.nodes.append(_node("mon", "control", "mon", "MON quorum", f"{len(quorum)}/{inputs.num_mons} trong quorum", mon_status, [
        f"Quorum: {', '.join(quorum) or 'chưa rõ'}",
        f"Monmap epoch {monmap.get('epoch', '?')} · {monmap.get('min_mon_release_name', '?')}",
        *_mon_address_facts(inputs.mon_daemons, quorum),
    ], mon_checks, "/nodes"))
    mgr_status, mgr_checks = _component_status("mgr", checks)
    if mgrmap and not mgrmap.get("available"):
        mgr_status = ERROR
    services = mgrmap.get("services") or {}
    graph.nodes.append(_node("mgr", "control", "mgr", "MGR", f"active + {mgrmap.get('num_standbys', 0)} standby", mgr_status, [
        f"Module: {', '.join(mgrmap.get('modules') or []) or 'chưa rõ'}",
        f"Dịch vụ: {', '.join(sorted(services)) or 'không có'}",
    ], mgr_checks))
    graph.edge("mgr", "mon", "trạng thái · module", "control")
    mds_up = [rank for rank in fsmap.get("by_rank") or [] if isinstance(rank, dict)]
    if mds_up or "cephfs" in inputs.apps:
        mds_status, mds_checks = _component_status("mds", checks)
        graph.nodes.append(_node("mds", "control", "mds", "MDS (CephFS)", f"{len(mds_up)} rank đang chạy",
                                 mds_status if mds_up else _worst(mds_status, WARN),
                                 [f"FSMap epoch {fsmap.get('epoch', '?')}"], mds_checks))
        graph.edge("mds", "mon", "fsmap", "control")
        graph.edge("mds", "osd", "metadata pool", "data")


def _data_path(graph: _Graph, inputs: _Inputs) -> None:
    osdmap, pgmap = inputs.status.get("osdmap") or {}, inputs.status.get("pgmap") or {}
    osd_status, osd_checks = _component_status("osd", inputs.checks)
    if inputs.osd_total and inputs.osd_up < inputs.osd_total:
        osd_status = ERROR
    total = inputs.osd_total
    graph.nodes.append(_node("osd", "data", "osd", "OSD", f"{inputs.osd_up}/{total} up · {inputs.osd_in}/{total} in", osd_status, [
        f"Osdmap epoch {osdmap.get('epoch', '?')}",
        f"OSD down: {', '.join(f'osd.{osd}' for osd in sorted(inputs.down_osds)) or 'không có'}",
        *([f"OSD out: {', '.join(f'osd.{osd}' for osd in inputs.out_osds) or 'không có'}"] if inputs.daemons else []),
    ], osd_checks, "/crush-map"))
    pool_status, pool_checks = _component_status("pools", inputs.checks)
    if inputs.pg_total and inputs.active_clean < inputs.pg_total:
        pool_status = _worst(pool_status, WARN)
    used = inputs.used_pct
    graph.nodes.append(_node("pools", "data", "pools", "Pool · PG",
                             f"{inputs.active_clean}/{inputs.pg_total} PG active+clean · {pgmap.get('num_pools', '?')} pool",
                             pool_status, [
                                 "PG theo trạng thái: " + (", ".join(f"{state} {count}" for state, count in inputs.pg_states.items()) or "chưa rõ"),
                                 f"Dung lượng đã dùng: {used}%" if used is not None else "Dung lượng: chưa rõ",
                                 f"Object: {pgmap.get('num_objects', '?')}",
                             ], pool_checks, "/pools"))
    graph.edge("osd", "pools", "lưu PG", "data")
    graph.edge("osd", "mon", "osdmap · heartbeat", "control")
    _rgw_node(graph, inputs)


def _rgw_node(graph: _Graph, inputs: _Inputs) -> None:
    if not inputs.rgw:
        return
    rgw_status, rgw_checks = _component_status("rgw", inputs.checks)
    for code, check in inputs.checks.items():
        if code == "CEPHADM_FAILED_DAEMON" and any("rgw" in message for message in _messages(check)):
            rgw_status = ERROR
            rgw_checks.append(_check_view(code, check))
    graph.nodes.append(_node("rgw", "data", "rgw", "RGW (S3)", f"{len(inputs.rgw)} daemon", rgw_status, [
        f"{daemon['host'] or '?'} · {daemon['frontend'] or '?'} · zonegroup {daemon['zonegroup'] or '?'}" for daemon in inputs.rgw
    ], rgw_checks, "/object-storage/buckets"))
    graph.edge("rgw", "osd", "object data", "data")
    graph.edge("rgw", "mon", "cluster map", "control")


_CLIENT_SPECS = (
    ("rbd", "rbd_clients", "RBD client", "OpenStack / Kubernetes / VM"),
    ("rgw", "s3_clients", "S3 client", "ứng dụng qua RGW"),
    ("cephfs", "cephfs_clients", "CephFS client", "mount CephFS"),
)


def _clients(graph: _Graph, inputs: _Inputs) -> None:
    """Clients from pool applications, plus Ceph AI itself."""
    graph.nodes.append(_node("ceph_ai", "clients", "client", "Ceph AI", "watcher · worker", OK, [
        "Đọc health/telemetry qua SSH chỉ-đọc", "Lệnh đã duyệt chạy bằng khóa riêng",
    ]))
    graph.edge("ceph_ai", "mon", "ceph status (SSH)", "control")
    for app, node_id, title, subtitle in _CLIENT_SPECS:
        if app not in inputs.apps:
            continue
        graph.nodes.append(_node(node_id, "clients", "client", title, subtitle, OK,
                                 [f"Có pool với ứng dụng '{app}'"]))
        if app != "rgw":
            graph.edge(node_id, "mon", "cluster map", "control")
            graph.edge(node_id, "osd", "đọc / ghi dữ liệu", "data")
        elif inputs.rgw:
            graph.edge(node_id, "rgw", "S3 / HTTP", "data")


def _networks_layer(graph: _Graph, inputs: _Inputs) -> None:
    public_nets, cluster_nets = inputs.public_nets, inputs.cluster_nets
    graph.nodes.extend(_network_nodes(public_nets, cluster_nets, inputs.daemons))
    slow = (("net_public", "OSD_SLOW_PING_TIME_FRONT"),
            ("net_cluster" if cluster_nets else "net_public", "OSD_SLOW_PING_TIME_BACK"))
    for node_id, code in slow:
        target, slow_check = graph.find(node_id), inputs.checks.get(code)
        if target is not None and slow_check:
            target["status"] = _worst(target["status"], WARN)
            target["checks"].append(_check_view(code, slow_check))
            target["facts"].extend(_slow_ping_facts(slow_check))
    mon_ports = sorted({item["addr"].rsplit(":", 1)[1] for mon in inputs.mon_daemons for item in mon.get("public") or []})
    if public_nets:
        graph.edge("osd", "net_public", "client I/O" if cluster_nets else "client I/O · replication", "network")
        graph.edge("mon", "net_public", f"lắng nghe :{', :'.join(mon_ports)}" if mon_ports else "lắng nghe", "network")
    if cluster_nets:
        graph.edge("osd", "net_cluster", "replication · recovery", "network")


def _host_status(down: list[int], host_checks: list[dict]) -> str:
    if down or any(check["code"] == "OSD_HOST_DOWN" for check in host_checks):
        return ERROR
    return WARN if host_checks else OK


def _host_node(graph: _Graph, inputs: _Inputs, entry: dict) -> bool:
    """One host node and its edges; True when it also has a management address."""
    name = _short_host(entry.get("node_name") or entry.get("host"))
    addresses = [str(address) for address in entry.get("aliases") or [entry.get("host")] if address]
    osd_ids = inputs.osds_by_host.get(name, [])
    host_checks = _host_mentions(inputs.checks, name, osd_ids)
    down = sorted(set(osd_ids) & inputs.down_osds)
    node_id = f"host_{re.sub(r'[^A-Za-z0-9_]', '_', name)}"
    public_addr = [address for address in addresses if _in_networks(address, inputs.public_nets)]
    cluster_addr = [address for address in addresses if _in_networks(address, inputs.cluster_nets)]
    other_addr = [address for address in addresses if address not in public_addr and address not in cluster_addr]
    osd_addrs = {osd["id"]: osd for osd in inputs.osd_daemons}
    graph.nodes.append(_node(node_id, "hosts", "host", name, " · ".join(entry.get("roles") or []) or "chưa rõ vai trò",
                             _host_status(down, host_checks), [
                                 "OSD: " + (", ".join(f"osd.{osd}{' (down)' if osd in down else ''}" for osd in osd_ids) or "không có"),
                                 "Địa chỉ public: " + (", ".join(public_addr) or "—"),
                                 *(["Địa chỉ cluster: " + ", ".join(cluster_addr)] if inputs.cluster_nets else []),
                                 "Địa chỉ khác (quản trị): " + (", ".join(other_addr) or "—"),
                                 *_host_daemon_facts(name, inputs.mon_daemons, osd_ids, osd_addrs),
                             ], host_checks, "/nodes"))
    for present, target, label, kind in ((osd_ids, "osd", f"{len(osd_ids)} OSD", "data"),
                                         (public_addr, "net_public", "public", "network"),
                                         (cluster_addr, "net_cluster", "cluster", "network"),
                                         (other_addr, "net_management", "SSH", "management")):
        if present:
            graph.edge(node_id, target, label, kind)
    return bool(other_addr)


def _hosts(graph: _Graph, inputs: _Inputs) -> None:
    """Hosts, OSDs grouped by host, and the management network they share."""
    entries = [entry for entry in inputs.nodes_payload.get("nodes") or [] if isinstance(entry, dict)]
    management = [_host_node(graph, inputs, entry) for entry in entries]
    if any(management):
        graph.nodes.append(_node("net_management", "networks", "network", "Mạng quản trị",
                                 "địa chỉ ngoài public/cluster network", OK,
                                 ["SSH của Ceph AI và operator", "Không mang dữ liệu Ceph"]))
        graph.edge("ceph_ai", "net_management", "SSH", "management")


def build(cluster, *, now: datetime | None = None, sections: dict[str, dict | None] | None = None) -> dict:
    """The topology of one cluster from its stored snapshots."""
    now = now or utc_now()
    sections = sections if sections is not None else _read(str(cluster.id))
    health_section = sections.get("health") or {}
    inputs = _inputs(sections)
    stale = bool(not health_section or health_section.get("stale") or not inputs.status
                 or (sections.get("status") or {}).get("stale"))
    graph = _Graph()
    for layer in (_control_plane, _data_path, _clients, _networks_layer, _hosts):
        layer(graph, inputs)
    if stale:
        for node in graph.nodes:
            node["status"] = UNKNOWN
    node_ids = {node["id"] for node in graph.nodes}
    return {
        "schema": SCHEMA,
        "cluster": {"id": str(cluster.id), "name": getattr(cluster, "name", "")},
        "generated_at": now.isoformat(),
        "stale": stale,
        "snapshots": {name: (sections.get(name) or {}).get("collected_at") for name in ("health", "status", "nodes", "crush")},
        "summary": {
            "health": (health_section.get("health") or {}).get("status") or (inputs.status.get("health") or {}).get("status"),
            "mons_in_quorum": len(inputs.quorum), "mons": inputs.num_mons,
            "osds_up": inputs.osd_up, "osds_in": inputs.osd_in, "osds": inputs.osd_total,
            "pgs_active_clean": inputs.active_clean, "pgs": inputs.pg_total,
            "used_percent": inputs.used_pct,
            "public_network": inputs.public_nets, "cluster_network": inputs.cluster_nets,
        },
        "groups": GROUPS,
        "nodes": graph.nodes,
        "edges": [item for item in graph.edges if {item["from"], item["to"]} <= node_ids],
    }
