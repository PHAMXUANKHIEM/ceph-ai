"""Warn when recoverable RBD trash consumes more than 20% of cluster capacity."""

from __future__ import annotations

import logging

from shared.telegram_alerts import send_trash_capacity_alert
from watcher import ceph_client

logger = logging.getLogger(__name__)

TRASH_CAPACITY_RATIO_THRESHOLD = 0.20
_was_over_threshold = False


def check_trash_capacity() -> dict:
    """Return aggregate trash usage for all currently discovered RBD pools."""
    pools = ceph_client.configured_rbd_pools()
    total_trash_bytes = 0
    entry_count = 0
    scanned_pools: list[str] = []
    for pool in pools:
        try:
            entries = ceph_client.query_rbd_trash(pool)
        except ceph_client.CephQueryError as exc:
            logger.warning("check_trash_capacity: skipping pool %s: %s", pool, exc)
            continue
        scanned_pools.append(pool)
        entry_count += len(entries)
        total_trash_bytes += sum(max(0, int(entry.get("size_bytes", 0))) for entry in entries)

    _host, df = ceph_client.run_ceph_json_command("ceph df")
    stats = df.get("stats", {}) if isinstance(df, dict) else {}
    total_bytes = max(0, int(stats.get("total_bytes") or 0))
    ratio = total_trash_bytes / total_bytes if total_bytes else 0.0
    return {
        "trash_bytes": total_trash_bytes,
        "total_bytes": total_bytes,
        "ratio": ratio,
        "entry_count": entry_count,
        "pools": scanned_pools,
        "over_threshold": bool(total_bytes and ratio > TRASH_CAPACITY_RATIO_THRESHOLD),
    }


def check_and_alert() -> dict:
    """Send only on the transition into the over-20% state."""
    global _was_over_threshold
    result = check_trash_capacity()
    over = result["over_threshold"]
    if over and not _was_over_threshold:
        send_trash_capacity_alert(
            result["trash_bytes"], result["total_bytes"], result["ratio"], result["entry_count"]
        )
    _was_over_threshold = over
    return result
