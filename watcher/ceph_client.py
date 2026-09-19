import atexit
import fcntl
import base64
import hashlib
import json
import logging
import os
import re
import shlex
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Callable, TypedDict

import paramiko
from paramiko.hostkeys import HostKeyEntry, InvalidHostKey

from config.settings import settings
from shared import ceph_releases
from shared.ceph_runner import CephCommandRunner, CephConnectionPool, CephRunnerError, CephSSHConfig
from shared.retry import RetryPolicy, retry_sync

logger = logging.getLogger(__name__)

CEPH_HEALTH_INNER_COMMAND = "ceph health detail --format json"
CONNECT_TIMEOUT_SECONDS = settings.ceph_ssh_connect_timeout
COMMAND_TIMEOUT_SECONDS = settings.ceph_command_timeout
HEALTH_COMMAND_TIMEOUT_SECONDS = settings.ceph_health_timeout
# Health is queried from alternate MONs, so retrying the same MON repeatedly
# only amplifies cephadm/SSH load while that MON is already unhealthy.
CEPH_HEALTH_MAX_RETRIES_PER_MON = 1
# cephadm shell spins up a fresh container per invocation (infers fsid/config/
# keyring itself) rather than exec-ing into an already-running one — measured
# ~2.5s against a real cephadm/reef cluster, comfortably under this but with
# headroom for a cold image pull or a slower link.
CEPHADM_COMMAND_TIMEOUT_SECONDS = settings.ceph_inventory_timeout
# Container layers are replaced on every deploy, so a host-key pin file in
# `/root/.ssh` would disappear with the container.  Keep it under the shared
# persistent application volume instead; bare-metal installs retain the
# historical path.  The optional override also supports an externally
# provisioned, read-only known_hosts file.
_DEFAULT_KNOWN_HOSTS_PATH = (
    "/var/lib/ceph-ai/ssh/ceph_lab_known_hosts"
    if os.environ.get("CEPH_AI_CONTAINERIZED", "").lower() == "true"
    else os.path.expanduser("~/.ssh/ceph_lab_known_hosts")
)
KNOWN_HOSTS_PATH = os.environ.get("CEPH_AI_SSH_KNOWN_HOSTS_PATH", _DEFAULT_KNOWN_HOSTS_PATH)
VALID_STATUSES = {"HEALTH_OK", "HEALTH_WARN", "HEALTH_ERR"}
VALID_EXEC_MODES = {"docker", "podman", "cephadm", "none"}

# Dashboard Nodes-page CLI (read-only diagnostics): command_id -> the exact
# `ceph`-family command run. This is the ONLY set of commands that feature
# can ever execute — command_id is a dict key looked up server-side, never
# a client-supplied command string, so there is no path from that UI to
# arbitrary shell (see run_diagnostic_command below).
DIAGNOSTIC_COMMANDS: dict[str, str] = {
    "ceph_status": "ceph -s",
    "ceph_health_detail": "ceph health detail",
    "ceph_osd_tree": "ceph osd tree",
    "ceph_osd_df": "ceph osd df",
    "ceph_df": "ceph df",
    "ceph_versions": "ceph versions",
}
# An operator-facing audit record, not a log store — long output (e.g. a
# large `ceph osd tree`) is truncated rather than growing the DB unbounded.
DIAGNOSTIC_OUTPUT_MAX_CHARS = 8000

# mcp_ceph_server.py's read-only cluster-query tools (Chat-with-AI, Part 2) —
# same non-cephadm/cephadm split as COMMAND_TIMEOUT_SECONDS/
# CEPHADM_COMMAND_TIMEOUT_SECONDS above, just a slightly longer default
# (these are chat-triggered, on-demand queries, not the Watcher's tight
# poll loop, so a couple more seconds of headroom costs nothing real).
MCP_COMMAND_TIMEOUT_SECONDS = settings.ceph_command_timeout

# `rbd perf image iostat` is a streaming command unless an iteration count is
# supplied.  Keep the collection finite at the Ceph CLI level and also put a
# remote-side deadline around it: a Paramiko channel timeout only closes the
# SSH channel and can leave the remote `rbd` child re-parented to PID 1.
RBD_IOSTAT_REMOTE_TIMEOUT_SECONDS = 8
CEPHADM_KEYRING_TARGET = "/etc/ceph/ceph.client.admin.keyring"
# Trash-capacity scans can inspect many images and run alongside the other
# auxiliary watcher scans.  Ten seconds made every contended cephadm shell
# fail with exit 1 and caused false "Trash is below threshold" readings.
CEPHADM_LOCK_WAIT_SECONDS = 30
CEPHADM_REMOTE_LOCK_PATH = "/run/ceph-ai-cephadm.lock"
CEPHADM_REMOTE_TIMEOUT_GRACE_SECONDS = 2

_HEALTH_POOL_LOCK = threading.RLock()
_HEALTH_POOLS: dict[tuple[object, ...], CephConnectionPool] = {}


def _get_shared_health_pool(
    ssh_user: str,
    ssh_key_path: str,
) -> CephConnectionPool:
    """Return one reusable SSH pool for a credential/timeout profile."""
    max_connections = max(1, settings.ceph_max_concurrency)
    config = CephSSHConfig(
        user=ssh_user,
        key_path=ssh_key_path,
        known_hosts_path=KNOWN_HOSTS_PATH,
        connect_timeout=settings.ceph_ssh_connect_timeout,
        banner_timeout=settings.ceph_ssh_banner_timeout,
        auth_timeout=settings.ceph_ssh_auth_timeout,
        max_connections=max_connections,
    )
    key = (
        config.user,
        config.key_path,
        config.known_hosts_path,
        config.connect_timeout,
        config.banner_timeout,
        config.auth_timeout,
        config.max_connections,
    )
    with _HEALTH_POOL_LOCK:
        pool = _HEALTH_POOLS.get(key)
        if pool is None:
            pool = CephConnectionPool(config)
            _HEALTH_POOLS[key] = pool
        return pool


def _close_shared_health_pools() -> None:
    """Close reusable health pools during orderly process shutdown."""
    with _HEALTH_POOL_LOCK:
        pools = list(_HEALTH_POOLS.values())
        _HEALTH_POOLS.clear()
    for pool in pools:
        pool.close()


atexit.register(_close_shared_health_pools)


def _rbd_iostat_base_command(pool: str, keyring_path: str) -> str:
    return (
        "timeout --signal=TERM --kill-after=2s "
        f"{RBD_IOSTAT_REMOTE_TIMEOUT_SECONDS}s rbd --keyring {shlex.quote(keyring_path)} "
        "perf image iostat "
        f"{shlex.quote(pool)} --iterations 1 --format json"
    )


def _rbd_iostat_command(pool: str, exec_mode: str, keyring_path: str) -> str:
    base = _rbd_iostat_base_command(pool, keyring_path)
    if exec_mode != "none":
        return base
    # Some imported clusters are configured as package/ceph-deploy (none)
    # while individual MON hosts only carry cephadm.  Keep the normal host
    # binary as first choice, then use cephadm shell only when rbd is absent.
    cephadm_inner = _rbd_iostat_base_command(pool, CEPHADM_KEYRING_TARGET)
    cephadm_base = (
        f"cephadm shell --mount {shlex.quote(keyring_path)}:{CEPHADM_KEYRING_TARGET} -- "
        f"{cephadm_inner}"
    )
    return (
        f"if command -v rbd >/dev/null 2>&1; then {base}; "
        f"elif command -v cephadm >/dev/null 2>&1; then {cephadm_base}; "
        "else echo 'rbd and cephadm are unavailable' >&2; exit 127; fi"
    )


def build_exec_command(exec_mode: str, container: str, inner_command: str) -> str:
    """Wraps `inner_command` for how this cluster is actually deployed —
    not every Ceph cluster is a plain `docker run` the way this lab's is:

    - "docker"  — `docker exec {container} <cmd>` (a plain `docker run` cluster)
    - "podman"  — `podman exec {container} <cmd>` (a plain `podman run` cluster
      with one fixed, known container name — NOT cephadm, see below)
    - "cephadm" — `cephadm shell -- <cmd>` — cephadm infers the right fsid/
      config/keyring itself; `container` is ignored, no name needed at all.
      This is NOT the same as "podman": a real cephadm mon container has no
      admin keyring mounted (`ceph` run directly inside it fails with
      "unable to find a keyring") and its name is auto-generated/per-host
      (e.g. `ceph-<fsid>-mon-<hostname>`) rather than one fixed name — both
      verified against a real cephadm/reef cluster.
    - "none"    — `<cmd>` run directly, no container at all (ceph-deploy or a
      package install with `ceph` native on the host) — `container` is
      ignored in this mode.

    Raises ValueError on an unrecognized mode rather than silently falling
    back to one wrapping style — a typo'd mode must fail loudly, not quietly
    run the wrong command shape against a real cluster.
    """
    if exec_mode not in VALID_EXEC_MODES:
        raise ValueError(f"unknown ceph_exec_mode: {exec_mode!r} (expected one of {sorted(VALID_EXEC_MODES)})")
    if exec_mode == "none":
        return inner_command
    if exec_mode == "cephadm":
        return f"cephadm shell -- {inner_command}"
    return f"{exec_mode} exec {shlex.quote(container)} {inner_command}"

# The MON node that last answered query_cluster_health() successfully — used
# by watcher/collector.py as a better fallback than "always the first
# configured node" when a check's detail text names no specific mon.
last_successful_mon_node: str | None = None


def ordered_mon_nodes(mon_nodes: list[str]) -> list[str]:
    """Prefer the last MON that answered, preserving configured fallbacks."""
    nodes = list(dict.fromkeys(node.strip() for node in mon_nodes if node and node.strip()))
    if last_successful_mon_node in nodes:
        return [last_successful_mon_node, *[node for node in nodes if node != last_successful_mon_node]]
    return nodes


def _balanced_query_mon_nodes(mon_nodes: list[str], command: str, exec_mode: str) -> list[str]:
    """Spread independent read queries across MONs without weakening fallback.

    cephadm starts a transient shell container for every command, and the
    host-local lock serializes those starts on one MON.  A stable hash keeps
    retries deterministic while sending different pool/image queries to
    different MONs; failures still fall through the complete ordered list.
    Health polling intentionally keeps its sticky MON behavior separately.
    """
    nodes = ordered_mon_nodes(mon_nodes)
    if exec_mode != "cephadm" or len(nodes) < 2:
        return nodes
    offset = int(hashlib.sha256(command.encode("utf-8")).hexdigest()[:8], 16) % len(nodes)
    return nodes[offset:] + nodes[:offset]

class CephQueryError(Exception):
    """Raised when no MON node could be reached and queried successfully."""


def get_mon_nodes() -> list[str]:
    return ordered_mon_nodes(settings.ceph_mon_nodes.split(","))


def _normalize_rbd_pools(payload: dict | list) -> list[str]:
    """Extract RBD-enabled pool names from ``ceph osd pool ls detail``."""
    raw_pools = payload if isinstance(payload, list) else payload.get("pools") if isinstance(payload, dict) else None
    if not isinstance(raw_pools, list):
        logger.warning(
            "discover_rbd_pools: unexpected response shape from 'ceph osd pool ls detail' "
            "— treating as no pools found"
        )
        return []

    pools: list[str] = []
    for entry in raw_pools:
        if not isinstance(entry, dict):
            continue
        name = entry.get("pool_name")
        applications = entry.get("application_metadata")
        if name and isinstance(applications, dict) and "rbd" in applications:
            pools.append(str(name))
    return sorted(pools)


