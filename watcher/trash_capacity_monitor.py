"""Warn when recoverable RBD trash consumes more than 20% of cluster capacity.

The alert must be fail-closed: a partial Ceph scan is not evidence that Trash
is small.  In particular, cephadm-backed clusters can temporarily refuse a
shell while another auxiliary watcher scan holds the host lock.
"""

from __future__ import annotations

import hashlib
import logging

from config.settings import settings
from shared import db, telegram_outbox
from shared.clusters import ensure_default_cluster
from shared.models import RbdTrashUsage
from shared.telegram_alerts import send_trash_capacity_alert as _send_trash_capacity_alert_direct
from watcher import ceph_client

logger = logging.getLogger(__name__)

TRASH_CAPACITY_RATIO_THRESHOLD = 0.20
_was_over_threshold = False
_was_over_threshold_by_cluster: dict[str, bool] = {}


def send_trash_capacity_alert(
    trash_bytes: int,
    total_bytes: int,
    ratio: float,
    entry_count: int,
    *,
    cluster_name: str | None = None,
    cluster_id: str | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
) -> bool:
    """Queue Trash threshold alerts without persisting channel credentials."""
    fingerprint = hashlib.sha256(
        f"{cluster_id}|{cluster_name}|{trash_bytes}|{total_bytes}|{ratio}|{entry_count}".encode(
            "utf-8"
        )
    ).hexdigest()[:24]

    def sender(*args, **kwargs):
        return _send_trash_capacity_alert_direct(
            *args,
            cluster_name=cluster_name,
            bot_token=bot_token,
            chat_id=chat_id,
            enabled=enabled,
        )

    return telegram_outbox.enqueue_alert_call_and_dispatch(
        event_id=f"trash-capacity:{cluster_id or 'default'}:{fingerprint}",
        category="capacity",
        function="send_trash_capacity_alert",
        args=(trash_bytes, total_bytes, ratio, entry_count),
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        sender=sender,
    )


def _cluster_connection(cluster) -> tuple[list[str], str, str, str, str]:
    nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    return (
        nodes,
        cluster.ceph_container_name,
        cluster.ssh_user,
        cluster.ssh_key_path,
        cluster.ceph_exec_mode,
    )


def _list_trash(pool: str, connection) -> list[dict]:
    """List one pool's Trash with measured capacity.

    This monitor's whole job is the trash/capacity ratio, so it needs the real
    allocated bytes that ceph_client measures from the pool's RADOS object
    listing. That scan costs one batched ``rbd info`` round trip plus one
    ``rados ls`` per pool — not one round trip per entry — and ceph_client
    skips it on a pool too large to list, in which case the saved snapshot
    below fills the gap.
    """
    query = ceph_client.query_rbd_trash if connection is None else ceph_client.query_rbd_trash_with
    args = (pool,) if connection is None else (pool, *connection)
    return query(*args)


def _saved_usage_by_trash_id(cluster, pool: str) -> dict[str, int]:
    """Allocated bytes recorded when the Dashboard moved an image to Trash.

    ``rbd trash ls``/``rbd info`` expose no allocated byte count for a trashed
    image, so ceph_client reports ``used_size_bytes=None`` for every entry.
    The ``rbd_trash_usages`` snapshot written at ``rbd trash mv`` time is the
    only place this number exists; without consulting it this monitor marks
    every scan incomplete and can never cross its own threshold.
    """
    try:
        with db.SessionLocal() as session:
            cluster_id = str(cluster.id) if cluster is not None else ensure_default_cluster(session).id
            rows = (
                session.query(RbdTrashUsage.trash_id, RbdTrashUsage.used_size_bytes)
                .filter(RbdTrashUsage.cluster_id == cluster_id, RbdTrashUsage.pool == pool)
                .all()
            )
        return {str(trash_id): int(used) for trash_id, used in rows}
    except Exception:
        # A snapshot lookup failure must degrade to "unknown usage" — the
        # fail-closed path this module already handles — never to zero.
        logger.exception("_saved_usage_by_trash_id: cannot read saved usage for pool %s", pool)
        return {}


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
    usage_known = True
    for pool in pools:
        try:
            entries = _list_trash(pool, connection)
        except ceph_client.CephQueryError as exc:
            logger.warning("check_trash_capacity: skipping pool %s: %s", pool, exc)
            errors.append(f"{pool}: {exc}")
            continue
        scanned_pools.append(pool)
        entry_count += len(entries)
        # Only pay for the snapshot lookup when Ceph itself left a gap, which
        # keeps a fully-populated listing (and the test doubles that emulate
        # one) from touching the database at all.
        saved_usage = (
            _saved_usage_by_trash_id(cluster, pool)
            if any(entry.get("used_size_bytes") is None for entry in entries)
            else {}
        )
        for entry in entries:
            try:
                raw_used_size = entry.get("used_size_bytes")
                if raw_used_size is None:
                    raw_used_size = saved_usage.get(str(entry.get("id")))
                if raw_used_size is None:
                    usage_known = False
                    continue
                used_size = int(raw_used_size)
            except (TypeError, ValueError):
                errors.append(f"{pool}: Trash entry có used_size_bytes không hợp lệ")
                usage_known = False
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
    if not usage_known:
        errors.append("Trash: không xác định được allocated bytes của mọi image")
    measurement_complete = not errors and len(scanned_pools) == len(pools)
    # Do not turn an unknown allocated size into zero and falsely clear an
    # existing threshold alert.
    ratio = total_trash_bytes / total_bytes if total_bytes and usage_known else 0.0
    return {
        "trash_bytes": total_trash_bytes if usage_known else None,
        "total_bytes": total_bytes,
        "ratio": ratio,
        "entry_count": entry_count,
        "pools": scanned_pools,
        "errors": errors,
        "measurement_complete": measurement_complete,
        "usage_known": usage_known,
        # Never call an incomplete/partial scan "under threshold".  The
        # caller deliberately ignores its state transition below.
        "over_threshold": bool(
            measurement_complete and usage_known and total_bytes and ratio > TRASH_CAPACITY_RATIO_THRESHOLD
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
                "cluster_id": str(cluster.id),
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
