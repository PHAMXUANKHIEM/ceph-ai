from config.settings import settings


def configured_nodes() -> list[dict]:
    """Every node the operator has configured for this cluster (MON/MGR/OSD),
    deduplicated by host — a small lab cluster commonly collocates multiple
    roles on the same box, so a host can carry more than one role.

    Moved out of dashboard/routes/nodes.py so other callers (dashboard/chat.py's
    tool-calling loop) can reuse the exact same whitelist instead of
    duplicating it — every route that turns a host string into an SSH target
    (SSRF-via-SSH risk) must check against this same list.
    """
    mon_nodes = [n.strip() for n in settings.ceph_mon_nodes.split(",") if n.strip()]
    mgr_nodes = [n.strip() for n in settings.ceph_mgr_nodes.split(",") if n.strip()]
    osd_nodes = [n.strip() for n in settings.ceph_osd_nodes.split(",") if n.strip()]
    rgw_nodes = [n.strip() for n in settings.ceph_rgw_nodes.split(",") if n.strip()]
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
