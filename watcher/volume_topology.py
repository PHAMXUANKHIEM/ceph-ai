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
MAX_DATA_OBJECT_SAMPLES = 8


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


def sample_data_object_names(info: dict, image_id: str) -> tuple[list[str], int]:
    """Return bounded, deterministic samples of RBD data objects.

    RBD object indexes are hexadecimal and zero-padded to 16 characters.
    Sampling across the image avoids treating the metadata header as the
    workload placement while keeping the collector bounded for large images.
    """
    try:
        size = max(0, int(info.get("size") or 0))
        object_size = max(0, int(info.get("object_size") or 0))
        object_count = int(info.get("num_objs") or 0)
    except (TypeError, ValueError):
        size, object_size, object_count = 0, 0, 0
    if object_count <= 0 and size > 0 and object_size > 0:
        object_count = (size + object_size - 1) // object_size
    object_count = max(1, object_count)
    prefix = info.get("block_name_prefix") or f"rbd_data.{image_id}"
    if not isinstance(prefix, str) or not prefix:
        prefix = f"rbd_data.{image_id}"
    indexes = {0, object_count - 1}
    for step in range(1, MAX_DATA_OBJECT_SAMPLES - 1):
        indexes.add(round((object_count - 1) * step / (MAX_DATA_OBJECT_SAMPLES - 1)))
    indexes = sorted(indexes)[:MAX_DATA_OBJECT_SAMPLES]
    return [f"{prefix}.{index:016x}" for index in indexes], object_count


def map_volume(cluster, pool: str, image: str) -> dict:
    """Map bounded samples of RBD data objects to their PG/acting OSD sets."""
    connection = _connection(cluster)
    spec = f"{shlex.quote(pool)}/{shlex.quote(image)}"
    _stdout, info = ceph_client.run_ceph_json_command_with(
        *connection, f"rbd info {spec}",
    )
    if not isinstance(info, dict) or not info.get("id"):
        raise ValueError("rbd info không trả image id")
    image_id = str(info["id"])
    object_names, data_object_count = sample_data_object_names(info, image_id)
    mapped_objects = []
    for object_name in object_names:
        _stdout, mapped = ceph_client.run_ceph_json_command_with(
            *connection,
            f"ceph osd map {shlex.quote(pool)} {shlex.quote(object_name)}",
        )
        result = normalize_osd_map_payload(mapped)
        mapped_objects.append({"object_name": object_name, **result})
    if not mapped_objects:
        raise ValueError("không map được data object nào")
    acting_osds = sorted({osd_id for item in mapped_objects for osd_id in item["acting_osds"]})
    pgids = [item["pgid"] for item in mapped_objects]
    return {
        "pool": pool,
        "image": image,
        "image_id": image_id,
        "object_name": mapped_objects[0]["object_name"],
        "pgid": mapped_objects[0]["pgid"],
        "acting_osds": acting_osds,
        "primary_osd": mapped_objects[0]["primary_osd"],
        "pgids": pgids,
        "sampled_objects": [item["object_name"] for item in mapped_objects],
        "data_object_count": data_object_count,
        "mapping_scope": "data_sample",
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
            row.pgids_json = json.dumps(mapping["pgids"], separators=(",", ":"))
            row.sampled_objects_json = json.dumps(mapping["sampled_objects"], separators=(",", ":"))
            row.data_object_count = mapping["data_object_count"]
            row.mapping_scope = mapping["mapping_scope"]
            row.captured_at = now
            stored += 1
        session.commit()
        return stored