def discover_rbd_pools() -> list[str]:
    """Auto-detects which pools have the RBD application enabled (the same
    `application_metadata` flag `rbd pool init`/`ceph osd pool application
    enable <pool> rbd` sets, and the same signal `rbd`'s own tooling uses to
    decide "this is an RBD pool") via `ceph osd pool ls detail --format
    json`. The bare-list response shape for this exact command is already
    verified against a real cephadm/reef cluster (see
    run_ceph_json_command's own docstring) — only the per-pool
    `application_metadata` key name here follows Ceph's documented (not
    independently re-verified this session) JSON schema, so this carries
    less risk than query_rbd_iostat/query_rbd_trash's fully-unverified
    schemas below.

    configured_rbd_pools() below is the only caller — this just answers
    "which pools currently look RBD-backed," it doesn't decide what an
    empty result or a query failure means to a caller.
    """
    _, payload = run_ceph_json_command("ceph osd pool ls detail")
    return _normalize_rbd_pools(payload)


def discover_rbd_pools_with(
    mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> list[str]:
    """Cluster-scoped counterpart to :func:`discover_rbd_pools`."""
    _, payload = run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
        "ceph osd pool ls detail",
    )
    return _normalize_rbd_pools(payload)


def configured_rbd_pools() -> list[str]:
    """Pools watcher/volume_monitor.py polls for per-image RBD performance.

    settings.ceph_rbd_pools (comma-separated in .env) ALWAYS wins when set
    — an explicit operator opt-in/scoping knob (restrict polling to a
    subset, e.g. for SSH-call cost control, or exclude a pool on purpose).
    Left blank (the default), this auto-discovers every RBD-application
    pool via discover_rbd_pools() above instead of requiring manual setup
    — a cluster query failure (no MON reachable yet, nothing configured at
    all) degrades to an empty list, logged rather than raised, same
    best-effort posture as every other live query in this codebase, so
    callers (Dashboard route handlers, the Watcher poll loop) never need
    their own try/except just to call this.
    """
    manual = [p.strip() for p in settings.ceph_rbd_pools.split(",") if p.strip()]
    if manual:
        return manual
    try:
        return discover_rbd_pools()
    except CephQueryError as exc:
        logger.warning("configured_rbd_pools: auto-discovery failed: %s", exc)
        return []


# `rbd perf image iostat --format json` reports the native `read_latency`
# and `write_latency` counters in nanoseconds. Keep support for an explicit
# *_latency_ms key (useful for adapters/tests), but never label the native
# counter as milliseconds without converting it first.
class VolumeIoSample(TypedDict):
    pool: str
    image: str
    iops: float
    read_latency_ms: float
    write_latency_ms: float


class RbdInventoryEntry(TypedDict):
    name: str
    image_id: str | None
    provisioned_size: int
    used_size: int
    used_percent: float
    snapshot_count: int


