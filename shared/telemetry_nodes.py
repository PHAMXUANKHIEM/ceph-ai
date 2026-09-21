"""Helpers for collapsing network aliases into physical telemetry nodes.

Cluster configuration can contain several addresses for the same machine, for
example a MON address and a separate OSD address.  Those addresses must remain
available to Ceph/SSH callers, but host telemetry must be sampled once per
physical machine or the dashboard will show duplicate, slightly different
CPU/RAM values.
"""

from __future__ import annotations

from typing import Mapping


def collapse_nodes(
    nodes: list[dict],
    identities: Mapping[str, str | None] | None = None,
) -> list[dict]:
    """Collapse configured node aliases using a host-provided identity.

    ``nodes`` is intentionally not modified.  The first configured address is
    the preferred display/telemetry address, except that a MON address wins if
    a group was initially encountered through another role.  All original
    addresses are retained in ``aliases`` so old URLs and operational lookups
    can still be resolved safely.
    """
    identities = identities or {}
    groups: dict[str, dict] = {}

    for node in nodes or []:
        host = str(node.get("host") or "").strip()
        if not host:
            continue
        identity = str(identities.get(host) or node.get("node_name") or host).strip() or host
        key = identity.casefold()
        group = groups.get(key)
        if group is None:
            group = {
                "host": host,
                "node_name": identity if identity.casefold() != host.casefold() else None,
                "roles": set(),
                "aliases": [],
            }
            groups[key] = group

        previous_roles = set(group["roles"])
        group["roles"].update(str(role).upper() for role in (node.get("roles") or []))
        aliases = [host, *(node.get("aliases") or [])]
        for alias in aliases:
            alias = str(alias or "").strip()
            if alias and alias not in group["aliases"]:
                group["aliases"].append(alias)
        if "MON" in group["roles"] and "MON" not in previous_roles:
            group["host"] = host

    result = []
    for group in groups.values():
        result.append({
            "host": group["host"],
            "node_name": group["node_name"],
            "roles": sorted(group["roles"]),
            "aliases": list(group["aliases"]),
            "alias_count": len(group["aliases"]),
        })
    return result
