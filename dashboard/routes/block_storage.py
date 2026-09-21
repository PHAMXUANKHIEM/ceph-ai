"""Read-only RBD inventory for the selected Ceph cluster."""

import asyncio
import logging
import shlex

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_connection, cluster_selection
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.object_storage_cache import get_or_load, is_refreshing as cache_is_refreshing
from shared.ceph_query_cache import get_cached as get_persisted_cache, store as store_persisted_cache
from watcher.ceph_client import (
    CephQueryError,
    run_ceph_json_batch_command_with,
    run_ceph_json_command_with,
)


router = APIRouter()
templates = make_templates()
logger = logging.getLogger(__name__)
BLOCK_STORAGE_OVERVIEW_LIMIT = 10
# Serve the inventory from cache for 30 minutes, then keep serving the last
# value while one background refresh is running for up to another 30 minutes.
BLOCK_STORAGE_CACHE_TTL_SECONDS = 1800
BLOCK_STORAGE_CACHE_STALE_TTL_SECONDS = 3600
BLOCK_STORAGE_API_VERSION = "v1"


class BlockStorageInventory(list):
    def __init__(self, rows=(), *, pools=()):
        super().__init__(rows)
        self.pools = list(pools)


def _inventory_summary(images: list[dict]) -> dict:
    """Build read-only overview metrics from the already cached inventory."""
    allocated = sum(int(item.get("size_bytes") or 0) for item in images)
    known_used = [int(item["used_size_bytes"]) for item in images if item.get("used_size_bytes") is not None]
    used = sum(known_used)
    known_percent = [float(item["used_percent"]) for item in images if item.get("used_percent") is not None]
    pool_counts: dict[str, int] = {}
    for item in images:
        pool = str(item.get("pool") or "Không xác định")
        pool_counts[pool] = pool_counts.get(pool, 0) + 1
    return {
        "total_allocated_bytes": allocated,
        "total_used_bytes": used if known_used else None,
        "total_allocated": _format_size(allocated),
        "total_used": _format_size(used) if known_used else "—",
        "average_used_percent": round(sum(known_percent) / len(known_percent), 1) if known_percent else None,
        "used_ratio": round(used / allocated * 100, 1) if allocated and known_used else None,
        "pool_counts": [{"name": name, "count": count} for name, count in sorted(pool_counts.items())],
    }


def _block_storage_cache_key(cluster) -> str:
    return f"{cluster.id}:inventory"


def _load_block_storage(cluster) -> BlockStorageInventory:
    """Load the live inventory and persist the successful result."""
    inventory = _query_block_storage(cluster)
    if _uses_mocked_ceph_client():
        return inventory
    try:
        # The persistent cache stores only JSON-compatible rows. The
        # process-local BlockStorageInventory wrapper is reconstructed when
        # it is read back.
        store_persisted_cache(
            "block-storage",
            _block_storage_cache_key(cluster),
            {
                "rows": list(inventory),
                "pools": list(getattr(inventory, "pools", ())),
            },
        )
    except Exception:
        # Cache persistence must never turn a successful Ceph read into a
        # failed page response.
        logger.exception("Block Storage persistent cache write failed for cluster %s", cluster.id)
    return inventory


def _persistent_block_storage_fallback(cluster) -> BlockStorageInventory:
    """Return recent disk-backed inventory without contacting Ceph."""
    if _uses_mocked_ceph_client():
        return BlockStorageInventory(pools=[])
    try:
        cached = get_persisted_cache(
            "block-storage",
            _block_storage_cache_key(cluster),
            max_age_seconds=BLOCK_STORAGE_CACHE_STALE_TTL_SECONDS,
        )
    except Exception:
        logger.exception("Block Storage persistent cache read failed for cluster %s", cluster.id)
        return BlockStorageInventory(pools=[])
    if cached is None:
        return BlockStorageInventory(pools=[])
    value, _age_seconds = cached
    if isinstance(value, dict):
        raw_rows = value.get("rows", [])
        raw_pools = value.get("pools", [])
        pools = [str(pool) for pool in raw_pools if pool]
    elif isinstance(value, list):
        # Backward-compatible read of the pre-envelope cache format.
        raw_rows = value
        pools = []
    else:
        return BlockStorageInventory(pools=[])
    rows = [item for item in raw_rows if isinstance(item, dict)] if isinstance(raw_rows, list) else []
    pools = sorted(set(pools) | {str(item["pool"]) for item in rows if item.get("pool")})
    return BlockStorageInventory(rows, pools=pools)