def _as_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _normalize_rbd_inventory(payload: dict | list) -> list[RbdInventoryEntry]:
    rows = payload.get("images") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        logger.warning("query_rbd_inventory: unexpected rbd du response shape")
        return []
    # `rbd du` emits one row per snapshot and then one row for the head image,
    # all sharing the same `name`. Treating a snapshot row as an image
    # multiplies every total (a 10 GiB image with two snapshots reported 30 GiB
    # provisioned) and makes query_rbd_image_usage's `next(... name == image)`
    # return the FIRST row — a snapshot's used_size, not the image's. This is
    # the same guard dashboard/routes/block_storage.py::_image_rows already
    # applies to `rbd ls --long`; the two normalizers must not disagree.
    snapshot_counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("snapshot") is None:
            continue
        snapshot_name = row.get("name") or row.get("image")
        if snapshot_name:
            key = str(snapshot_name)
            snapshot_counts[key] = snapshot_counts.get(key, 0) + 1
    result: list[RbdInventoryEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("snapshot") is not None:
            continue
        name = row.get("name") or row.get("image")
        if not name:
            continue
        snapshots = row.get("snapshots")
        provisioned_size = _as_int(row.get("provisioned_size") or row.get("size"))
        used_size = _as_int(row.get("used_size"))
        # Prefer whatever the payload states explicitly; only fall back to the
        # snapshot rows we just filtered out, which is the sole source `rbd du`
        # actually gives us for this count.
        if isinstance(snapshots, list):
            snapshot_count = len(snapshots)
        elif row.get("snapshot_count") is not None:
            snapshot_count = _as_int(row.get("snapshot_count"))
        else:
            snapshot_count = snapshot_counts.get(str(name), 0)
        result.append(
            RbdInventoryEntry(
                name=str(name),
                image_id=str(row.get("id")) if row.get("id") is not None else None,
                provisioned_size=provisioned_size,
                used_size=used_size,
                used_percent=round((used_size * 100.0 / provisioned_size), 2) if provisioned_size else 0.0,
                snapshot_count=snapshot_count,
            )
        )
    return result


def _normalize_rbd_ls_metadata(payload: dict | list) -> dict[str, dict]:
    """Index image metadata from ``rbd ls --long`` without snapshot rows."""
    rows = payload.get("images") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return {}
    metadata: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("snapshot") is not None:
            continue
        name = row.get("image") or row.get("name")
        if not name:
            continue
        features = row.get("features") or []
        if isinstance(features, str):
            features = [item.strip() for item in features.split(",") if item.strip()]
        metadata[str(name)] = {
            "image_id": str(row.get("id")) if row.get("id") is not None else None,
            "format": row.get("format"),
            "features": features if isinstance(features, list) else [],
        }
    return metadata


def _merge_rbd_inventory_metadata(
    rows: list[RbdInventoryEntry], metadata: dict[str, dict],
) -> list[RbdInventoryEntry]:
    """Add optional format/features while preserving the stable base schema."""
    for row in rows:
        extra = metadata.get(str(row["name"]))
        if not extra:
            continue
        if row.get("image_id") is None and extra.get("image_id"):
            row["image_id"] = extra["image_id"]
        row["format"] = extra.get("format")
        row["features"] = extra.get("features", [])
    return rows


def _enrich_rbd_inventory(
    pool: str,
    rows: list[RbdInventoryEntry],
    query_json,
) -> list[RbdInventoryEntry]:
    """Best-effort metadata enrichment; usage data remains authoritative."""
    if not rows:
        return rows
    try:
        _host, payload = query_json(
            # run_ceph_json_command[_with] appends the single JSON format
            # flag. Keeping it out of the inner command is important for
            # Ceph versions that reject duplicate --format options.
            f"rbd ls --long --pool {shlex.quote(pool)}"
        )
        return _merge_rbd_inventory_metadata(rows, _normalize_rbd_ls_metadata(payload))
    except CephQueryError as exc:
        # ``rbd du`` is still useful when older Ceph versions or permissions do
        # not expose long listing metadata. Detail API reports the partial error.
        logger.warning("RBD inventory metadata enrichment failed for pool %s: %s", pool, exc)
        return rows


def _attachment_from_status(payload: dict | list | None) -> dict:
    """Normalize one ``rbd status`` payload into a small safe summary."""
    if not isinstance(payload, dict):
        return {"attachment_state": "unknown", "watcher_count": None}
    watchers = payload.get("watchers")
    if not isinstance(watchers, list):
        return {"attachment_state": "unknown", "watcher_count": None}
    return {
        "attachment_state": "attached" if watchers else "idle",
        "watcher_count": len(watchers),
    }


def _enrich_rbd_attachment(
    pool: str,
    rows: list[RbdInventoryEntry],
    query_batch,
) -> list[RbdInventoryEntry]:
    """Read attachment state with bounded parallel read-only commands."""
    if not rows:
        return rows
    commands = [
        f"rbd status {shlex.quote(pool)}/{shlex.quote(str(row['name']))} --format json"
        for row in rows
    ]
    try:
        payloads = query_batch(commands)
    except CephQueryError as exc:
        logger.warning("RBD attachment enrichment failed for pool %s: %s", pool, exc)
        return rows
    for row, payload in zip(rows, payloads):
        row.update(_attachment_from_status(payload))
    return rows


def query_rbd_inventory(pool: str) -> list[RbdInventoryEntry]:
    """Return every live image plus best-effort format/features metadata."""
    _, payload = run_ceph_json_command(f"rbd du --pool {shlex.quote(pool)}")
    rows = _enrich_rbd_inventory(pool, _normalize_rbd_inventory(payload), run_ceph_json_command)
    nodes = get_mon_nodes()
    return _enrich_rbd_attachment(
        pool,
        rows,
        lambda commands: run_ceph_json_batch_command_with(
            nodes, settings.ceph_container_name, settings.ssh_user,
            settings.ssh_key_path, settings.ceph_exec_mode, commands, parallel=True,
        )[1],
    )


def query_rbd_inventory_with(
    pool: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> list[RbdInventoryEntry]:
    connection = (mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode)
    _, payload = run_ceph_json_command_with(
        *connection,
        f"rbd du --pool {shlex.quote(pool)}",
    )
    rows = _enrich_rbd_inventory(
        pool,
        _normalize_rbd_inventory(payload),
        lambda command: run_ceph_json_command_with(*connection, command),
    )
    return _enrich_rbd_attachment(
        pool,
        rows,
        lambda commands: run_ceph_json_batch_command_with(*connection, commands, parallel=True)[1],
    )


def query_rbd_image_usage(pool: str, image: str) -> RbdInventoryEntry | None:
    """Return the latest provisioned/used byte counts for one live image."""
    _, payload = run_ceph_json_command(
        f"rbd du {shlex.quote(pool)}/{shlex.quote(image)} --format json"
    )
    return next((row for row in _normalize_rbd_inventory(payload) if row["name"] == image), None)


def query_rbd_image_usage_with(
    pool: str, image: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> RbdInventoryEntry | None:
    """Cluster-scoped counterpart to :func:`query_rbd_image_usage`."""
    _, payload = run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
        f"rbd du {shlex.quote(pool)}/{shlex.quote(image)} --format json",
    )
    return next((row for row in _normalize_rbd_inventory(payload) if row["name"] == image), None)


def _normalize_rbd_image_detail(
    pool: str, image: str, info: dict | list, snapshots: dict | list,
    status: dict | list, children: dict | list, errors: dict | None = None,
    locks: dict | list | None = None,
) -> dict:
    info_row = info if isinstance(info, dict) else {}
    snapshot_rows = snapshots if isinstance(snapshots, list) else snapshots.get("snapshots", []) if isinstance(snapshots, dict) else []
    watcher_rows = status.get("watchers", []) if isinstance(status, dict) else []
    if isinstance(locks, dict) and isinstance(locks.get("lockers"), list):
        lock_rows = locks["lockers"]
    elif isinstance(locks, list):
        lock_rows = locks
    elif isinstance(locks, dict):
        lock_rows = []
        for locker_id, value in locks.items():
            row = dict(value) if isinstance(value, dict) else {"value": value}
            row.setdefault("locker_id", locker_id)
            lock_rows.append(row)
    else:
        lock_rows = []
    child_rows = children if isinstance(children, list) else children.get("children", []) if isinstance(children, dict) else []
    features = info_row.get("features") or []
    if isinstance(features, str):
        features = [item.strip() for item in features.split(",") if item.strip()]
    parent = info_row.get("parent")
    return {
        "pool": pool,
        "name": image,
        "image_id": info_row.get("id"),
        "size": _as_int(info_row.get("size")),
        "object_size": _as_int(info_row.get("object_size")),
        "object_count": _as_int(info_row.get("num_objs")),
        "format": info_row.get("format"),
        "features": features if isinstance(features, list) else [],
        "flags": info_row.get("flags") or [],
        "created_at": info_row.get("create_timestamp") or info_row.get("create_time"),
        "parent": parent,
        "snapshots": snapshot_rows if isinstance(snapshot_rows, list) else [],
        "watchers": watcher_rows if isinstance(watcher_rows, list) else [],
        "locks": lock_rows,
        "attachment_summary": {
            "attached": bool(watcher_rows or lock_rows),
            "watcher_count": len(watcher_rows) if isinstance(watcher_rows, list) else 0,
            "lock_count": len(lock_rows),
            "management_source": "unknown",
            "mutation_supported": False,
            "blocked_reason": "Chưa xác định Cinder/CSI source of truth.",
        },
        "children": child_rows if isinstance(child_rows, list) else [],
        "partial_errors": errors or {},
    }


def query_rbd_image_detail(pool: str, image: str) -> dict:
    spec = f"{shlex.quote(pool)}/{shlex.quote(image)}"
    info = run_ceph_json_command(f"rbd info {spec}")[1]
    errors: dict[str, str] = {}

    def optional(section: str, command: str) -> dict | list:
        try:
            return run_ceph_json_command(command)[1]
        except CephQueryError as exc:
            errors[section] = str(exc)
            return []

    locks = optional("locks", f"rbd lock list {spec}")
    return _normalize_rbd_image_detail(
        pool, image, info,
        optional("snapshots", f"rbd snap ls {spec}"),
        optional("watchers", f"rbd status {spec}"),
        optional("children", f"rbd children {spec}"),
        errors, locks,
    )


def query_rbd_image_detail_with(
    pool: str, image: str, mon_nodes: list[str], container_name: str,
    ssh_user: str, ssh_key_path: str, exec_mode: str,
) -> dict:
    spec = f"{shlex.quote(pool)}/{shlex.quote(image)}"
    connection = (mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode)
    info = run_ceph_json_command_with(*connection, f"rbd info {spec}")[1]
    errors: dict[str, str] = {}

    def optional(section: str, command: str) -> dict | list:
        try:
            return run_ceph_json_command_with(*connection, command)[1]
        except CephQueryError as exc:
            errors[section] = str(exc)
            return []

    locks = optional("locks", f"rbd lock list {spec}")
    return _normalize_rbd_image_detail(
        pool, image, info,
        optional("snapshots", f"rbd snap ls {spec}"),
        optional("watchers", f"rbd status {spec}"),
        optional("children", f"rbd children {spec}"),
        errors, locks,
    )


def _normalize_rbd_child_refs(payload: dict | list, default_pool: str) -> list[dict[str, str]]:
    """Normalize the version-dependent output of ``rbd children``.

    Ceph releases have returned both a list of strings and a mapping containing
    ``children``.  Keep the graph contract stable and never trust an arbitrary
    slash-delimited value as more than ``pool/image``.
    """
    values = payload.get("children", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if isinstance(value, dict):
            child_pool = value.get("pool") or value.get("namespace") or default_pool
            child_image = value.get("image") or value.get("name") or value.get("child")
            token = f"{child_pool}/{child_image}" if child_image else ""
        else:
            token = str(value or "").strip()
        parts = token.split("/", 1)
        child_pool = (parts[0] or default_pool).strip()
        child_image = (parts[1] if len(parts) == 2 else parts[0]).strip()
        if not child_pool or not child_image or len(child_pool) > 128 or len(child_image) > 128:
            continue
        key = (child_pool, child_image)
        if key in seen:
            continue
        seen.add(key)
        result.append({"pool": child_pool, "image": child_image})
    return result


def _rbd_dependency_graph(
    pool: str,
    image: str,
    run_command,
    max_depth: int = 3,
    max_nodes: int = 64,
) -> dict:
    """Build a bounded read-only parent-to-child graph from live RBD metadata."""
    max_depth = max(0, min(int(max_depth), 5))
    max_nodes = max(1, min(int(max_nodes), 128))
    root = {"pool": pool, "image": image}
    queue: list[tuple[str, str, int]] = [(pool, image, 0)]
    visited: set[tuple[str, str]] = set()
    nodes: list[dict] = []
    edges: list[dict] = []
    errors: list[dict] = []
    truncated = False

    while queue:
        current_pool, current_image, depth = queue.pop(0)
        current_key = (current_pool, current_image)
        if current_key in visited:
            continue
        visited.add(current_key)
        nodes.append({"pool": current_pool, "image": current_image, "depth": depth})
        if depth >= max_depth:
            if depth == max_depth:
                truncated = truncated or bool(queue)
            continue
        try:
            payload = run_command(
                f"rbd children {shlex.quote(current_pool)}/{shlex.quote(current_image)}"
            )[1]
            children = _normalize_rbd_child_refs(payload, current_pool)
        except CephQueryError as exc:
            errors.append({
                "pool": current_pool,
                "image": current_image,
                "message": str(exc),
            })
            continue
        for child in children:
            child_key = (child["pool"], child["image"])
            edges.append({
                "parent": {"pool": current_pool, "image": current_image},
                "child": child,
            })
            if child_key in visited or any(
                item[0] == child_key[0] and item[1] == child_key[1] for item in queue
            ):
                continue
            if len(nodes) + len(queue) >= max_nodes:
                truncated = True
                continue
            queue.append((child_key[0], child_key[1], depth + 1))

    return {
        "root": root,
        "nodes": nodes,
        "edges": edges,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "truncated": truncated,
        "partial_errors": errors,
    }


def query_rbd_dependency_graph(
    pool: str, image: str, max_depth: int = 3, max_nodes: int = 64
) -> dict:
    return _rbd_dependency_graph(
        pool, image, run_ceph_json_command, max_depth=max_depth, max_nodes=max_nodes
    )


def query_rbd_dependency_graph_with(
    pool: str, image: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str, max_depth: int = 3, max_nodes: int = 64,
) -> dict:
    connection = (mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode)
    return _rbd_dependency_graph(
        pool,
        image,
        lambda command: run_ceph_json_command_with(*connection, command),
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


_RBD_QOS_OPTION_NAMES = (
    "rbd_qos_iops_limit", "rbd_qos_bps_limit", "rbd_qos_iops_burst", "rbd_qos_bps_burst",
    "rbd_qos_read_iops_limit", "rbd_qos_read_bps_limit",
    "rbd_qos_write_iops_limit", "rbd_qos_write_bps_limit",
)


def _normalize_rbd_qos(payload: dict | list) -> dict[str, int]:
    rows = payload.get("options") if isinstance(payload, dict) else payload
    values: dict[str, int] = {}
    if isinstance(rows, dict):
        pairs = rows.items()
    elif isinstance(rows, list):
        pairs = []
        for row in rows:
            if isinstance(row, dict):
                key = row.get("name") or row.get("key") or row.get("option")
                if key:
                    pairs.append((key, row.get("value")))
    else:
        pairs = []
    for key, raw in pairs:
        if key not in _RBD_QOS_OPTION_NAMES:
            continue
        try:
            values[key] = int(raw)
        except (TypeError, ValueError):
            continue
    return {key: values.get(key, 0) for key in _RBD_QOS_OPTION_NAMES}


def query_rbd_qos(pool: str, image: str) -> dict[str, int]:
    spec = f"{shlex.quote(pool)}/{shlex.quote(image)}"
    _, payload = run_ceph_json_command(f"rbd config image list {spec}")
    return _normalize_rbd_qos(payload)


def query_rbd_qos_with(
    pool: str, image: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> dict[str, int]:
    spec = f"{shlex.quote(pool)}/{shlex.quote(image)}"
    _, payload = run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
        f"rbd config image list {spec}",
    )
    return _normalize_rbd_qos(payload)


_GLOBAL_CAPACITY_HEALTH_CHECKS = {
    "OSD_NEARFULL", "OSD_BACKFILLFULL", "OSD_FULL", "POOL_NEAR_FULL", "POOL_FULL",
}


def _normalize_pool_health(pool: str, health: dict | list | None) -> tuple[str, bool, list[dict]]:
    if not isinstance(health, dict):
        return "unknown", False, []
    checks = health.get("checks")
    if not isinstance(checks, dict):
        return ("ok" if health.get("status") == "HEALTH_OK" else "unknown"), False, []
    pool_token = pool.casefold()
    matched: list[dict] = []
    near_full = False
    for code, value in checks.items():
        if not isinstance(value, dict):
            continue
        serialized = json.dumps(value, ensure_ascii=False).casefold()
        is_capacity_global = code in _GLOBAL_CAPACITY_HEALTH_CHECKS
        if pool_token not in serialized and not is_capacity_global:
            continue
        severity = str(value.get("severity") or "HEALTH_WARN")
        summary = value.get("summary")
        if isinstance(summary, dict):
            summary = summary.get("message")
        matched.append({"code": code, "severity": severity, "summary": str(summary or code)})
        near_full = near_full or is_capacity_global
    if any(item["severity"] == "HEALTH_ERR" for item in matched):
        status = "error"
    elif matched:
        status = "warning"
    else:
        status = "ok"
    return status, near_full, matched


def _normalize_rbd_pool_overview(
    pool: str, pool_detail: dict | list, df: dict | list, health: dict | list | None = None,
) -> dict:
    detail_rows = pool_detail if isinstance(pool_detail, list) else pool_detail.get("pools", []) if isinstance(pool_detail, dict) else []
    detail = next(
        (row for row in detail_rows if isinstance(row, dict) and (row.get("pool_name") or row.get("name")) == pool),
        {},
    )
    df_rows = df.get("pools", []) if isinstance(df, dict) else []
    usage = next(
        (row.get("stats", row) for row in df_rows if isinstance(row, dict) and (row.get("name") or row.get("pool_name")) == pool),
        {},
    )
    applications = detail.get("application_metadata") if isinstance(detail, dict) else {}
    pool_type = detail.get("type") if isinstance(detail, dict) else None
    if pool_type is None and isinstance(detail, dict):
        pool_type = "erasure" if detail.get("erasure_code_profile") else "replicated"
    health_status, near_full, health_checks = _normalize_pool_health(pool, health)
    return {
        "pool": pool,
        "pool_id": detail.get("pool") if isinstance(detail, dict) else None,
        "type": pool_type,
        "replica_size": _as_int(detail.get("size")) if isinstance(detail, dict) else 0,
        "min_size": _as_int(detail.get("min_size")) if isinstance(detail, dict) else 0,
        "pg_num": _as_int(detail.get("pg_num")) if isinstance(detail, dict) else 0,
        "pgp_num": _as_int(detail.get("pg_placement_num") or detail.get("pgp_num")) if isinstance(detail, dict) else 0,
        "crush_rule": detail.get("crush_rule") if isinstance(detail, dict) else None,
        "erasure_code_profile": detail.get("erasure_code_profile") if isinstance(detail, dict) else None,
        "rbd_enabled": isinstance(applications, dict) and "rbd" in applications,
        "application_metadata": applications if isinstance(applications, dict) else {},
        "quota_max_bytes": _as_int(detail.get("quota_max_bytes")) if isinstance(detail, dict) and detail.get("quota_max_bytes") is not None else None,
        "quota_max_objects": _as_int(detail.get("quota_max_objects")) if isinstance(detail, dict) and detail.get("quota_max_objects") is not None else None,
        "pg_autoscale_mode": detail.get("pg_autoscale_mode") if isinstance(detail, dict) else None,
        "target_size_ratio": _as_float(detail.get("target_size_ratio")) if isinstance(detail, dict) and detail.get("target_size_ratio") is not None else None,
        "target_size_bytes": _as_int(detail.get("target_size_bytes")) if isinstance(detail, dict) and detail.get("target_size_bytes") is not None else None,
        "bytes_used": _as_int(usage.get("bytes_used")) if isinstance(usage, dict) else 0,
        "max_available": _as_int(usage.get("max_avail")) if isinstance(usage, dict) else 0,
        "percent_used": _as_float(usage.get("percent_used")) if isinstance(usage, dict) else 0.0,
        "objects": _as_int(usage.get("objects")) if isinstance(usage, dict) else 0,
        "health": health_status,
        "near_full": near_full,
        "health_checks": health_checks,
    }


def query_rbd_pool_overview(pool: str) -> dict:
    detail = run_ceph_json_command("ceph osd pool ls detail")[1]
    usage = run_ceph_json_command("ceph df detail")[1]
    health = run_ceph_json_command("ceph health detail")[1]
    return _normalize_rbd_pool_overview(pool, detail, usage, health)


def query_rbd_pool_overview_with(
    pool: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> dict:
    connection = (mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode)
    detail = run_ceph_json_command_with(*connection, "ceph osd pool ls detail")[1]
    usage = run_ceph_json_command_with(*connection, "ceph df detail")[1]
    health = run_ceph_json_command_with(*connection, "ceph health detail")[1]
    return _normalize_rbd_pool_overview(pool, detail, usage, health)


def query_erasure_code_profile(profile: str) -> dict:
    """Read one EC profile's k/m parameters without changing Ceph state."""
    if not profile or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", profile):
        raise CephQueryError("invalid erasure-code profile name")
    _host, payload = run_ceph_json_command(
        f"ceph osd erasure-code-profile get {shlex.quote(profile)}"
    )
    return payload if isinstance(payload, dict) else {"raw": payload}


def query_erasure_code_profile_with(
    profile: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> dict:
    """Cluster-scoped variant of :func:`query_erasure_code_profile`."""
    if not profile or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", profile):
        raise CephQueryError("invalid erasure-code profile name")
    _host, payload = run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
        f"ceph osd erasure-code-profile get {shlex.quote(profile)}",
    )
    return payload if isinstance(payload, dict) else {"raw": payload}


def query_rbd_pool_dependency_health(pool: str) -> dict:
    """Read health, PG membership and OSD topology for one RBD pool."""
    if not pool or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", pool):
        raise CephQueryError("invalid RBD pool name")
    _host, payloads = run_ceph_json_batch_command_with(
        get_mon_nodes(), settings.ceph_container_name, settings.ssh_user,
        settings.ssh_key_path, settings.ceph_exec_mode,
        [
            "ceph health detail --format json",
            f"ceph pg ls-by-pool {shlex.quote(pool)} --format json",
            "ceph osd tree --format json",
        ],
    )
    if any(payload is None for payload in payloads):
        raise CephQueryError("one or more pool dependency queries failed")
    return {"health": payloads[0], "pg": payloads[1], "osd_tree": payloads[2]}


def query_rbd_pool_dependency_health_with(
    pool: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> dict:
    """Cluster-scoped variant of :func:`query_rbd_pool_dependency_health`."""
    if not pool or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", pool):
        raise CephQueryError("invalid RBD pool name")
    _host, payloads = run_ceph_json_batch_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
        [
            "ceph health detail --format json",
            f"ceph pg ls-by-pool {shlex.quote(pool)} --format json",
            "ceph osd tree --format json",
        ],
    )
    if any(payload is None for payload in payloads):
        raise CephQueryError("one or more pool dependency queries failed")
    return {"health": payloads[0], "pg": payloads[1], "osd_tree": payloads[2]}


def query_crush_rules() -> dict | list:
    """Read the current CRUSH rule inventory without changing placement."""
    return run_ceph_json_command("ceph osd crush rule dump")[1]


def query_crush_rules_with(
    mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> dict | list:
    """Cluster-scoped variant of :func:`query_crush_rules`."""
    return run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
        "ceph osd crush rule dump",
    )[1]


def query_rbd_mirror_pool_info(pool: str) -> dict:
    """Read-only RBD mirroring capability/configuration for one pool."""
    _host, payload = run_ceph_json_command(f"rbd mirror pool info {shlex.quote(pool)}")
    return payload if isinstance(payload, dict) else {"raw": payload}


def query_rbd_mirror_pool_info_with(
    pool: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> dict:
    _host, payload = run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
        f"rbd mirror pool info {shlex.quote(pool)}",
    )
    return payload if isinstance(payload, dict) else {"raw": payload}


def query_rbd_mirror_pool_status(pool: str) -> dict:
    """Read-only mirror status; disabled pools are handled by the route."""
    _host, payload = run_ceph_json_command(f"rbd mirror pool status {shlex.quote(pool)}")
    return payload if isinstance(payload, dict) else {"raw": payload}


def query_rbd_mirror_pool_status_with(
    pool: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
) -> dict:
    _host, payload = run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
        f"rbd mirror pool status {shlex.quote(pool)}",
    )
    return payload if isinstance(payload, dict) else {"raw": payload}


def query_rbd_iostat(pool: str) -> list[VolumeIoSample]:
    """Runs `rbd perf image iostat <pool> --format json` against a MON node
    (same multi-MON-fallback/exec-mode-wrapping `run_ceph_json_command`
    every other live Ceph query in this codebase already goes through —
    `rbd` accepts the same `--format json`/exec-mode-wrapping shape `ceph`
    does). Requires the mgr `rbd_support` module to be enabled on the
    cluster (`ceph mgr module enable rbd_support`) — NOT verified live.

    Returns one entry per RBD image that had recent I/O activity in this
    pool — an image with zero I/O simply doesn't appear (this is `rbd perf
    image iostat`'s own behavior, not something this function filters).
    Raises CephQueryError (from run_ceph_json_command) if every MON node
    failed; returns an empty list (not an error) if the response parses but
    doesn't look like the expected shape, so an unexpected-schema surprise
    degrades to "no data this poll" rather than crashing the Watcher loop.
    """
    try:
        _, payload = run_ceph_json_command(
            _rbd_iostat_command(pool, settings.ceph_exec_mode, settings.ceph_keyring_path),
            append_json_format=False,
            cephadm_mount_path=settings.ceph_keyring_path,
        )
    except CephQueryError as exc:
        # Reef prints this when rbd_support has no fresh client statistics.
        # The bounded CLI then exits 124; that is an empty activity sample,
        # not proof that the MON hosts are unhealthy.
        if "waiting for initial image stats" in str(exc).lower():
            return []
        raise
    return _normalize_rbd_iostat(pool, payload)


def query_rbd_iostat_with(
    pool: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str,
    ceph_keyring_path: str = "/etc/ceph/ceph.client.admin.keyring",
) -> list[VolumeIoSample]:
    """Cluster-scoped counterpart to :func:`query_rbd_iostat`."""
    try:
        _, payload = run_ceph_json_command_with(
            mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
            _rbd_iostat_command(pool, exec_mode, ceph_keyring_path), append_json_format=False,
            cephadm_mount_path=ceph_keyring_path,
        )
    except CephQueryError as exc:
        if "waiting for initial image stats" in str(exc).lower():
            return []
        raise
    return _normalize_rbd_iostat(pool, payload)


def _normalize_rbd_iostat(pool: str, payload: dict | list) -> list[VolumeIoSample]:
    raw_entries = payload if isinstance(payload, list) else payload.get("images") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        logger.warning(
            "query_rbd_iostat: unexpected response shape for pool %r (rbd_support module "
            "enabled? verified schema?) — treating as no data this poll",
            pool,
        )
        return []

    samples: list[VolumeIoSample] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        image = entry.get("image") or entry.get("name")
        if not image:
            continue
        read_ops = _as_float(entry.get("read_ops") or entry.get("read_iops"))
        write_ops = _as_float(entry.get("write_ops") or entry.get("write_iops"))
        read_latency_ms = _rbd_latency_ms(entry, "read_latency_ms", "read_latency")
        write_latency_ms = _rbd_latency_ms(entry, "write_latency_ms", "write_latency")
        samples.append(
            VolumeIoSample(
                pool=pool,
                image=image,
                iops=read_ops + write_ops,
                read_latency_ms=read_latency_ms,
                write_latency_ms=write_latency_ms,
            )
        )
    return samples


def _rbd_latency_ms(entry: dict, milliseconds_key: str, nanoseconds_key: str) -> float:
    """Normalize RBD latency while preserving an explicit millisecond key."""
    if milliseconds_key in entry and entry[milliseconds_key] is not None:
        return _as_float(entry[milliseconds_key])
    return _as_float(entry.get(nanoseconds_key)) / 1_000_000


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class TrashEntry(TypedDict):
    id: str
    name: str
    deletion_time: str
    status: str
    size_bytes: int | None
    used_size_bytes: int | None


def query_rbd_trash(pool: str, *, include_capacity: bool = True) -> list[TrashEntry]:
    """Runs `rbd trash ls <pool> --format json` — lists RBD images an
    operator already soft-deleted (`rbd trash mv`) in this pool, which Ceph
    keeps recoverable (`rbd trash restore`) until explicitly purged
    (`rbd trash rm` — see worker/executor/commands.py's
    `_rbd_trash_remove_command`, the only thing that actually deletes the
    underlying data). Same multi-MON-fallback/exec-mode-wrapping
    `run_ceph_json_command` every other live Ceph query in this codebase
    goes through. Returns an empty list (not an error) on an
    unexpected-shape response, same defensive posture as
    query_rbd_iostat."""
    _, payload = run_ceph_json_command(f"rbd trash ls --long {shlex.quote(pool)}")
    return _normalize_rbd_trash(
        pool,
        payload,
        lambda command: run_ceph_json_command(command)[1],
        lambda command: run_ceph_text_command(command)[1],
        lambda commands: run_ceph_json_batch_command_with(
            get_mon_nodes(), settings.ceph_container_name, settings.ssh_user,
            settings.ssh_key_path, settings.ceph_exec_mode, commands, parallel=True,
        )[1],
        include_capacity=include_capacity,
    )


def query_rbd_trash_with(
    pool: str, mon_nodes: list[str], container_name: str, ssh_user: str,
    ssh_key_path: str, exec_mode: str, *, include_capacity: bool = True,
) -> list[TrashEntry]:
    """Cluster-scoped counterpart to :func:`query_rbd_trash`."""
    connection = (mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode)
    _, payload = run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
        f"rbd trash ls --long {shlex.quote(pool)}",
    )
    return _normalize_rbd_trash(
        pool,
        payload,
        lambda command: run_ceph_json_command_with(*connection, command)[1],
        lambda command: run_ceph_text_command_with(*connection, command)[1],
        lambda commands: run_ceph_json_batch_command_with(
            *connection, commands, parallel=True
        )[1],
        include_capacity=include_capacity,
    )


# One `rados ls` can hold this many object names in memory before the scan is
# not worth its cost; above it the UI keeps showing "—" rather than stalling a
# page render on a multi-million-object pool.
_TRASH_USAGE_MAX_POOL_OBJECTS = 500_000


def _trash_used_sizes(
    pool: str,
    images: list[tuple[str, str, int]],
    query_json: Callable[[str], dict | list],
    query_text: Callable[[str], str],
) -> dict[str, int]:
    """Allocated bytes per Trash ID, measured from the pool's RADOS objects.

    No ``rbd`` subcommand except ``info`` accepts ``--image-id``, and a trashed
    image is gone from the pool directory, so neither ``rbd du <pool>/<name>``
    nor a pool-wide ``rbd du`` can see it (verified against Ceph 20.2, which
    answers ``rbd: unrecognised option '--image-id'`` for both ``du`` and
    ``diff``). What does work is counting the image's own
    ``<block_name_prefix>.*`` objects: ``rbd du`` without ``--exact`` also
    reports object-granular usage, so ``object_count * object_size``
    reproduces its figure exactly — measured at 0.0% deviation against
    ``rbd du`` on live images of the same pool.

    Costs one ``rados ls`` for the whole pool regardless of how many entries
    are in Trash, never one per entry.
    """
    if not images:
        return {}
    try:
        stats = query_json("rados df")
    except CephQueryError as exc:
        logger.warning("_trash_used_sizes: cannot size pool %r before scanning: %s", pool, exc)
        return {}
    pools = stats.get("pools") if isinstance(stats, dict) else None
    entry = next(
        (row for row in pools if isinstance(row, dict) and row.get("name") == pool),
        None,
    ) if isinstance(pools, list) else None
    object_count = _as_int(entry.get("num_objects")) if isinstance(entry, dict) else None
    if object_count is None or object_count > _TRASH_USAGE_MAX_POOL_OBJECTS:
        logger.info(
            "_trash_used_sizes: skipping pool %r (%s objects, cap %s)",
            pool, object_count, _TRASH_USAGE_MAX_POOL_OBJECTS,
        )
        return {}
    try:
        listing = query_text(f"rados -p {shlex.quote(pool)} ls")
    except CephQueryError as exc:
        logger.warning("_trash_used_sizes: rados ls failed for pool %r: %s", pool, exc)
        return {}
    names = listing.split()
    used: dict[str, int] = {}
    for trash_id, block_name_prefix, object_size in images:
        if not block_name_prefix or object_size <= 0:
            continue
        prefix = f"{block_name_prefix}."
        allocated = sum(1 for name in names if name.startswith(prefix))
        used[trash_id] = allocated * object_size
    return used


def _normalize_rbd_trash(
    pool: str,
    payload: dict | list,
    query_json: Callable[[str], dict | list],
    query_text: Callable[[str], str] | None = None,
    query_batch: Callable[[list[str]], list[dict | list | None]] | None = None,
    *,
    include_capacity: bool = True,
) -> list[TrashEntry]:
    if not isinstance(payload, list):
        logger.warning(
            "query_rbd_trash: unexpected response shape for pool %r — treating as no data",
            pool,
        )
        return []

    entries: list[TrashEntry] = []
    measurable: list[tuple[str, str, int]] = []
    # One `rbd info` per entry means one SSH round trip per entry, and under
    # `cephadm shell` each costs ~10s — 78s measured for six entries. The same
    # reads batched into a single remote shell cost one round trip total.
    batched_infos: dict[str, dict] | None = None
    if include_capacity and query_batch is not None:
        listed_ids = [
            str(entry["id"]) for entry in payload
            if isinstance(entry, dict) and entry.get("id")
        ]
        if listed_ids:
            frames = query_batch([
                # The batch runner sends commands verbatim; unlike
                # run_ceph_json_command it does not append the format flag.
                f"rbd info --pool {shlex.quote(pool)} --image-id {shlex.quote(trash_id)} --format json"
                for trash_id in listed_ids
            ])
            batched_infos = {
                trash_id: frame
                for trash_id, frame in zip(listed_ids, frames)
                if isinstance(frame, dict)
            }
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        trash_id = entry.get("id")
        if not trash_id:
            continue
        # Ceph's `rbd trash ls --long --format json` does not include image
        # capacity. Open the trashed image by id and use the logical size
        # reported by `rbd info`. Do not fake allocated usage by multiplying
        # object count by object_size: RBD is thin-provisioned and the final
        # object can be partial, while `rados ls` does not support the object
        # prefix filter this code previously assumed.
        if not include_capacity:
            entries.append(
                TrashEntry(
                    id=str(trash_id),
                    name=str(entry.get("name") or "?"),
                    deletion_time=str(entry.get("deleted_at") or ""),
                    status=str(entry.get("status") or ""),
                    size_bytes=None,
                    used_size_bytes=None,
                )
            )
            continue
        if batched_infos is not None:
            info = batched_infos.get(str(trash_id))
            if info is None:
                # A frame with no JSON is the same restore/purge race the
                # per-entry path tolerates below: the ID vanished between the
                # listing and the metadata read.
                logger.info(
                    "query_rbd_trash: entry %s/%s unreadable during batch scan; skipping",
                    pool, trash_id,
                )
                continue
            entries_info = info
        else:
            entries_info = None
        try:
            info = entries_info if entries_info is not None else query_json(
                f"rbd info --pool {shlex.quote(pool)} --image-id {shlex.quote(str(trash_id))}"
            )
        except CephQueryError as exc:
            # Trash is mutable while it is being restored or purged. A
            # listing can therefore contain an ID that disappears before
            # its metadata is read. Do not discard the whole pool scan for
            # that normal race; the next scan will see the authoritative list.
            error_text = str(exc).lower()
            if "no such file" in error_text or "not found" in error_text or "does not exist" in error_text:
                logger.info(
                    "query_rbd_trash: entry %s/%s disappeared during scan; skipping",
                    pool, trash_id,
                )
                continue
            raise
        if (
            not isinstance(info, dict)
            or "size" not in info
        ):
            raise CephQueryError(
                f"rbd info returned incomplete capacity metadata for trash image {pool}/{trash_id}"
            )
        try:
            provisioned_size = max(0, int(_as_float(info["size"])))
        except (TypeError, ValueError) as exc:
            raise CephQueryError(f"invalid logical size for trash image {pool}/{trash_id}") from exc
        # `rbd info --image-id` is the one command that reaches a trashed
        # image, and it hands over exactly what the RADOS object scan below
        # needs to turn allocated objects into bytes.
        measurable.append((
            str(trash_id),
            str(info.get("block_name_prefix") or ""),
            _as_int(info.get("object_size")) or 0,
        ))
        entries.append(
            TrashEntry(
                id=str(trash_id),
                name=str(entry.get("name") or "?"),
                deletion_time=str(entry.get("deleted_at") or ""),
                status=str(entry.get("status") or ""),
                size_bytes=provisioned_size,
                # Filled in below from the pool-wide object listing; stays
                # None when that scan is unavailable or too expensive, and the
                # UI must then show “—” rather than a false number.
                used_size_bytes=None,
            )
        )
    if query_text is not None:
        used_by_id = _trash_used_sizes(pool, measurable, query_json, query_text)
        for item in entries:
            used = used_by_id.get(item["id"])
            if used is not None:
                item["used_size_bytes"] = used
    return entries


class TrashPurgeResult(TypedDict):
    id: str
    name: str
    error: str | None


# `rbd trash rm --force` on a real image has to delete every RADOS object
# backing it — genuinely slow for a large image, unlike every other command
# in this module (status/health/pool-list queries). Not the 30-minute
# ceiling worker/executor/ssh_executor.py's package-install commands get
# (this codebase's longest precedent), but well past every other timeout
# here — a judgment call, not a measured value (no real large-image trash
# purge was timed this session).
RBD_TRASH_PURGE_TIMEOUT_SECONDS = 600


def _query_rbd_trash_for_purge(pool: str) -> list[TrashEntry]:
    """Validate purge targets without the per-entry capacity N+1 scan."""
    try:
        return query_rbd_trash(pool, include_capacity=False)
    except TypeError as exc:
        if "include_capacity" not in str(exc):
            raise
        return query_rbd_trash(pool)


def force_purge_rbd_trash(pool: str) -> list[TrashPurgeResult]:
    """Force-removes EVERY entry currently in `pool`'s RBD trash — one
    `rbd trash rm <pool>/<id> --force` per id returned by query_rbd_trash
    above, run against a single MON node (same "management command, no
    multi-host fan-out" posture dashboard/chat_client.py documents for
    other mutating commands — this only ever needs to run once).

    Deliberately `--force`, unlike the single-item "Xoá" button's Command
    (worker/executor/commands.py::_rbd_trash_remove_command): that one
    stays bare specifically so an image some running VM still has mapped
    refuses to remove instead of being silently forced out from under it.
    This function exists ONLY for the Volumes page's "Xoá tất cả trash"
    button — an explicit, deliberate operator request to skip both that
    per-image safeguard AND the propose/approve workflow every other
    RISKY action in this codebase requires (worker/policy/action_policy.yaml
    even calls rbd_trash_remove out as "always requires explicit approval,
    no exceptions" — this button is that one exception, by explicit request).

    Continues past a single item's failure (a stubborn still-in-use image,
    a mid-loop SSH hiccup) rather than aborting the whole batch — mirrors
    the operator's own shell loop, which has no `set -e` either. Returns
    one result per attempted id so a partial failure is visible per-image,
    not just as one opaque "batch failed" outcome.

    Raises CephQueryError (propagated from query_rbd_trash/get_mon_nodes)
    if the trash listing itself can't be fetched at all — nothing was
    attempted in that case, so there's nothing per-item to report.
    """
    entries = _query_rbd_trash_for_purge(pool)
    mon_nodes = get_mon_nodes()
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    host = mon_nodes[0]

    results: list[TrashPurgeResult] = []
    for entry in entries:
        trash_id = entry["id"]
        inner_command = f"rbd trash rm {shlex.quote(pool)}/{shlex.quote(trash_id)} --force"
        command = build_exec_command(settings.ceph_exec_mode, settings.ceph_container_name, inner_command)
        error: str | None = None
        try:
            _run_remote_command(host, command, RBD_TRASH_PURGE_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning(
                "force_purge_rbd_trash: failed to remove %s/%s: %s", pool, trash_id, exc
            )
            error = str(exc)
        results.append(TrashPurgeResult(id=trash_id, name=entry["name"], error=error))
    return results


def force_purge_rbd_trash_item(pool: str, trash_id: str) -> TrashPurgeResult:
    """Force-remove one named entry from RBD trash, ignoring retention rules."""
    entries = _query_rbd_trash_for_purge(pool)
    entry = next((row for row in entries if str(row.get("id")) == str(trash_id)), None)
    if entry is None:
        raise CephQueryError(f"Trash ID không còn tồn tại trong pool: {pool}/{trash_id}")
    mon_nodes = get_mon_nodes()
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    inner_command = f"rbd trash rm {shlex.quote(pool)}/{shlex.quote(str(trash_id))} --force"
    command = build_exec_command(settings.ceph_exec_mode, settings.ceph_container_name, inner_command)
    error: str | None = None
    try:
        _run_remote_command(mon_nodes[0], command, RBD_TRASH_PURGE_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("force_purge_rbd_trash_item: failed to remove %s/%s: %s", pool, trash_id, exc)
        error = str(exc)
    return TrashPurgeResult(id=str(trash_id), name=str(entry.get("name") or "?"), error=error)


def ssh_key_path_error(ssh_key_path: str) -> str | None:
    """Story 5.1: checked by the Dashboard's cluster-connection form BEFORE
    attempting an SSH connection, so a bad path fails with a clear message
    instead of a confusing paramiko error. Returns None if the path is
    usable, else a human-readable reason."""
    if not os.path.exists(ssh_key_path):
        return f"SSH key path không tồn tại trên server: {ssh_key_path}"
    if not os.path.isfile(ssh_key_path):
        return f"SSH key path không phải là file hợp lệ (có thể là thư mục): {ssh_key_path}"
    if not os.access(ssh_key_path, os.R_OK):
        return f"SSH key tồn tại nhưng không đọc được (kiểm tra quyền file): {ssh_key_path}"
    return None


def validate_ceph_keyring_with(
    mon_nodes: list[str],
    container_name: str,
    ssh_user: str,
    ssh_key_path: str,
    exec_mode: str,
    ceph_keyring_path: str,
) -> None:
    """Require the configured Ceph keyring to be readable on every MON.

    Only the path is stored.  For docker/podman it is checked inside the
    configured MON container; package installs and cephadm use the host path
    (cephadm commands mount that path into their ephemeral shell container).
    """
    path = ceph_keyring_path.strip()
    if not path or not path.startswith("/") or "\x00" in path or "\n" in path or "\r" in path:
        raise CephQueryError("Ceph keyring path phải là đường dẫn tuyệt đối hợp lệ")
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured for this cluster")
    inner = f"test -r {shlex.quote(path)}"
    command = build_exec_command(exec_mode, container_name, inner) if exec_mode in ("docker", "podman") else inner
    errors: list[str] = []
    for host in mon_nodes:
        try:
            _run_remote_command_with(host, command, ssh_user, ssh_key_path)
        except Exception as exc:
            errors.append(f"{host}: {str(exc) or type(exc).__name__}")
    if errors:
        raise CephQueryError(f"Keyring không đọc được trên MON: {'; '.join(errors)}")


class HostKeyProvisionError(ValueError):
    """A supplied SSH host key is malformed or cannot be stored safely."""


_HOSTNAME_RE = re.compile(r"[A-Za-z0-9_.:-]+")


@contextmanager
def _host_keys_lock():
    """Serialize read-modify-write updates to the persistent host-key file."""
    directory = os.path.dirname(KNOWN_HOSTS_PATH) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    lock_path = f"{KNOWN_HOSTS_PATH}.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_host_keys_atomically(host_keys: paramiko.HostKeys) -> None:
    directory = os.path.dirname(KNOWN_HOSTS_PATH) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".known_hosts.", dir=directory)
    try:
        os.close(fd)
        host_keys.save(temporary_path)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, KNOWN_HOSTS_PATH)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


def provision_host_key(host: str, public_key: str) -> str:
    """Atomically pin an operator-verified OpenSSH host public key.

    The key must be obtained and verified out of band. This function never
    opens an SSH connection or performs trust-on-first-use.
    """
    if not _HOSTNAME_RE.fullmatch(host):
        raise HostKeyProvisionError("IP/hostname không hợp lệ")
    parts = public_key.strip().split()
    if len(parts) < 2 or not re.fullmatch(r"(?:ssh|ecdsa)-[A-Za-z0-9@._+-]+", parts[0]):
        raise HostKeyProvisionError("SSH host public key không hợp lệ")
    try:
        entry = HostKeyEntry.from_line(f"{host} {parts[0]} {parts[1]}")
    except (TypeError, ValueError, InvalidHostKey) as exc:
        raise HostKeyProvisionError("SSH host public key không hợp lệ") from exc
    if entry is None or entry.key is None:
        raise HostKeyProvisionError("Loại SSH host key không được hỗ trợ")

    with _host_keys_lock():
        host_keys = paramiko.HostKeys()
        if os.path.exists(KNOWN_HOSTS_PATH):
            try:
                host_keys.load(KNOWN_HOSTS_PATH)
            except (OSError, paramiko.SSHException) as exc:
                raise HostKeyProvisionError("Không đọc được kho SSH host key hiện tại") from exc
        host_keys.pop(host, None)
        host_keys.add(host, entry.key.get_name(), entry.key)
        try:
            _write_host_keys_atomically(host_keys)
        except OSError as exc:
            raise HostKeyProvisionError("Không thể lưu SSH host key") from exc
    return entry.key.get_name()


def forget_host_key(host: str) -> bool:
    """Removes `host`'s pinned SSH host key from KNOWN_HOSTS_PATH.

    After a node rebuild, the operator must provision its newly verified key
    before the next connection; this function never silently trusts a
    replacement key.  It remains a surgical recovery mechanism rather than
    a blanket `known_hosts` reset.

    Returns True if a stored entry was found and removed, False if the host
    had no entry (nothing to do -- not an error, e.g. it was never connected
    to, or was already cleared)."""
    with _host_keys_lock():
        if not os.path.exists(KNOWN_HOSTS_PATH):
            return False
        host_keys = paramiko.HostKeys()
        host_keys.load(KNOWN_HOSTS_PATH)
        removed = host_keys.pop(host, None) is not None
        if removed:
            _write_host_keys_atomically(host_keys)
    return removed


def list_host_keys() -> list[dict[str, str]]:
    """Return pinned Ceph node keys without exposing their key material."""
    if not os.path.exists(KNOWN_HOSTS_PATH):
        return []
    try:
        host_keys = paramiko.HostKeys()
        host_keys.load(KNOWN_HOSTS_PATH)
    except (OSError, paramiko.SSHException) as exc:
        raise HostKeyProvisionError("Không đọc được kho SSH host key Ceph") from exc
    result = []
    for host, keys in sorted(host_keys.items()):
        for key_type, key in sorted(keys.items()):
            result.append({
                "host": host,
                "key_type": key_type,
                "fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("="),
            })
    return result


def read_public_key(ssh_key_path: str) -> str | None:
    """Reads the paired `<ssh_key_path>.pub` file (the standard ssh-keygen
    convention) so the Dashboard can show the operator exactly what to add
    to `authorized_keys` on a NEW cluster's nodes, instead of requiring them
    to go find/cat the file themselves over a separate shell session.
    Returns None if `ssh_key_path` itself is invalid or no `.pub` file
    exists/is readable alongside it — a display nicety, not a hard error
    (the private key path's own validity is `ssh_key_path_error()`'s job)."""
    if ssh_key_path_error(ssh_key_path) is not None:
        return None
    pub_path = ssh_key_path + ".pub"
    if not os.path.exists(pub_path) or not os.access(pub_path, os.R_OK):
        return None
    try:
        return open(pub_path).read().strip()
    except OSError:
        return None


def _run_remote_command(host: str, command: str, command_timeout: int = COMMAND_TIMEOUT_SECONDS) -> str:
    return _run_remote_command_with(
        host, command, settings.ssh_user, settings.ssh_key_path, command_timeout
    )


def _run_remote_command_with(
    host: str,
    command: str,
    ssh_user: str,
    ssh_key_path: str,
    command_timeout: int = COMMAND_TIMEOUT_SECONDS,
    pool: CephConnectionPool | None = None,
) -> str:
    """Story 5.1: parameterized core so a Dashboard form's not-yet-saved
    values can be tested for real before being written to `.env` —
    `_run_remote_command` above is a thin wrapper over this using current
    `settings`, preserving its existing signature/behavior for
    `run_command_on_node` (watcher/collector.py) unchanged.

    `command_timeout` defaults to the module constant (used by every
    existing caller) but is overridable — watcher/node_metrics.py's
    /proc sampling script sleeps ~1s on the remote end, which the fixed
    5s default leaves too little headroom for over a slow link."""
    active_pool = pool or CephConnectionPool(
        CephSSHConfig(
            user=ssh_user,
            key_path=ssh_key_path,
            known_hosts_path=KNOWN_HOSTS_PATH,
            connect_timeout=settings.ceph_ssh_connect_timeout,
            banner_timeout=settings.ceph_ssh_banner_timeout,
            auth_timeout=settings.ceph_ssh_auth_timeout,
            max_connections=settings.ceph_max_concurrency,
        )
    )
    owns_pool = pool is None
    try:
        # cephadm creates a transient Podman container per call. A bounded
        # host lock prevents concurrent app processes from stampeding one MON
        # without turning brief contention into a false MON failure.
        remote_command = command
        remote_timeout = command_timeout
        if command.lstrip().startswith("cephadm shell"):
            remote_command = (
                "timeout --signal=TERM "
                f"--kill-after={CEPHADM_REMOTE_TIMEOUT_GRACE_SECONDS}s "
                f"{float(command_timeout):g}s "
                f"flock -w {CEPHADM_LOCK_WAIT_SECONDS} "
                f"{shlex.quote(CEPHADM_REMOTE_LOCK_PATH)} {command}"
            )
            # The remote timeout owns the process group, so TERM/KILL reaches
            # flock and its cephadm/Podman descendants. Closing a Paramiko
            # channel alone does not reliably terminate those remote children.
        return CephCommandRunner(active_pool).run(host, remote_command, remote_timeout)
    except CephRunnerError as exc:
        raise CephQueryError(str(exc)) from exc
    finally:
        if owns_pool:
            active_pool.close()


def run_command_on_node(host: str, command: str, timeout: int = COMMAND_TIMEOUT_SECONDS) -> str:
    """Public wrapper around `_run_remote_command` for other watcher modules
    (e.g. `collector.py`, `node_metrics.py`) that need to run an arbitrary
    read-only command on a specific node — reuses the same SSH connection +
    host-key-pinning logic, no duplicated paramiko setup."""
    return _run_remote_command(host, command, timeout)


def run_command_on_node_with(
    host: str, command: str, ssh_user: str, ssh_key_path: str, timeout: int = COMMAND_TIMEOUT_SECONDS
) -> str:
    """Same as `run_command_on_node()` but takes SSH creds explicitly instead
    of reading `settings.ssh_user`/`settings.ssh_key_path` (2026-08-10,
    multi-tenant remediation Phase 1) — `watcher/collector.py`'s log
    collection uses this for a non-default cluster's Incident, mirroring the
    `query_cluster_health_with`/`_run_remote_command_with` split this module
    already established for the health check itself."""
    return _run_remote_command_with(host, command, ssh_user, ssh_key_path, timeout)


def run_diagnostic_command(host: str, command_id: str) -> str:
    """Runs one WHITELISTED read-only command on `host`, wrapped for however
    this cluster is deployed (settings.ceph_exec_mode). `command_id` must be
    a key of DIAGNOSTIC_COMMANDS — raises ValueError otherwise, same
    fail-loud posture as build_exec_command's exec_mode check.

    This is the only place the Dashboard's Nodes-page CLI ever calls to run
    something — there is no path from that feature to an arbitrary shell
    string, by construction.
    """
    if command_id not in DIAGNOSTIC_COMMANDS:
        raise ValueError(f"unknown diagnostic command_id: {command_id!r}")
    inner_command = DIAGNOSTIC_COMMANDS[command_id]
    command = build_exec_command(settings.ceph_exec_mode, settings.ceph_container_name, inner_command)
    command_timeout = (
        CEPHADM_COMMAND_TIMEOUT_SECONDS if settings.ceph_exec_mode == "cephadm" else COMMAND_TIMEOUT_SECONDS
    )
    output = _run_remote_command(host, command, command_timeout)
    return output[:DIAGNOSTIC_OUTPUT_MAX_CHARS]


def run_ceph_json_command(
    inner_command: str, *, append_json_format: bool = True,
    cephadm_mount_path: str | None = None,
) -> tuple[str, dict | list]:
    """Runs `inner_command` (a `ceph ...` subcommand WITHOUT `--format json`
    — this appends it) against each configured MON node in turn until one
    succeeds, parsing the output as JSON. Same multi-MON fallback posture as
    `query_cluster_health_with` (FR1: keep answering even if one MON node is
    temporarily unreachable).

    Powers dashboard/ceph_tools.py's Chat-with-AI query tools — for
    FIXED_TOOL_COMMANDS, `inner_command` is a fixed literal string, same
    AD-5 closed-enum posture as DIAGNOSTIC_COMMANDS/action_id elsewhere in
    this codebase; run_ceph_command_tool is the one deliberate exception
    (an explicitly requested, denylist-gated escape hatch for an arbitrary
    read-only `ceph ...` command — see that function's docstring for why
    this is a real, accepted gap rather than the closed-enum guarantee the
    fixed tools get).

    Returns `(host, parsed)` — `parsed` is the decoded JSON value (a dict for
    most subcommands; `ceph osd pool ls detail --format json` returns a bare
    list when there's more than one pool, verified against a real
    cephadm/reef cluster), or `{"raw_output": <text>}` if some future ceph
    version's `--format json` support for a subcommand stops round-tripping
    as valid JSON. Raises `CephQueryError` if every configured MON node
    failed.
    """
    nodes = get_mon_nodes()
    if not nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    return run_ceph_json_command_with(
        nodes, settings.ceph_container_name, settings.ssh_user, settings.ssh_key_path,
        settings.ceph_exec_mode, inner_command, append_json_format=append_json_format,
        cephadm_mount_path=cephadm_mount_path,
    )


def run_ceph_text_command(inner_command: str) -> tuple[str, str]:
    """Run a read-only Ceph-family command and return its unparsed output."""
    nodes = get_mon_nodes()
    if not nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    return run_ceph_text_command_with(
        nodes, settings.ceph_container_name, settings.ssh_user, settings.ssh_key_path,
        settings.ceph_exec_mode, inner_command,
    )


def run_ceph_text_command_with(
    mon_nodes: list[str],
    container_name: str,
    ssh_user: str,
    ssh_key_path: str,
    exec_mode: str,
    inner_command: str,
) -> tuple[str, str]:
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured for this cluster")
    command = build_exec_command(exec_mode, container_name, inner_command)
    command_timeout = CEPHADM_COMMAND_TIMEOUT_SECONDS if exec_mode == "cephadm" else MCP_COMMAND_TIMEOUT_SECONDS
    query_nodes = _balanced_query_mon_nodes(mon_nodes, command, exec_mode)
    errors = []
    for host in query_nodes:
        try:
            return host, _run_remote_command_with(host, command, ssh_user, ssh_key_path, command_timeout)
        except Exception as exc:
            logger.warning("run_ceph_text_command_with: %s failed: %s", host, exc)
            errors.append(f"{host}: {exc}")
    raise CephQueryError(f"All MON nodes failed: {'; '.join(errors)}")


def run_ceph_json_command_with(
    mon_nodes: list[str],
    container_name: str,
    ssh_user: str,
    ssh_key_path: str,
    exec_mode: str,
    inner_command: str,
    *,
    append_json_format: bool = True,
    cephadm_mount_path: str | None = None,
) -> tuple[str, dict | list]:
    """Same as `run_ceph_json_command()` but takes every connection
    parameter explicitly instead of reading `settings` (2026-08-10,
    multi-tenant remediation Phase 1) — same `_with`-suffix split as
    `query_cluster_health_with()`. `watcher/collector.py`'s
    `_collect_recent_crash_excerpt`/`_collect_device_health_excerpt` use
    this for a non-default cluster's Incident."""
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured for this cluster")
    formatted_command = f"{inner_command} --format json" if append_json_format else inner_command
    if exec_mode == "cephadm" and cephadm_mount_path:
        command = (
            f"cephadm shell --mount {shlex.quote(cephadm_mount_path)}:{CEPHADM_KEYRING_TARGET} -- "
            f"{formatted_command}"
        )
    else:
        command = build_exec_command(exec_mode, container_name, formatted_command)
    command_timeout = CEPHADM_COMMAND_TIMEOUT_SECONDS if exec_mode == "cephadm" else MCP_COMMAND_TIMEOUT_SECONDS
    query_nodes = _balanced_query_mon_nodes(mon_nodes, command, exec_mode)
    errors = []
    for host in query_nodes:
        try:
            output = _run_remote_command_with(host, command, ssh_user, ssh_key_path, command_timeout)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            # rbd_support reports cluster-wide activity, so an idle response
            # from one MON cannot be improved by retrying the same streaming
            # sample on every MON.  Let query_rbd_iostat[_with] normalize it
            # to an empty sample without warning/retry amplification.
            if (
                "rbd" in inner_command
                and "perf image iostat" in inner_command
                and "waiting for initial image stats" in error.lower()
            ):
                raise CephQueryError(error) from exc
            logger.warning("run_ceph_json_command_with: %s failed: %s", host, error)
            errors.append(f"{host}: {error}")
            continue
        try:
            parsed = json.loads(output)
        except (TypeError, ValueError):
            parsed = {"raw_output": output}
        return host, parsed
    raise CephQueryError(f"All MON nodes failed: {'; '.join(errors)}")

JSON_BATCH_MAX_PARALLEL = 8


def _build_json_batch_script(inner_commands: list[str], *, parallel: bool = False) -> str:
    """Build the bounded remote script used by JSON batch queries.

    Parallel mode is safe for independent read-only RBD commands: every
    command writes its own framed output, then the parent shell waits and
    concatenates frames in the original order. This preserves the parser
    contract while avoiding serial Ceph client startup and request latency.
    """
    frames = []
    active_indices = []
    if parallel:
        frames.extend((
            "batch_dir=$(mktemp -d)",
            "trap 'rm -rf \"$batch_dir\"' EXIT",
        ))
    for index, inner_command in enumerate(inner_commands):
        begin = f"__CEPH_AI_BATCH_{index}_BEGIN__"
        status = f"__CEPH_AI_BATCH_{index}_STATUS__"
        end = f"__CEPH_AI_BATCH_{index}_END__"
        if parallel:
            output_path = f'"$batch_dir/{index}"'
            frames.extend((
                "(",
                f"printf '%s\\n' {shlex.quote(begin)} > {output_path}",
                f"{inner_command} 2>/dev/null >> {output_path}",
                "command_status=$?",
                f"printf '\\n' >> {output_path}",
                f"printf '%s:%s\\n' {shlex.quote(status)} $command_status >> {output_path}",
                f"printf '%s\\n' {shlex.quote(end)} >> {output_path}",
                ") &",
                f"batch_pid_{index}=$!",
            ))
            active_indices.append(index)
            if len(active_indices) >= JSON_BATCH_MAX_PARALLEL:
                for active_index in active_indices:
                    frames.append(f"wait \"$batch_pid_{active_index}\"")
                active_indices = []
        else:
            frames.extend((
                f"printf '%s\\n' {shlex.quote(begin)}",
                f"{inner_command} 2>/dev/null",
                "command_status=$?",
                "printf '\\n'",
                f"printf '%s:%s\\n' {shlex.quote(status)} $command_status",
                f"printf '%s\\n' {shlex.quote(end)}",
            ))
    if parallel:
        for active_index in active_indices:
            frames.append(f"wait \"$batch_pid_{active_index}\"")
        for index in range(len(inner_commands)):
            frames.append(f"cat \"$batch_dir/{index}\"")
    return chr(10).join(frames)


def run_ceph_json_batch_command_with(
    mon_nodes: list[str],
    container_name: str,
    ssh_user: str,
    ssh_key_path: str,
    exec_mode: str,
    inner_commands: list[str],
    *,
    parallel: bool = False,
) -> tuple[str, list[dict | list | None]]:
    """Run bounded JSON commands in one remote Ceph shell.

    parallel should only be used for independent read-only commands.
    Results are returned in the same order as inner_commands.
    """
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured for this cluster")
    query_nodes = _balanced_query_mon_nodes(mon_nodes, "\n".join(inner_commands), exec_mode)
    if not inner_commands:
        return query_nodes[0], []
    batch_script = _build_json_batch_script(inner_commands, parallel=parallel)
    batch_inner_command = f"bash -lc {shlex.quote(batch_script)}"
    command = build_exec_command(exec_mode, container_name, batch_inner_command)
    command_timeout = CEPHADM_COMMAND_TIMEOUT_SECONDS if exec_mode == "cephadm" else MCP_COMMAND_TIMEOUT_SECONDS
    errors = []
    for host in query_nodes:
        try:
            output = _run_remote_command_with(host, command, ssh_user, ssh_key_path, command_timeout)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            logger.warning("run_ceph_json_batch_command_with: %s failed: %s", host, error)
            errors.append(f"{host}: {error}")
            continue
        parsed: list[dict | list | None] = [None] * len(inner_commands)
        output_lines = output.splitlines()
        for index in range(len(inner_commands)):
            begin = f"__CEPH_AI_BATCH_{index}_BEGIN__"
            status_prefix = f"__CEPH_AI_BATCH_{index}_STATUS__:"
            end = f"__CEPH_AI_BATCH_{index}_END__"
            try:
                start = output_lines.index(begin)
                finish = output_lines.index(end, start + 1)
                payload_lines = output_lines[start + 1:finish]
                status_line = next(line for line in payload_lines if line.startswith(status_prefix))
                payload_lines.remove(status_line)
                if status_line == f"{status_prefix}0":
                    parsed[index] = json.loads("\n".join(payload_lines))
            except (StopIteration, ValueError, TypeError, json.JSONDecodeError):
                logger.warning("run_ceph_json_batch_command_with: invalid response frame %s from %s", index, host)
        return host, parsed
    raise CephQueryError(f"All MON nodes failed: {'; '.join(errors)}")


def _parse_health_payload(raw_output: str) -> dict:
    payload = json.loads(raw_output)
    if not isinstance(payload, dict) or payload.get("status") not in VALID_STATUSES:
        raise CephQueryError(f"unexpected health payload shape: {raw_output[:200]!r}")
    return payload


def query_cluster_health() -> dict:
    """Query `ceph health detail --format json` via SSH, wrapped for however
    this cluster is deployed (settings.ceph_exec_mode — see
    `build_exec_command`), using the currently configured `settings`.

    Tries each configured MON node in turn until one succeeds — Watcher must
    keep monitoring even if one MON node is temporarily unreachable (FR1).
    """
    nodes = get_mon_nodes()
    if not nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    return query_cluster_health_with(
        nodes,
        settings.ceph_container_name,
        settings.ssh_user,
        settings.ssh_key_path,
        settings.ceph_exec_mode,
    )


def query_cluster_health_with(
    mon_nodes: list[str],
    container_name: str,
    ssh_user: str,
    ssh_key_path: str,
    exec_mode: str = "docker",
    update_sticky_fallback: bool = True,
) -> dict:
    """Story 5.1: same fallback-across-MON-nodes logic as `query_cluster_health()`,
    but takes every connection parameter explicitly instead of reading
    `settings` — lets the Dashboard test a cluster-connection form's
    not-yet-saved values for real before writing them to `.env`.

    `exec_mode` defaults to "docker" so every pre-existing caller (before
    multi-deploy-mode support existed) keeps its exact original behavior.

    `update_sticky_fallback` (multi-cluster observability Phase 1): the
    module-level `last_successful_mon_node` this function updates on
    success is READ by watcher/collector.py as a fallback for the DEFAULT
    cluster's own log collection — there is only ever one such global, not
    one per cluster. watcher/main.py's new observed-cluster loop (any
    cluster other than the default one) calls this same function to poll
    OTHER clusters concurrently, and must pass `update_sticky_fallback=False`
    so a successful poll of cluster B never overwrites the sticky node the
    DEFAULT cluster's log collection depends on. Defaults to True so every
    pre-existing caller (the default cluster's own health poll, and the
    Dashboard's own "test connection before saving" forms for the default
    cluster) keeps its exact original behavior unchanged.

    Probe MONs sequentially with one global deadline. A failed MON falls back
    to the next configured peer, while a normal poll starts only one cephadm
    shell. This avoids background probes continuing after the first result and
    keeps MON CPU bounded during an incident."""
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured")

    command = build_exec_command(exec_mode, container_name, CEPH_HEALTH_INNER_COMMAND)
    command_timeout = HEALTH_COMMAND_TIMEOUT_SECONDS
    deadline = time.monotonic() + settings.ceph_health_timeout
    global last_successful_mon_node
    def retryable_health_error(exc: BaseException) -> bool:
        """Retry transport failures, but never auth or command/data errors."""
        cause = exc.__cause__
        return isinstance(cause, CephRunnerError) and cause.kind in {
            "timeout",
            "unreachable",
            "pool_wait_timeout",
        }

    pool = _get_shared_health_pool(ssh_user, ssh_key_path)
    errors: list[str] = []
    for host in ordered_mon_nodes(mon_nodes):
        try:
            def attempt() -> str:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CephQueryError("health collection deadline exceeded")
                return _run_remote_command_with(
                    host,
                    command,
                    ssh_user,
                    ssh_key_path,
                    min(command_timeout, max(0.01, remaining)),
                    pool=pool,
                )

            output = retry_sync(
                attempt,
                RetryPolicy(
                    max_retries=min(
                        settings.ceph_max_retries,
                        CEPH_HEALTH_MAX_RETRIES_PER_MON,
                    ),
                    base_delay_seconds=settings.ceph_retry_base_delay_seconds,
                    max_delay_seconds=settings.ceph_retry_max_delay_seconds,
                ),
                should_retry=retryable_health_error,
                deadline=deadline,
            )
            payload = _parse_health_payload(output)
        except Exception as exc:
            logger.warning("query_cluster_health_with: %s failed: %s", host, exc)
            errors.append(f"{host}: {exc}")
            continue
        if update_sticky_fallback:
            last_successful_mon_node = host
        return payload

    detail = "; ".join(errors) if errors else "deadline exceeded"
    raise CephQueryError(f"All MON nodes failed: {detail}")


# --- Cluster Upgrade feature (2026-07-23) ----------------------------------
#
# dashboard/routes/upgrade.py-only — read-only version detection + upgrade
# progress, plus two narrow write commands (pause/resume) an operator uses
# to intervene in an upgrade already in flight. Unlike every remediation
# command (worker/executor/), these two run directly from the Dashboard
# process rather than through the Action/approval pipeline: once
# `ceph orch upgrade start` has been issued (see
# worker/executor/commands.py::_upgrade_ceph_cluster_command), cephadm's own
# mgr module drives the rest of the upgrade in the background — this app has
# no per-command check that can abort it once started (there is no
# kill-switch anymore either; see commit a3864dd, 2026-08-11).
# Pause/resume are the operator's actual off-switch for an in-flight
# upgrade; they are read as directly-actionable admin commands (like the
# Nodes page's read-only diagnostics), not as a new Action row.
_VERSION_RE = re.compile(r"ceph version (\d+\.\d+\.\d+)")


def propose_next_version(current_version: str) -> str | None:
    """Best-effort "what's the next release" suggestion shown on the
    Upgrade page — the operator can always type a different target version
    instead. Returns None (no guess) if `current_version`'s major isn't in
    shared/ceph_releases.py's table — already on the newest release it
    knows about, or an unparseable string — rather than fabricating a
    suggestion."""
    return ceph_releases.next_min_version(current_version)


def summarize_cluster_versions() -> dict:
    """Runs `ceph versions` (already whitelisted in DIAGNOSTIC_COMMANDS for
    the Nodes page) and summarizes it for the Upgrade page: per-daemon-type
    version(s), the set of distinct versions cluster-wide, and whether
    they're mixed (e.g. a previous upgrade only partially completed).

    `current_version` is only set when every daemon reports the exact same
    single version — a mixed cluster has no one "current version" to
    propose an upgrade FROM, so the Upgrade page must show the raw
    breakdown instead of a single-version summary in that case.

    Raises CephQueryError if no MON node could be reached (same as
    query_cluster_health/run_ceph_json_command).
    """
    _, payload = run_ceph_json_command("ceph versions")
    return summarize_versions_payload(payload)


def summarize_versions_payload(payload: dict | list) -> dict:
    """Build the Upgrade-page version summary from an already scoped query."""
    per_type: dict[str, list[str]] = {}
    distinct: set[str] = set()
    if isinstance(payload, dict):
        for daemon_type, version_counts in payload.items():
            if daemon_type == "overall" or not isinstance(version_counts, dict):
                continue
            versions_for_type = set()
            for version_string in version_counts:
                match = _VERSION_RE.search(version_string)
                if match:
                    versions_for_type.add(match.group(1))
                    distinct.add(match.group(1))
            per_type[daemon_type] = sorted(versions_for_type)
    return {
        "raw": payload,
        "per_type": per_type,
        "distinct_versions": sorted(distinct),
        "is_mixed": len(distinct) > 1,
        "current_version": next(iter(distinct)) if len(distinct) == 1 else None,
    }


_PROGRESS_FRACTION_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)")


def _upgrade_progress_percent(progress: str | None) -> float | None:
    """Parses cephadm's `progress` string (e.g. `"1/5"`, `"1/5 daemons
    upgraded"`) into a 0-100 percentage for the Upgrade page's progress bar.
    Returns None if `progress` is missing/unparseable (bar is simply omitted
    then — the raw string is always shown regardless, see upgrade.html) or
    if the total is 0 (nothing to divide by, e.g. before cephadm has
    discovered any daemons to upgrade yet)."""
    if not progress:
        return None
    match = _PROGRESS_FRACTION_RE.match(progress)
    if not match:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    if total <= 0:
        return None
    return max(0.0, min(100.0, done * 100.0 / total))


def get_upgrade_status() -> dict:
    """`ceph orch upgrade status` — live progress of an in-flight (or just-
    finished/never-started) upgrade, queried directly from the cluster each
    call, independent of any Action/Incident row in this app's own DB (an
    upgrade cephadm is driving in the background is real regardless of
    whether THIS app's DB still thinks an Action is EXECUTED/pending).

    Requires ceph_exec_mode=cephadm — `ceph orch` commands only work at all
    on a cephadm-managed cluster.
    """
    if settings.ceph_exec_mode != "cephadm":
        raise CephQueryError("ceph orch upgrade status requires ceph_exec_mode=cephadm")
    _, payload = run_ceph_json_command("ceph orch upgrade status")
    if not isinstance(payload, dict):
        return {"raw_output": payload}
    payload["progress_percent"] = _upgrade_progress_percent(payload.get("progress"))
    return payload


def get_upgrade_status_with(
    mon_nodes: list[str], container_name: str, ssh_user: str, ssh_key_path: str, exec_mode: str
) -> dict:
    """Cluster-scoped counterpart of :func:`get_upgrade_status`."""
    if exec_mode != "cephadm":
        raise CephQueryError("ceph orch upgrade status requires ceph_exec_mode=cephadm")
    _, payload = run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode, "ceph orch upgrade status"
    )
    if not isinstance(payload, dict):
        return {"raw_output": payload}
    payload["progress_percent"] = _upgrade_progress_percent(payload.get("progress"))
    return payload


def _run_upgrade_control_command(inner_command: str) -> None:
    if settings.ceph_exec_mode != "cephadm":
        raise CephQueryError(f"{inner_command} requires ceph_exec_mode=cephadm")
    nodes = get_mon_nodes()
    if not nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    command = build_exec_command(settings.ceph_exec_mode, settings.ceph_container_name, inner_command)
    errors = []
    for host in nodes:
        try:
            _run_remote_command(host, command, CEPHADM_COMMAND_TIMEOUT_SECONDS)
            return
        except Exception as exc:
            logger.warning("_run_upgrade_control_command: %s failed: %s", host, exc)
            errors.append(f"{host}: {exc}")
    raise CephQueryError(f"All MON nodes failed: {'; '.join(errors)}")


def _run_upgrade_control_command_with(
    mon_nodes: list[str], container_name: str, ssh_user: str, ssh_key_path: str,
    exec_mode: str, inner_command: str,
) -> None:
    if exec_mode != "cephadm":
        raise CephQueryError(f"{inner_command} requires ceph_exec_mode=cephadm")
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured for this cluster")
    command = build_exec_command(exec_mode, container_name, inner_command)
    errors = []
    for host in mon_nodes:
        try:
            _run_remote_command_with(host, command, ssh_user, ssh_key_path, CEPHADM_COMMAND_TIMEOUT_SECONDS)
            return
        except Exception as exc:
            logger.warning("_run_upgrade_control_command: %s failed: %s", host, exc)
            errors.append(f"{host}: {exc}")
    raise CephQueryError(f"All MON nodes failed: {'; '.join(errors)}")


def pause_upgrade() -> None:
    """Operator's off-switch for an in-flight upgrade (see module note
    own upgrade loop after whichever daemon it's currently mid-upgrading
    finishes; does not roll anything back."""
    _run_upgrade_control_command("ceph orch upgrade pause")


def resume_upgrade() -> None:
    _run_upgrade_control_command("ceph orch upgrade resume")


def pause_upgrade_with(*connection) -> None:
    _run_upgrade_control_command_with(*connection, "ceph orch upgrade pause")


def resume_upgrade_with(*connection) -> None:
    _run_upgrade_control_command_with(*connection, "ceph orch upgrade resume")


# 2026-08-04: worker/executor/commands.py::_upgrade_ceph_cluster_command
# sets these before starting `ceph orch upgrade start` (that command's own
# docstring explains why unsetting can't happen automatically there — the
# upgrade runs asynchronously in cephadm's own mgr module afterward, well
# past when the command that started it already returned). This is the
# manual cleanup step (dashboard/routes/upgrade.py's own "Bỏ noout/
# noscrub..." button) — same self-service-control posture as pause_upgrade/
# reason those aren't (an operator explicitly clicking a specific,
# narrow-scope button, not an automated remediation).
_UPGRADE_OSD_FLAGS = ("noout", "noscrub", "nodeep-scrub", "nosnaptrim")


def unset_upgrade_osd_flags() -> None:
    # bash -c wrapping is REQUIRED here, not optional — build_exec_command's
    # cephadm branch is a bare f"cephadm shell -- {inner_command}" with no
    # shell-separator awareness of its own; a raw `;`-joined inner_command
    # would have every command after the first `;` parsed by the REMOTE
    # host's own top-level shell as a SEPARATE command run OUTSIDE the
    # cephadm container entirely (where `ceph` may not even be on PATH),
    # not chained inside the same `cephadm shell` invocation. Same fix
    # worker/executor/commands.py::_upgrade_ceph_cluster_command already
    # applies for its own multi-command chain.
    unset_commands = "; ".join(f"ceph osd unset {flag}" for flag in _UPGRADE_OSD_FLAGS)
    inner_command = f"bash -c {shlex.quote(unset_commands)}"
    _run_upgrade_control_command(inner_command)


def unset_upgrade_osd_flags_with(*connection) -> None:
    unset_commands = "; ".join(f"ceph osd unset {flag}" for flag in _UPGRADE_OSD_FLAGS)
    _run_upgrade_control_command_with(*connection, f"bash -c {shlex.quote(unset_commands)}")


def list_osds() -> list[dict]:
    """Every OSD in the cluster's CRUSH tree via `ceph osd tree --format
    json` — `{"osd_id": int, "crush_host": str, "status": "up"|"down"}` per
    entry, sorted by osd_id. Powers the BlueStore quick-fix picker
    (dashboard/routes/nodes.py) — an operator still has to separately pick
    which of THIS APP's own configured OSD nodes (SSH-reachable IP) to
    actually run the fix on, since there's no hostname->IP mapping for OSD
    nodes in this app (unlike MON's ceph_mon_hostnames/ceph_mon_nodes pair)
    — same documented gap watcher/collector.py::identify_relevant_nodes
    already has for OSD_/PG_ node routing, not a new one.

    `ceph osd tree`'s own JSON shape (verified against a real parser,
    insights-core's CephOsdTree — NOT independently verified against a real
    cluster this session): `nodes` is a FLAT list; each host/root/rack node's
    `children` field holds INTEGER ID REFERENCES into that same flat list,
    not nested objects — this walks it accordingly rather than assuming a
    nested-object shape.
    """
    _, payload = run_ceph_json_command("ceph osd tree")
    return _normalize_osd_tree(payload)


def _normalize_osd_tree(payload: dict | list) -> list[dict]:
    """Normalize an already-fetched ``ceph osd tree`` response."""
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, list):
        return []

    by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}
    osds = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "host":
            continue
        host_name = node.get("name", "?")
        for child_id in node.get("children") or []:
            child = by_id.get(child_id)
            if not isinstance(child, dict) or child.get("type") != "osd":
                continue
            osds.append(
                {
                    "osd_id": child.get("id"),
                    "crush_host": host_name,
                    "status": child.get("status", "?"),
                }
            )
    osds.sort(key=lambda o: o["osd_id"] if isinstance(o["osd_id"], int) else -1)
    return osds
