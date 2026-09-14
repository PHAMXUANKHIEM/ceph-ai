"""Ceph-backed inventory queries owned by the Watcher, not Dashboard routes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from datetime import datetime, timedelta
import re

from shared.cluster_nodes import resolve_ssh_creds
from watcher import ceph_client
from watcher.ceph_client import CephQueryError, run_ceph_json_command_with


CEPH_POOL_FLAG_NODELETE = 1 << 4


def _cluster_connection(cluster):
    nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(cluster)
    return nodes, container_name, ssh_user, ssh_key_path, exec_mode


def _uses_mocked_ceph_client() -> bool:
    functions = (ceph_client.run_ceph_json_command, run_ceph_json_command_with)
    return any(getattr(function, "__module__", "") != "watcher.ceph_client" for function in functions)


def _payload_rows(payload: dict | list, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = []
        for key in keys:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _pool_names_by_id(payload: dict | list) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in _payload_rows(payload, "pools"):
        pool_id = row.get("pool_id", row.get("pool", row.get("poolnum")))
        pool_name = row.get("pool_name") or row.get("poolname") or row.get("name")
        if pool_id is not None and pool_name:
            names[str(pool_id)] = str(pool_name)
    return names


def _pool_is_protected(pool: dict) -> bool:
    flag_names = pool.get("flags_names")
    if isinstance(flag_names, list):
        names = {str(item).strip().lower() for item in flag_names}
    else:
        names = {part.lower() for part in re.split(r"[,;\s]+", str(flag_names or "")) if part}
    if "nodelete" in names:
        return True
    flags = pool.get("flags")
    if isinstance(flags, int) and not isinstance(flags, bool):
        return bool(flags & CEPH_POOL_FLAG_NODELETE)
    if isinstance(flags, str) and not flags.strip().isdigit():
        return "nodelete" in {part.lower() for part in re.split(r"[,;\s]+", flags) if part}
    try:
        return bool(int(flags) & CEPH_POOL_FLAG_NODELETE)
    except (TypeError, ValueError):
        return False


def _normalize_pool_rows(
    detail_payload: dict | list,
    df_payload: dict | list,
    stats_payload: dict | list,
    rules_payload: dict | list,
) -> list[dict]:
    df_by_name = {
        str(row.get("name") or row.get("pool_name")): row.get("stats") or {}
        for row in _payload_rows(df_payload, "pools")
        if row.get("name") or row.get("pool_name")
    }
    io_by_name = {
        str(row.get("pool_name") or row.get("name")): row.get("client_io_rate") or row.get("stats") or {}
        for row in _payload_rows(stats_payload, "pool_stats", "pools")
        if row.get("pool_name") or row.get("name")
    }
    rule_names = {
        str(row.get("rule_id")): str(row.get("rule_name") or row.get("name"))
        for row in _payload_rows(rules_payload, "rules")
        if row.get("rule_id") is not None and (row.get("rule_name") or row.get("name"))
    }

    rows = []
    for pool in _payload_rows(detail_payload, "pools"):
        name = pool.get("pool_name") or pool.get("poolname") or pool.get("name")
        if not name:
            continue
        name = str(name)
        df_stats = df_by_name.get(name, {})
        io_stats = io_by_name.get(name, {})
        size = pool.get("size")
        ec_profile = pool.get("erasure_code_profile")
        redundancy = f"EC · {ec_profile}" if ec_profile else (f"{size} replicas" if size is not None else "—")
        rule_id = pool.get("crush_rule")
        rows.append({
            "name": name,
            "redundancy": redundancy,
            "size": size,
            "protected": _pool_is_protected(pool),
            "pgs": pool.get("pg_num") if pool.get("pg_num") is not None else pool.get("pg_num_target", "—"),
            "crush_rule": rule_names.get(str(rule_id), str(rule_id) if rule_id is not None else "—"),
            "used_bytes": df_stats.get("stored", df_stats.get("bytes_used", 0)) or 0,
            "objects": df_stats.get("objects", 0) or 0,
            "read_iops": io_stats.get("read_op_per_sec", io_stats.get("read_iops", 0)) or 0,
            "write_iops": io_stats.get("write_op_per_sec", io_stats.get("write_iops", 0)) or 0,
        })
    return sorted(rows, key=lambda row: row["name"])


def _format_bytes(value) -> str:
    try:
        amount = max(0.0, float(value))
    except (TypeError, ValueError):
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"


def collect_pool_rows(cluster) -> list[dict]:
    """Collect and normalize Pool inventory for one cluster."""
    commands = (
        "ceph osd pool ls detail",
        "ceph df detail",
        "ceph osd pool stats",
        "ceph osd crush rule dump",
    )
    connection = _cluster_connection(cluster)
    if _uses_mocked_ceph_client():
        def fetch(command: str):
            if cluster.is_default:
                return ceph_client.run_ceph_json_command(command)[1]
            return run_ceph_json_command_with(*connection, command)[1]

        with ThreadPoolExecutor(max_workers=4) as executor:
            payloads = list(executor.map(fetch, commands))
    else:
        _host, payloads = ceph_client.run_ceph_json_batch_command_with(
            *connection, [f"{command} --format json" for command in commands]
        )
        if any(payload is None for payload in payloads):
            raise CephQueryError("One or more Pool queries failed")
    rows = _normalize_pool_rows(*payloads)
    for row in rows:
        row["used"] = _format_bytes(row.pop("used_bytes"))
    return rows


def _normalize_pg_rows(payload: dict | list, pool_names: dict[str, str] | None = None) -> list[dict]:
    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, dict):
        raw_rows = payload.get("pg_stats") or payload.get("pgs") or []
    else:
        raw_rows = []

    rows = []
    for pg in raw_rows:
        if not isinstance(pg, dict):
            continue
        pgid = str(pg.get("pgid", "—"))
        pool_id = pg.get("pool_id", pg.get("pool"))
        if pool_id is None:
            pool_id = pgid.split(".", 1)[0] if "." in pgid else ""
        acting = pg.get("acting") if isinstance(pg.get("acting"), list) else []
        up = pg.get("up") if isinstance(pg.get("up"), list) else []
        primary = pg.get("acting_primary", pg.get("up_primary"))
        if primary is None:
            primary = acting[0] if acting else "—"
        rows.append({
            "pgid": pgid,
            "state": pg.get("state", "unknown"),
            "pool": (pool_names or {}).get(str(pool_id), str(pool_id) or "—"),
            "up": up,
            "acting": acting,
            "primary": primary,
            "last_scrub": pg.get("last_scrub_stamp") or "—",
            "last_deep_scrub": pg.get("last_deep_scrub_stamp") or "—",
        })
    return sorted(rows, key=lambda row: str(row["pgid"]))


def collect_pg_rows(cluster) -> list[dict]:
    """Collect and normalize PG inventory for one cluster."""
    connection = _cluster_connection(cluster)
    if _uses_mocked_ceph_client():
        if cluster.is_default:
            pool_payload = ceph_client.run_ceph_json_command("ceph osd pool ls detail")[1]
            pg_payload = ceph_client.run_ceph_json_command("ceph pg dump pgs")[1]
        else:
            pool_payload = run_ceph_json_command_with(*connection, "ceph osd pool ls detail")[1]
            pg_payload = run_ceph_json_command_with(*connection, "ceph pg dump pgs")[1]
    else:
        _host, payloads = ceph_client.run_ceph_json_batch_command_with(
            *connection,
            ["ceph osd pool ls detail --format json", "ceph pg dump pgs --format json"],
        )
        if any(payload is None for payload in payloads):
            raise CephQueryError("One or more PG queries failed")
        pool_payload, pg_payload = payloads
    return _normalize_pg_rows(pg_payload, _pool_names_by_id(pool_payload))


def _crush_distribution(cluster_id: str, include_legacy_null: bool) -> dict[int, dict]:
    from shared import db
    from shared.models import CrushOsdDistribution
    from sqlalchemy import or_

    with db.SessionLocal() as session:
        scope = CrushOsdDistribution.cluster_id == cluster_id
        if include_legacy_null:
            scope = or_(scope, CrushOsdDistribution.cluster_id.is_(None))
        rows = session.query(CrushOsdDistribution).filter(scope).all()
        return {
            row.osd_id: {
                "host": row.host,
                "bytes_used": row.bytes_used,
                "bytes_total": row.bytes_total,
                "pgs": row.pgs,
            }
            for row in rows
        }


def _crush_changes(diff: dict | None, changed_at: str) -> dict[int, dict]:
    changes = {}
    for kind in ("added", "reweighted"):
        for item in (diff or {}).get(kind) or []:
            node_id = item.get("id")
            if node_id is None:
                continue
            change = {
                "kind": kind,
                "name": item.get("name"),
                "type": item.get("type"),
                "changed_at": changed_at,
            }
            if kind == "reweighted":
                change.update(old_weight=item.get("old_weight"), new_weight=item.get("new_weight"))
            changes[node_id] = change
    return changes


def _sum_known(nodes: list[dict], key: str):
    values = [node[key] for node in nodes if node.get(key) is not None]
    return (sum(values), len(values)) if values else (None, 0)


def _augment_crush_node(node: dict, distribution: dict[int, dict], changes: dict[int, dict]) -> dict:
    node_id = node.get("id")
    if node.get("type") == "osd":
        row = distribution.get(node_id)
        return {
            "id": node_id,
            "name": node.get("name"),
            "type": "osd",
            "weight": node.get("weight"),
            "weight_normalized": node.get("weight") / 65536 if isinstance(node.get("weight"), (int, float)) else None,
            "host": row.get("host") if row else None,
            "bytes_used": row.get("bytes_used") if row else None,
            "bytes_total": row.get("bytes_total") if row else None,
            "pgs": row.get("pgs") if row else None,
            "has_distribution_data": row is not None,
            "partial_distribution_data": False,
            "recent_change": changes.get(node_id),
            "children": [],
        }
    children = [_augment_crush_node(child, distribution, changes) for child in node.get("children") or []]
    used, used_count = _sum_known(children, "bytes_used")
    total, total_count = _sum_known(children, "bytes_total")
    pgs, pgs_count = _sum_known(children, "pgs")
    contributors = max(used_count, total_count, pgs_count)
    return {
        "id": node_id,
        "name": node.get("name"),
        "type": node.get("type"),
        "weight": node.get("weight"),
        "weight_normalized": node.get("weight") / 65536 if isinstance(node.get("weight"), (int, float)) else None,
        "bytes_used": used,
        "bytes_total": total,
        "pgs": pgs,
        "has_distribution_data": contributors > 0,
        "partial_distribution_data": 0 < contributors < len(children),
        "recent_change": changes.get(node_id),
        "children": children,
    }


def _crush_tree_has_osd(node: dict) -> bool:
    if node.get("type") == "osd":
        return True
    return any(_crush_tree_has_osd(child) for child in node.get("children") or [])


def build_crush_tree_response(latest, cluster_id: str, include_legacy_null: bool = False) -> dict:
    tree = json.loads(latest.tree_json)
    roots = tree.get("roots") or []
    rules = tree.get("rules") or []
    created_at = latest.created_at.isoformat()
    if not any(_crush_tree_has_osd(node) for node in roots):
        return {
            "state": "empty_cluster",
            "snapshot_id": latest.id,
            "created_at": created_at,
            "rules": rules,
        }
    diff = json.loads(latest.diff_json) if latest.diff_json else None
    changes = (
        _crush_changes(diff, created_at)
        if diff is not None and datetime.utcnow() - latest.created_at <= timedelta(hours=24)
        else {}
    )
    distribution = _crush_distribution(cluster_id, include_legacy_null)
    return {
        "state": "ok",
        "snapshot_id": latest.id,
        "created_at": created_at,
        "roots": [_augment_crush_node(root, distribution, changes) for root in roots],
        "rules": rules,
    }
