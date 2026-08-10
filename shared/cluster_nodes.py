from __future__ import annotations

from typing import TYPE_CHECKING

from config.settings import settings

if TYPE_CHECKING:
    from shared.models import Cluster


def configured_nodes(cluster: "Cluster | None" = None) -> list[dict]:
    """Every node the operator has configured for this cluster (MON/MGR/OSD),
    deduplicated by host — a small lab cluster commonly collocates multiple
    roles on the same box, so a host can carry more than one role.

    Moved out of dashboard/routes/nodes.py so other callers (dashboard/chat.py's
    tool-calling loop) can reuse the exact same whitelist instead of
    duplicating it — every route that turns a host string into an SSH target
    (SSRF-via-SSH risk) must check against this same list.

    `cluster` (2026-08-10, multi-tenant remediation Phase 1): when given,
    builds the node list from THAT `Cluster` row's own mon/mgr/osd/rgw fields
    instead of the global `settings` singleton. Omitted (the default), this
    function's behavior is byte-for-byte unchanged — every one of its ~30
    existing call sites keeps compiling and behaving exactly as before.
    Multi-cluster callers (worker/llm/router_client.py's per-Incident
    execution) opt in explicitly by passing the `Cluster` row the Incident
    they're handling belongs to."""
    if cluster is not None:
        mon_raw = cluster.ceph_mon_nodes
        mgr_raw = cluster.ceph_mgr_nodes
        osd_raw = cluster.ceph_osd_nodes
        rgw_raw = cluster.ceph_rgw_nodes
    else:
        mon_raw = settings.ceph_mon_nodes
        mgr_raw = settings.ceph_mgr_nodes
        osd_raw = settings.ceph_osd_nodes
        rgw_raw = settings.ceph_rgw_nodes
    mon_nodes = [n.strip() for n in mon_raw.split(",") if n.strip()]
    mgr_nodes = [n.strip() for n in mgr_raw.split(",") if n.strip()]
    osd_nodes = [n.strip() for n in osd_raw.split(",") if n.strip()]
    rgw_nodes = [n.strip() for n in rgw_raw.split(",") if n.strip()]
    roles: dict[str, set[str]] = {}
    for host in mon_nodes:
        roles.setdefault(host, set()).add("MON")
    for host in mgr_nodes:
        roles.setdefault(host, set()).add("MGR")
    for host in osd_nodes:
        roles.setdefault(host, set()).add("OSD")
    for host in rgw_nodes:
        roles.setdefault(host, set()).add("RGW")
    # Preserve MON-then-MGR-then-OSD-then-RGW configured order rather than dict/set order.
    ordered_hosts = list(dict.fromkeys(mon_nodes + mgr_nodes + osd_nodes + rgw_nodes))
    return [{"host": host, "roles": sorted(roles[host])} for host in ordered_hosts]


def resolve_ssh_creds(cluster: "Cluster | None" = None) -> tuple[str, str, str, str]:
    """(ssh_user, ssh_key_path, ceph_exec_mode, ceph_container_name) for
    `cluster`, or the global `settings` singleton's equivalents when `cluster`
    is None (the default cluster) — same opt-in-via-explicit-cluster posture
    as `configured_nodes()` above. Callers that need to SSH into a specific
    cluster's nodes (worker/llm/router_client.py's per-Incident execution,
    worker/executor/commands.py's exec-mode-branching command builders)
    should use this instead of reading `settings.ssh_*`/`settings.ceph_
    exec_mode`/`settings.ceph_container_name` inline, so a multi-cluster
    caller gets the RIGHT cluster's creds instead of always the default's.

    `ceph_container_name` here is the MON container name (same field
    `watcher/ceph_client.py` already means by that name) — RGW's own
    container name is a separate field (`ceph_rgw_container_name`),
    unrelated to this general-purpose helper."""
    if cluster is not None:
        return cluster.ssh_user, cluster.ssh_key_path, cluster.ceph_exec_mode, cluster.ceph_container_name
    return settings.ssh_user, settings.ssh_key_path, settings.ceph_exec_mode, settings.ceph_container_name


def patch_build_node() -> str | None:
    """The Ceph patch build server (dashboard/routes/patch.py) — deliberately
    NOT part of configured_nodes()'s SSH SSRF whitelist above: that list is
    specifically "hosts a Ceph-targeted action may SSH into", and the build
    server is never a valid target for one (it isn't a Ceph node at all), the
    same way a Ceph node must never be resolved as the build server. Returns
    None if unconfigured (blank ceph_patch_build_node)."""
    host = settings.ceph_patch_build_node.strip()
    return host or None
