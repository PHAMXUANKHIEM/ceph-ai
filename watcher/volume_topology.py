"""Read-only RBD Volume -> PG -> acting OSD mapping collector."""

from __future__ import annotations

import json
import logging
import shlex
from datetime import datetime, timedelta
from shared.time import utc_now

from config.settings import settings
from shared import db
from shared.cluster_nodes import resolve_ssh_creds
from shared.models import VolumeMetric, VolumeOsdMapping
from watcher import ceph_client
from watcher.ceph_client import CephQueryError

logger = logging.getLogger(__name__)

LOOKBACK_MINUTES = 30
MAX_VOLUMES_PER_SCAN = 10
MAX_DATA_OBJECT_SAMPLES = 2


def _connection(cluster):
    if cluster is None or not hasattr(cluster, "ceph_mon_nodes"):
        nodes = ceph_client.get_mon_nodes()
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


def sample_data_object_names(
    info: dict, image_id: str, *, max_samples: int | None = None
) -> tuple[list[str], int]:
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
    sample_limit = max(2, int(max_samples or MAX_DATA_OBJECT_SAMPLES))
    indexes = {0, object_count - 1}
    for step in range(1, sample_limit - 1):
        indexes.add(round((object_count - 1) * step / (sample_limit - 1)))
    indexes = sorted(indexes)[:sample_limit]
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
    object_names, data_object_count = sample_data_object_names(
        info,
        image_id,
        max_samples=max(
            2,
            int(getattr(settings, "volume_topology_max_data_object_samples", MAX_DATA_OBJECT_SAMPLES)),
        ),
    )
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


def map_volumes(cluster, keys: list[tuple[str, str]]) -> list[tuple[str, str, dict]]:
    """Map several recent volumes using two bounded remote shells."""
    if not keys:
        return []
    connection = _connection(cluster)
    info_commands = [
        f"rbd info {shlex.quote(pool)}/{shlex.quote(image)} --format json"
        for pool, image in keys
    ]
    try:
        _info_host, infos = ceph_client.run_ceph_json_batch_command_with(
            *connection, info_commands
        )
    except CephQueryError as exc:
        logger.info("volume topology info batch unavailable: %s", exc)
        return []

    requests = []
    grouped: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for key, info in zip(keys, infos):
        pool, image = key
        if not isinstance(info, dict) or not info.get("id"):
            continue
        image_id = str(info["id"])
        object_names, data_object_count = sample_data_object_names(
            info,
            image_id,
            max_samples=max(
                2,
                int(getattr(settings, "volume_topology_max_data_object_samples", MAX_DATA_OBJECT_SAMPLES)),
            ),
        )
        grouped[key] = [(object_name, data_object_count) for object_name in object_names]
        requests.extend(
            (key, object_name)
            for object_name in object_names
        )
    if not requests:
        return []

    map_commands = [
        f"ceph osd map {shlex.quote(pool)} {shlex.quote(object_name)} --format json"
        for (pool, _image), object_name in requests
    ]
    try:
        _map_host, mapped_payloads = ceph_client.run_ceph_json_batch_command_with(
            *connection, map_commands
        )
    except CephQueryError as exc:
        logger.info("volume topology map batch unavailable: %s", exc)
        return []

    mapped_by_key: dict[tuple[str, str], list[dict]] = {}
    request_index = 0
    for key in grouped:
        mapped_objects = []
        for object_name, _data_object_count in grouped[key]:
            payload = mapped_payloads[request_index]
            request_index += 1
            if payload is None:
                mapped_objects = []
                break
            try:
                mapped_objects.append({
                    "object_name": object_name,
                    **normalize_osd_map_payload(payload),
                })
            except (TypeError, ValueError, KeyError):
                mapped_objects = []
                break
        if mapped_objects:
            mapped_by_key[key] = mapped_objects

    results = []
    for pool, image in keys:
        mapped_objects = mapped_by_key.get((pool, image))
        info = infos[keys.index((pool, image))]
        if not mapped_objects or not isinstance(info, dict):
            continue
        try:
            image_id = str(info["id"])
            _objects, data_object_count = sample_data_object_names(info, image_id)
            acting_osds = sorted({
                osd_id
                for item in mapped_objects
                for osd_id in item["acting_osds"]
            })
            results.append((
                pool,
                image,
                {
                    "pool": pool,
                    "image": image,
                    "image_id": image_id,
                    "object_name": mapped_objects[0]["object_name"],
                    "pgid": mapped_objects[0]["pgid"],
                    "acting_osds": acting_osds,
                    "primary_osd": mapped_objects[0]["primary_osd"],
                    "pgids": [item["pgid"] for item in mapped_objects],
                    "sampled_objects": [item["object_name"] for item in mapped_objects],
                    "data_object_count": data_object_count,
                    "mapping_scope": "data_sample",
                },
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return results
def collect_and_store(cluster_id: str, cluster, *, now: datetime | None = None) -> int:
    """Refresh mappings for recently active RBD volumes, best effort."""
    now = now or utc_now()
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
                if len(keys) >= max(
                    1, int(getattr(settings, "volume_topology_max_volumes_per_scan", MAX_VOLUMES_PER_SCAN))
                ):
                    break

        stored = 0
        for pool, image, mapping in map_volumes(cluster, keys):
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