def _parse_cluster_status_payload(raw_output: str) -> dict:
    """Validate the full ``ceph -s`` JSON used by dashboard status cards."""
    payload = json.loads(raw_output)
    if not isinstance(payload, dict):
        raise CephQueryError(f"unexpected cluster status payload shape: {raw_output[:200]!r}")
    health = payload.get("health")
    if not isinstance(health, dict) or not health.get("status"):
        raise CephQueryError(f"unexpected cluster status payload shape: {raw_output[:200]!r}")
    return payload


def query_cluster_status() -> dict:
    """Return the full read-only ``ceph -s`` payload for dashboard cards."""
    nodes = get_mon_nodes()
    if not nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    return query_cluster_status_with(
        nodes,
        settings.ceph_container_name,
        settings.ssh_user,
        settings.ssh_key_path,
        settings.ceph_exec_mode,
    )


def query_cluster_status_with(
    mon_nodes: list[str],
    container_name: str,
    ssh_user: str,
    ssh_key_path: str,
    exec_mode: str = "docker",
    update_sticky_fallback: bool = True,
) -> dict:
    """Query full cluster counters with the same MON fallback policy as health."""
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured")

    command = build_exec_command(exec_mode, container_name, "ceph -s --format json")
    command_timeout = (
        CEPHADM_COMMAND_TIMEOUT_SECONDS if exec_mode == "cephadm" else COMMAND_TIMEOUT_SECONDS
    )
    global last_successful_mon_node
    errors = []
    for host in ordered_mon_nodes(mon_nodes):
        try:
            output = _run_remote_command_with(host, command, ssh_user, ssh_key_path, command_timeout)
            payload = _parse_cluster_status_payload(output)
            if update_sticky_fallback:
                last_successful_mon_node = host
            return payload
        except Exception as exc:
            logger.warning("query_cluster_status_with: %s failed: %s", host, exc)
            errors.append(f"{host}: {exc}")
    raise CephQueryError(f"All MON nodes failed: {'; '.join(errors)}")
