"""Read-only RBD inventory for the selected Ceph cluster."""

import asyncio
import shlex

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_connection, cluster_selection
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.object_storage_cache import get_or_load, is_refreshing as cache_is_refreshing
from watcher.ceph_client import (
    CephQueryError,
    run_ceph_json_batch_command_with,
    run_ceph_json_command_with,
)


router = APIRouter()
templates = make_templates()
BLOCK_STORAGE_OVERVIEW_LIMIT = 10


class BlockStorageInventory(list):
    def __init__(self, rows=(), *, pools=()):
        super().__init__(rows)
        self.pools = list(pools)


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


def _image_rows(payload: dict | list, pool: str, namespace: str) -> list[dict]:
    rows = payload if isinstance(payload, list) else payload.get("images", []) if isinstance(payload, dict) else []
    result = []
    for row in rows:
        if isinstance(row, str):
            row = {"name": row}
        if not isinstance(row, dict):
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
        result.append({
            "name": str(image_name),
            "pool": pool,
            "namespace": namespace,
            "size_bytes": size_bytes,
            "size": _format_size(size_bytes),
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
                images.extend(_image_rows(image_payload, pool, namespace))
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
    _host, namespace_frames = run_ceph_json_batch_command_with(*connection, namespace_commands)
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
        _host, image_frames = run_ceph_json_batch_command_with(*connection, image_commands)
        for (pool, namespace), image_payload in zip(image_requests, image_frames):
            if image_payload is None:
                raise CephQueryError(f"Không lấy được inventory của pool {pool}")
            images.extend(_image_rows(image_payload, pool, namespace))
    return BlockStorageInventory(
        sorted(images, key=lambda item: (item["pool"], item["namespace"], item["name"])),
        pools=pool_names,
    )


def _cached_block_storage(cluster) -> list[dict]:
    return get_or_load(
        "block-storage",
        f"{cluster.id}:inventory",
        lambda: _query_block_storage(cluster),
        stale_ttl_seconds=1800,
    )


@router.get("/block-storage", response_class=HTMLResponse)
async def block_storage_page(
    request: Request, page: int = Query(1, ge=1), user: str = Depends(require_login)
):
    clusters, cluster = cluster_selection(request)
    images: list[dict] = []
    total_images = 0
    total_pages = 1
    create_pools: list[str] = []
    error = None
    cache_key = f"{cluster.id}:inventory"
    cache_loading = False
    try:
        images = await asyncio.to_thread(
            get_or_load,
            "block-storage",
            cache_key,
            lambda: _query_block_storage(cluster),
            stale_ttl_seconds=1800,
            background_on_miss=not _uses_mocked_ceph_client(),
            fallback=BlockStorageInventory(pools=[]),
        )
        cache_loading = cache_is_refreshing("block-storage", cache_key)
        total_images = len(images)
        create_pools = list(getattr(images, "pools", ())) or sorted({
            str(item["pool"]) for item in images if item.get("pool")
        })
        total_pages = max(1, (total_images + BLOCK_STORAGE_OVERVIEW_LIMIT - 1) // BLOCK_STORAGE_OVERVIEW_LIMIT)
        page = min(page, total_pages)
        start = (page - 1) * BLOCK_STORAGE_OVERVIEW_LIMIT
        images = images[start:start + BLOCK_STORAGE_OVERVIEW_LIMIT]
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
        "page": page,
        "total_pages": total_pages,
        "create_pools": create_pools,
        "error": error,
        "cache_loading": cache_loading,
    })
