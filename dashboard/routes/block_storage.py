"""Read-only RBD inventory for the selected Ceph cluster."""

import asyncio
import shlex

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_connection, cluster_selection
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.object_storage_cache import get_or_load
from watcher.ceph_client import CephQueryError, run_ceph_json_command_with


router = APIRouter()
templates = make_templates()
BLOCK_STORAGE_OVERVIEW_LIMIT = 10


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
    _host, pool_payload = run_ceph_json_command_with(*connection, "ceph osd pool ls detail")
    images: list[dict] = []
    for pool in _rbd_pool_names(pool_payload):
        quoted_pool = shlex.quote(pool)
        _host, namespace_payload = run_ceph_json_command_with(
            *connection, f"rbd namespace list --pool {quoted_pool}"
        )
        for namespace in [""] + _namespace_names(namespace_payload):
            namespace_arg = f" --namespace {shlex.quote(namespace)}" if namespace else ""
            _host, image_payload = run_ceph_json_command_with(
                *connection, f"rbd ls --long --pool {quoted_pool}{namespace_arg}"
            )
            images.extend(_image_rows(image_payload, pool, namespace))
    return sorted(images, key=lambda item: (item["pool"], item["namespace"], item["name"]))


def _cached_block_storage(cluster) -> list[dict]:
    return get_or_load(
        "block-storage", f"{cluster.id}:inventory", lambda: _query_block_storage(cluster)
    )


@router.get("/block-storage", response_class=HTMLResponse)
async def block_storage_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    images: list[dict] = []
    total_images = 0
    error = None
    try:
        images = await asyncio.to_thread(_cached_block_storage, cluster)
        total_images = len(images)
        images = images[:BLOCK_STORAGE_OVERVIEW_LIMIT]
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
        "error": error,
    })