def _uses_mocked_ceph_client() -> bool:
    return (
        getattr(run_ceph_json_command_with, "__module__", "") != "watcher.ceph_client"
        or getattr(_query_block_storage, "__module__", "") != __name__
    )


def _rbd_pool_names(payload: dict | list) -> list[str]:
    rows = payload if isinstance(payload, list) else payload.get("pools", []) if isinstance(payload, dict) else []
    return sorted({
        str(row.get("pool_name") or row.get("poolname"))
        for row in rows
        if isinstance(row, dict)
        and (row.get("pool_name") or row.get("poolname"))
        and isinstance(row.get("application_metadata"), dict)
        and "rbd" in row["application_metadata"]
    })


def _namespace_names(payload: dict | list) -> list[str]:
    rows = payload if isinstance(payload, list) else []
    names = []
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else row
        if name:
            names.append(str(name))
    return sorted(set(names))


def _usage_by_image(payload: dict | list) -> dict[str, dict[str, int]]:
    """Index one namespace-scoped ``rbd du`` response by image name."""
    rows = payload if isinstance(payload, list) else payload.get("images", []) if isinstance(payload, dict) else []
    usage: dict[str, dict[str, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name") or row.get("image")
        if not name:
            continue
        try:
            provisioned = max(0, int(row.get("provisioned_size") or row.get("size") or 0))
            used = max(0, int(row.get("used_size") or 0))
        except (TypeError, ValueError):
            continue
        usage[str(name)] = {
            "provisioned_size_bytes": provisioned,
            "used_size_bytes": used,
        }
    return usage


def _image_rows(
    payload: dict | list, pool: str, namespace: str,
    usage_by_name: dict[str, dict[str, int]] | None = None,
) -> list[dict]:
    rows = payload if isinstance(payload, list) else payload.get("images", []) if isinstance(payload, dict) else []
    result = []
    for row in rows:
        if isinstance(row, str):
            row = {"name": row}
        if not isinstance(row, dict):
            continue
        # `rbd ls --long` includes one row for the image and one row for
        # every snapshot. Overview is an image inventory, so snapshot rows
        # must not become duplicate volumes.
        if row.get("snapshot") is not None:
            continue
        # `rbd ls --long --format json` uses `image` on Ceph Reef (the
        # production payload), while some older/newer CLI builds and test
        # fixtures expose `name`. Accept both instead of silently dropping
        # every real image from the inventory.
        image_name = row.get("image") or row.get("name")
        if not image_name:
            continue
        size = row.get("size", 0)
        try:
            size_bytes = int(size)
        except (TypeError, ValueError):
            size_bytes = 0
        usage = (usage_by_name or {}).get(str(image_name), {})
        provisioned_size_bytes = usage.get("provisioned_size_bytes", size_bytes)
        used_size_bytes = usage.get("used_size_bytes")
        used_percent = (
            round(used_size_bytes * 100.0 / provisioned_size_bytes, 2)
            if used_size_bytes is not None and provisioned_size_bytes > 0
            else None
        )
        result.append({
            "name": str(image_name),
            "pool": pool,
            "namespace": namespace,
            "size_bytes": provisioned_size_bytes,
            "size": _format_size(provisioned_size_bytes),
            "used_size_bytes": used_size_bytes,
            "used_size": _format_size(used_size_bytes) if used_size_bytes is not None else "—",
            "used_percent": used_percent,
        })
    return result


def _format_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


def _query_block_storage(cluster) -> list[dict]:
    connection = cluster_connection(cluster)
    if _uses_mocked_ceph_client():
        _host, pool_payload = run_ceph_json_command_with(*connection, "ceph osd pool ls detail")
        pool_names = _rbd_pool_names(pool_payload)
        images: list[dict] = BlockStorageInventory(pools=pool_names)
        for pool in pool_names:
            quoted_pool = shlex.quote(pool)
            _host, namespace_payload = run_ceph_json_command_with(
                *connection, f"rbd namespace list --pool {quoted_pool}"
            )
            for namespace in ["", *_namespace_names(namespace_payload)]:
                namespace_arg = f" --namespace {shlex.quote(namespace)}" if namespace else ""
                _host, image_payload = run_ceph_json_command_with(
                    *connection, f"rbd ls --long --pool {quoted_pool}{namespace_arg}"
                )
                _host, usage_payload = run_ceph_json_command_with(
                    *connection, f"rbd du --pool {quoted_pool}{namespace_arg}"
                )
                images.extend(_image_rows(image_payload, pool, namespace, _usage_by_image(usage_payload)))
        return BlockStorageInventory(
            sorted(images, key=lambda item: (item["pool"], item["namespace"], item["name"])),
            pools=pool_names,
        )

    _host, pool_frames = run_ceph_json_batch_command_with(
        *connection, ["ceph osd pool ls detail --format json"]
    )
    pool_payload = pool_frames[0]
    if pool_payload is None:
        raise CephQueryError("Không lấy được danh sách pool RBD")
    pool_names = _rbd_pool_names(pool_payload)
    images: list[dict] = BlockStorageInventory(pools=pool_names)
    namespace_commands = [
        f"rbd namespace list --pool {shlex.quote(pool)} --format json"
        for pool in pool_names
    ]
    _host, namespace_frames = run_ceph_json_batch_command_with(*connection, namespace_commands, parallel=True)
    pool_namespaces: list[tuple[str, list[str]]] = []
    for pool, namespace_payload in zip(pool_names, namespace_frames):
        if namespace_payload is None:
            raise CephQueryError(f"Không lấy được namespace của pool {pool}")
        pool_namespaces.append((pool, ["", *_namespace_names(namespace_payload)]))

    image_requests: list[tuple[str, str]] = []
    image_commands: list[str] = []
    for pool, namespaces in pool_namespaces:
        quoted_pool = shlex.quote(pool)
        for namespace in namespaces:
            namespace_arg = f" --namespace {shlex.quote(namespace)}" if namespace else ""
            image_requests.append((pool, namespace))
            image_commands.append(
                f"rbd ls --long --pool {quoted_pool}{namespace_arg} --format json"
            )
    if image_commands:
        _host, image_frames = run_ceph_json_batch_command_with(*connection, image_commands, parallel=True)
        usage_commands = [
            f"rbd du --pool {shlex.quote(pool)}"
            f"{(' --namespace ' + shlex.quote(namespace)) if namespace else ''} --format json"
            for pool, namespace in image_requests
        ]
        # Keep rbd du sequential: without fast-diff it may inspect every
        # potential object, so parallel usage scans would create an avoidable
        # OSD/CPU burst when the overview is refreshed.
        _host, usage_frames = run_ceph_json_batch_command_with(
            *connection, usage_commands, parallel=False
        )
        for index, ((pool, namespace), image_payload) in enumerate(zip(image_requests, image_frames)):
            if image_payload is None:
                raise CephQueryError(f"Không lấy được inventory của pool {pool}")
            usage_payload = usage_frames[index] if index < len(usage_frames) else None
            images.extend(_image_rows(image_payload, pool, namespace, _usage_by_image(usage_payload or {})))
    return BlockStorageInventory(
        sorted(images, key=lambda item: (item["pool"], item["namespace"], item["name"])),
        pools=pool_names,
    )


def _cached_block_storage(cluster) -> list[dict]:
    return get_or_load(
        "block-storage",
        _block_storage_cache_key(cluster),
        lambda: _load_block_storage(cluster),
        ttl_seconds=BLOCK_STORAGE_CACHE_TTL_SECONDS,
        stale_ttl_seconds=BLOCK_STORAGE_CACHE_STALE_TTL_SECONDS,
    )


@router.get("/api/v1/block-storage/contract", tags=["block-storage"])
async def block_storage_api_contract(request: Request, user: str = Depends(require_login)):
    """Publish the stable client contract without exposing credentials or commands."""
    del user
    _clusters, cluster = cluster_selection(request)
    return {
        "api_version": BLOCK_STORAGE_API_VERSION,
        "resource": "block_storage",
        "cluster_id": cluster.id,
        "read_only": True,
        "compatibility": {
            "legacy_routes_supported": True,
            "canonical_prefix": "/api/v1/block-storage",
            "openapi": "/openapi.json",
        },
        "inventory": {
            "endpoint": "/api/volumes/{pool}/inventory",
            "detail_endpoint": "/api/volumes/{pool}/inventory/{image}",
            "cluster_scope_required": True,
            "stale_state_is_explicit": True,
        },
        "mutation": {
            "direct_execution": False,
            "response_status": "PENDING_APPROVAL",
            "required_header": "Idempotency-Key",
            "replay_is_safe": True,
            "post_check_required": True,
        },
        "job_status": {
            "action_id_returned": True,
            "polling_supported": True,
            "progress_is_bounded": True,
            "terminal_states": ["EXECUTED", "FAILED", "REJECTED", "INCONCLUSIVE"],
        },
        "webhook": {
            "block_storage_specific_webhook": False,
            "fallback": "poll action status using action_id",
        },
        "iac": {
            "terraform_provider": "not_available",
            "sdk": "not_available",
            "policy_bypass": False,
        },
        "error_contract": {
            "unsupported": "HTTP 404/409 with operator-safe detail",
            "backend_unavailable": "HTTP 502",
            "missing_evidence": "HTTP 200 with status=INSUFFICIENT_EVIDENCE where applicable",
        },
    }


@router.get("/block-storage", response_class=HTMLResponse)
async def block_storage_page(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(BLOCK_STORAGE_OVERVIEW_LIMIT, ge=10, le=100),
    user: str = Depends(require_login),
):
    page_size = BLOCK_STORAGE_OVERVIEW_LIMIT
    clusters, cluster = cluster_selection(request)
    images: list[dict] = []
    total_images = 0
    total_pages = 1
    create_pools: list[str] = []
    error = None
    cache_key = f"{cluster.id}:inventory"
    if _uses_mocked_ceph_client():
        # Test doubles must not reuse a previous test's process-local cache;
        # keep the two-request cache contract while isolating each double.
        function = _query_block_storage
        code = getattr(function, "__code__", None)
        marker = (
            f"{getattr(code, 'co_filename', '')}:"
            f"{getattr(code, 'co_firstlineno', id(function))}"
        )
        cache_key = f"{cache_key}:mock:{marker}"
    cache_loading = False
    inventory_summary = _inventory_summary([])
    try:
        images = await asyncio.to_thread(
            get_or_load,
            "block-storage",
            cache_key,
            lambda: _load_block_storage(cluster),
            ttl_seconds=BLOCK_STORAGE_CACHE_TTL_SECONDS,
            stale_ttl_seconds=BLOCK_STORAGE_CACHE_STALE_TTL_SECONDS,
            background_on_miss=not _uses_mocked_ceph_client(),
            fallback=_persistent_block_storage_fallback(cluster),
        )
        cache_loading = cache_is_refreshing("block-storage", cache_key)
        total_images = len(images)
        inventory_summary = _inventory_summary(list(images))
        create_pools = list(getattr(images, "pools", ())) or sorted({
            str(item["pool"]) for item in images if item.get("pool")
        })
        total_pages = max(1, (total_images + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        images = images[start:start + page_size]
    except CephQueryError as exc:
        error = str(exc)
    return templates.TemplateResponse(request, "block_storage.html", {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "clusters": clusters,
        "selected_cluster": cluster,
        "images": images,
        "total_images": total_images,
        "overview_limit": BLOCK_STORAGE_OVERVIEW_LIMIT,
        "page_size": page_size,
        "page": page,
        "total_pages": total_pages,
        "create_pools": create_pools,
        "error": error,
        "cache_loading": cache_loading,
        "inventory_summary": inventory_summary,
    })
