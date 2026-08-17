"""Read-only RGW bucket inventory scoped to the selected Ceph cluster."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from math import ceil
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection, selected_cluster
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from dashboard.vntime import to_utc_iso
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from watcher.rgw_access_log import (
    RgwLogError,
    fetch_bucket_list,
    fetch_bucket_list_with,
    fetch_bucket_stats,
    fetch_bucket_stats_with,
    summarize_bucket_stats,
)


router = APIRouter()
templates = make_templates()

PAGE_SIZE = 25
MAX_QUERY_LENGTH = 120


class ObjectStorageError(RuntimeError):
    """A safe, operator-facing failure while querying the configured RGW."""


def _format_bytes(value: object) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "—"


def _rgw_hosts(cluster) -> list[str]:
    nodes = configured_nodes() if cluster.is_default else configured_nodes(cluster)
    return [str(node["host"]) for node in nodes if "RGW" in node["roles"]]


def _list_from_first_reachable_rgw(cluster, hosts: list[str]) -> tuple[str, list[str]]:
    """Use any configured RGW endpoint; bucket metadata is shared per zone."""
    errors = []
    for host in hosts:
        try:
            if cluster.is_default:
                return host, fetch_bucket_list(host)
            ssh_user, ssh_key_path, exec_mode, _mon_container = resolve_ssh_creds(cluster)
            container = cluster.ceph_rgw_container_name
            return host, fetch_bucket_list_with(host, ssh_user, ssh_key_path, exec_mode, container)
        except RgwLogError as exc:
            errors.append(str(exc))
    raise ObjectStorageError("Không thể lấy danh sách bucket từ RGW. " + "; ".join(errors))


def _bucket_summary(cluster, host: str, name: str) -> dict:
    try:
        if cluster.is_default:
            raw = fetch_bucket_stats(host, name)
        else:
            ssh_user, ssh_key_path, exec_mode, _mon_container = resolve_ssh_creds(cluster)
            container = cluster.ceph_rgw_container_name
            raw = fetch_bucket_stats_with(host, name, ssh_user, ssh_key_path, exec_mode, container)
        if raw is None:
            return {"name": name, "stats_available": False, "stats_error": None}
        result = {"name": name, "stats_available": True, "stats_error": None}
        result.update(summarize_bucket_stats(raw))
        created_at = result.get("creation_time")
        result["creation_time"] = to_utc_iso(created_at) if created_at else None
        result["size"] = _format_bytes(result.get("size_bytes"))
        result["quota_size"] = _format_bytes(result.get("quota_max_size_bytes"))
        return result
    except RgwLogError as exc:
        # A bucket may be removed just after the list query, or one stats call
        # may fail while the remainder of the inventory is useful.  Preserve a
        # per-row unavailable state instead of failing the full page.
        return {"name": name, "stats_available": False, "stats_error": str(exc)}


def _inventory(cluster, query: str, page: int) -> dict:
    hosts = _rgw_hosts(cluster)
    if not hosts:
        raise ObjectStorageError("Chưa cấu hình node RGW cho cluster đang chọn.")
    host, names = _list_from_first_reachable_rgw(cluster, hosts)
    normalized_query = query.strip().casefold()
    if normalized_query:
        names = [name for name in names if normalized_query in name.casefold()]
    total = len(names)
    page_count = max(1, ceil(total / PAGE_SIZE))
    page = min(max(page, 1), page_count)
    page_names = names[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(page_names)))) as executor:
        rows = list(executor.map(lambda name: _bucket_summary(cluster, host, name), page_names))
    return {
        "host": host,
        "items": rows,
        "query": query.strip(),
        "page": page,
        "page_size": PAGE_SIZE,
        "page_count": page_count,
        "total": total,
    }


def _detail(cluster, name: str) -> dict:
    bucket_name = name.strip()
    if not bucket_name or len(bucket_name) > 255 or "/" in bucket_name:
        raise HTTPException(status_code=404, detail="Tên bucket không hợp lệ")
    hosts = _rgw_hosts(cluster)
    if not hosts:
        raise ObjectStorageError("Chưa cấu hình node RGW cho cluster đang chọn.")
    host, _names = _list_from_first_reachable_rgw(cluster, hosts)
    result = _bucket_summary(cluster, host, bucket_name)
    if not result["stats_available"]:
        if result["stats_error"]:
            raise ObjectStorageError(result["stats_error"])
        raise HTTPException(status_code=404, detail="Không tìm thấy bucket")
    return {"host": host, **result}


@router.get("/api/object-storage/buckets")
async def bucket_inventory_api(
    request: Request,
    query: str = Query("", max_length=MAX_QUERY_LENGTH),
    page: int = Query(1, ge=1),
    user: str = Depends(require_login),
):
    del user
    try:
        return await asyncio.to_thread(_inventory, selected_cluster(request), query, page)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/api/object-storage/buckets/{bucket}")
async def bucket_detail_api(request: Request, bucket: str, user: str = Depends(require_login)):
    del user
    try:
        return await asyncio.to_thread(_detail, selected_cluster(request), bucket)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/object-storage/buckets", response_class=HTMLResponse)
async def bucket_inventory_page(
    request: Request,
    user: str = Depends(require_login),
    query: str = Query("", max_length=MAX_QUERY_LENGTH),
    page: int = Query(1, ge=1),
):
    clusters, cluster = cluster_selection(request)
    inventory = {"items": [], "query": query.strip(), "page": page, "page_count": 1, "total": 0}
    error = None
    try:
        inventory = await asyncio.to_thread(_inventory, cluster, query, page)
    except ObjectStorageError as exc:
        error = str(exc)
    return templates.TemplateResponse(request, "object_storage_buckets.html", {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "clusters": clusters,
        "selected_cluster": cluster,
        "inventory": inventory,
        "error": error,
        "quote_bucket": lambda value: quote(value, safe=""),
        "quote_query": lambda value: quote(value, safe=""),
    })


@router.get("/object-storage/buckets/{bucket}", response_class=HTMLResponse)
async def bucket_detail_page(request: Request, bucket: str, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    detail = None
    error = None
    try:
        detail = await asyncio.to_thread(_detail, cluster, bucket)
    except ObjectStorageError as exc:
        error = str(exc)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        error = str(exc.detail)
    return templates.TemplateResponse(request, "object_storage_bucket_detail.html", {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "clusters": clusters,
        "selected_cluster": cluster,
        "bucket": bucket,
        "detail": detail,
        "error": error,
    })
