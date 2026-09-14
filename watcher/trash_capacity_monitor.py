"""Warn when recoverable RBD trash consumes more than 20% of cluster capacity.

The alert must be fail-closed: a partial Ceph scan is not evidence that Trash
is small.  In particular, cephadm-backed clusters can temporarily refuse a
shell while another auxiliary watcher scan holds the host lock.
"""

from __future__ import annotations

import logging

from config.settings import settings
from shared.telegram_alerts import send_trash_capacity_alert
from watcher import ceph_client

logger = logging.getLogger(__name__)

TRASH_CAPACITY_RATIO_THRESHOLD = 0.20
_was_over_threshold = False
_was_over_threshold_by_cluster: dict[str, bool] = {}


def _cluster_connection(cluster) -> tuple[list[str], str, str, str, str]:
    nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    return (
        nodes,
        cluster.ceph_container_name,
        cluster.ssh_user,
        cluster.ssh_key_path,
        cluster.ceph_exec_mode,
    )


def _cluster_pools(cluster) -> list[str]:
    if cluster is None:
        return ceph_client.configured_rbd_pools()
    manual = [p.strip() for p in settings.ceph_rbd_pools.split(",") if p.strip()]
    if manual:
        return manual
    return ceph_client.discover_rbd_pools_with(*_cluster_connection(cluster))


def check_trash_capacity(cluster=None) -> dict:
    """Return aggregate trash usage for all currently discovered RBD pools."""
    pools = _cluster_pools(cluster)
    connection = _cluster_connection(cluster) if cluster is not None else None
    total_trash_bytes = 0
    entry_count = 0
    scanned_pools: list[str] = []
    errors: list[str] = []
    for pool in pools:
        try:
            entries = (
                ceph_client.query_rbd_trash(pool)
                if connection is None
                else ceph_client.query_rbd_trash_with(pool, *connection)
            )
        except ceph_client.CephQueryError as exc:
            logger.warning("check_trash_capacity: skipping pool %s: %s", pool, exc)
            errors.append(f"{pool}: {exc}")
            continue
        scanned_pools.append(pool)
        entry_count += len(entries)
        for entry in entries:
            try:
                used_size = int(entry.get("used_size_bytes", 0) or 0)
            except (TypeError, ValueError):
                errors.append(f"{pool}: Trash entry có used_size_bytes không hợp lệ")
                continue
            total_trash_bytes += max(0, used_size)

    try:
        _host, df = (
            ceph_client.run_ceph_json_command("ceph df")
            if connection is None
            else ceph_client.run_ceph_json_command_with(*connection, "ceph df")
        )
    except ceph_client.CephQueryError as exc:
        errors.append(f"ceph df: {exc}")
        df = {}
    stats = df.get("stats", {}) if isinstance(df, dict) else {}
    try:
        total_bytes = max(0, int(stats.get("total_bytes") or 0))
    except (TypeError, ValueError):
        total_bytes = 0
        errors.append("ceph df: total_bytes không hợp lệ")
    if total_bytes <= 0:
        errors.append("ceph df: total_bytes bằng 0, không thể xác định tỷ lệ Trash")
    measurement_complete = not errors and len(scanned_pools) == len(pools)
    ratio = total_trash_bytes / total_bytes if total_bytes else 0.0
    return {
        "trash_bytes": total_trash_bytes,
        "total_bytes": total_bytes,
        "ratio": ratio,
        "entry_count": entry_count,
        "pools": scanned_pools,
        "errors": errors,
        "measurement_complete": measurement_complete,
        # Never call an incomplete/partial scan "under threshold".  The
        # caller deliberately ignores its state transition below.
        "over_threshold": bool(
            measurement_complete and total_bytes and ratio > TRASH_CAPACITY_RATIO_THRESHOLD
        ),
    }


def check_and_alert(cluster=None) -> dict:
    """Send only on a complete scan's transition into the over-20% state."""
    global _was_over_threshold
    result = check_trash_capacity(cluster) if cluster is not None else check_trash_capacity()
    state_key = str(cluster.id) if cluster is not None else "default"
    previous = (
        _was_over_threshold_by_cluster.get(state_key, False)
        if cluster is not None
        else _was_over_threshold
    )
    if not result["measurement_complete"]:
        logger.warning(
            "check_and_alert: incomplete Trash measurement; keeping previous alert state: %s",
            "; ".join(result["errors"]),
        )
        return result
    over = result["over_threshold"]
    if over and not previous:
        alert_kwargs = {}
        if cluster is not None:
            has_own_channel = bool(cluster.telegram_bot_token and cluster.telegram_chat_id)
            alert_kwargs = {
                "cluster_name": cluster.name,
                "bot_token": cluster.telegram_bot_token if has_own_channel else None,
                "chat_id": cluster.telegram_chat_id if has_own_channel else None,
                "enabled": cluster.telegram_enabled if has_own_channel else None,
            }
        delivered = send_trash_capacity_alert(
            result["trash_bytes"], result["total_bytes"], result["ratio"], result["entry_count"],
            **alert_kwargs,
        )
        # Do not suppress future retries when Telegram is disabled, not
        # configured, or temporarily unavailable.
        if cluster is None:
            _was_over_threshold = bool(delivered)
        else:
            _was_over_threshold_by_cluster[state_key] = bool(delivered)
        if not delivered:
            logger.warning("check_and_alert: Trash threshold alert was not delivered; will retry")
        return result
    if cluster is None:
        _was_over_threshold = over
    else:
        _was_over_threshold_by_cluster[state_key] = over
    return result
