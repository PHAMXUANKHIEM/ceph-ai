"""Read-only RBD Volume -> PG -> acting OSD mapping collector."""

from __future__ import annotations

import json
import logging
import shlex
from datetime import datetime, timedelta

from config.settings import settings
from shared import db
from shared.cluster_nodes import resolve_ssh_creds
from shared.models import VolumeMetric, VolumeOsdMapping
from watcher import ceph_client
from watcher.ceph_client import CephQueryError

logger = logging.getLogger(__name__)

LOOKBACK_MINUTES = 30
MAX_VOLUMES_PER_SCAN = 50


def _connection(cluster):
    if cluster is None:
        nodes = [node.strip() for node in settings.ceph_mon_nodes.split(",") if node.strip()]
        ssh_user = settings.ssh_user
        ssh_key_path = settings.ssh_key_path
        exec_mode = settings.ceph_exec_mode
        container_name = settings.ceph_container_name
    else:
        nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
        ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(cluster)
    return nodes, container_name, ssh_user, ssh_key_path, exec_mode


def normalize_osd_map_payload(payload: dict | list) -> dict:
    """Normalize the stable fields of ``ceph osd map --format json``."""
    if not isinstance(payload, dict):
        raise ValueError("ceph osd map response không phải object")
    pgid = payload.get("pgid")
    acting = payload.get("acting")
    if not isinstance(pgid, str) or not pgid or not isinstance(acting, list):
        raise ValueError("ceph osd map thiếu pgid/acting")
    acting_osds = []
    for osd_id in acting:
        if isinstance(osd_id, int) and osd_id >= 0:
            acting_osds.append(osd_id)
    if not acting_osds:
        raise ValueError("ceph osd map không có acting OSD")
    return {
        "pgid": pgid,
        "acting_osds": acting_osds,
        "primary_osd": acting_osds[0],
    }


def map_volume(cluster, pool: str, image: str) -> dict:
    """Map the RBD header object, which is present even for an idle image."""
    connection = _connection(cluster)
    spec = f"{shlex.quote(pool)}/{shlex.quote(image)}"
    _stdout, info = ceph_client.run_ceph_json_command_with(
        *connection, f"rbd info {spec}",
    )
    if not isinstance(info, dict) or not info.get("id"):
        raise ValueError("rbd info không trả image id")
    image_id = str(info["id"])
    object_name = f"rbd_header.{image_id}"
    _stdout, mapped = ceph_client.run_ceph_json_command_with(
        *connection,
        f"ceph osd map {shlex.quote(pool)} {shlex.quote(object_name)}",
    )
    result = normalize_osd_map_payload(mapped)
    return {
        "pool": pool,
        "image": image,
        "image_id": image_id,
        "object_name": object_name,
        **result,
    }


def collect_and_store(cluster_id: str, cluster, *, now: datetime | None = None) -> int:
    """Refresh mappings for recently active RBD volumes, best effort."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(minutes=LOOKBACK_MINUTES)
    with db.SessionLocal() as session:
        recent = session.query(VolumeMetric).filter(
            VolumeMetric.cluster_id == cluster_id,
            VolumeMetric.polled_at >= cutoff,
            VolumeMetric.polled_at <= now,
        ).order_by(VolumeMetric.polled_at.desc()).all()
        keys = []
        seen = set()
        for row in recent:
            key = (row.pool, row.image)
            if key not in seen:
                seen.add(key)
                keys.append(key)
                if len(keys) >= MAX_VOLUMES_PER_SCAN:
                    break

        stored = 0
        for pool, image in keys:
            try:
                mapping = map_volume(cluster, pool, image)
            except (CephQueryError, ValueError, KeyError, TypeError) as exc:
                logger.info("volume topology mapping unavailable for %s/%s: %s", pool, image, exc)
                continue
            row = session.get(VolumeOsdMapping, (cluster_id, pool, image))
            if row is None:
                row = VolumeOsdMapping(cluster_id=cluster_id, pool=pool, image=image)
                session.add(row)
            row.image_id = mapping["image_id"]
            row.object_name = mapping["object_name"]
            row.pgid = mapping["pgid"]
            row.acting_osds_json = json.dumps(mapping["acting_osds"], separators=(",", ":"))
            row.primary_osd = mapping["primary_osd"]
            row.captured_at = now
            stored += 1
        session.commit()
        return stored
