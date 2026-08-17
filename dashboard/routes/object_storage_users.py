"""Read-only, cluster-scoped RGW S3 user inventory with secret-safe output."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from math import ceil
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection, selected_cluster
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from watcher.rgw_access_log import (
    RgwLogError,
    fetch_s3_user_info,
    fetch_s3_user_info_with,
    fetch_s3_user_list,
    fetch_s3_user_list_with,
    summarize_s3_user,
    build_s3_user_action_command,
    execute_s3_user_action,
    execute_s3_user_action_with,
)

router = APIRouter()
templates = make_templates()
PAGE_SIZE = 25
MAX_QUERY_LENGTH = 120
logger = logging.getLogger(__name__)
USER_ACTIONS = {"create", "modify", "suspend", "enable"}


def _host(cluster) -> str:
    nodes = configured_nodes() if cluster.is_default else configured_nodes(cluster)
    hosts = [str(node["host"]) for node in nodes if "RGW" in node["roles"]]
    if not hosts:
        raise RgwLogError("Chưa cấu hình node RGW cho cluster đang chọn.")
    return hosts[0]


def _list(cluster, host: str) -> list[str]:
    if cluster.is_default:
        return fetch_s3_user_list(host)
    user, key, mode, _container = resolve_ssh_creds(cluster)
    return fetch_s3_user_list_with(host, user, key, mode, cluster.ceph_rgw_container_name)


def _info(cluster, host: str, uid: str) -> dict | None:
    raw = fetch_s3_user_info(host, uid) if cluster.is_default else None
    if not cluster.is_default:
        user, key, mode, _container = resolve_ssh_creds(cluster)
        raw = fetch_s3_user_info_with(host, uid, user, key, mode, cluster.ceph_rgw_container_name)
    return summarize_s3_user(raw) if raw else None


def _valid_uid(uid: str) -> str:
    value = uid.strip()
    if not value or len(value) > 128 or any(ord(char) < 32 for char in value) or "/" in value:
        raise HTTPException(status_code=404, detail="S3 user không hợp lệ")
    return value


def _require_admin(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được quản lý S3 user")


def _action_payload(body: dict) -> tuple[str, str, dict]:
    action = str(body.get("action") or "")
    if action not in USER_ACTIONS:
        raise HTTPException(status_code=400, detail="Thao tác S3 user không hợp lệ")
    uid = _valid_uid(str(body.get("uid") or ""))
    params = {
        "display_name": str(body.get("display_name") or "").strip(),
        "email": str(body.get("email") or "").strip(),
    }
    if any(len(value) > 254 or any(ord(char) < 32 for char in value) for value in params.values()):
        raise HTTPException(status_code=400, detail="Metadata S3 user không hợp lệ")
    if action == "create" and not params["display_name"]:
        raise HTTPException(status_code=400, detail="Display name là bắt buộc khi tạo user")
    if action == "modify" and not any(params.values()):
        raise HTTPException(status_code=400, detail="Cần ít nhất một trường để cập nhật")
    return action, uid, params


def _execute(cluster, action: str, uid: str, params: dict) -> str:
    host = _host(cluster)
    if cluster.is_default:
        execute_s3_user_action(host, action, uid, params)
    else:
        ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
        execute_s3_user_action_with(
            host, action, uid, params, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name
        )
    return host


def _inventory(cluster, query: str, page: int) -> dict:
    host = _host(cluster)
    users = _list(cluster, host)
    normalized = query.strip().casefold()
    if normalized:
        users = [uid for uid in users if normalized in uid.casefold()]
    total = len(users)
    page_count = max(1, ceil(total / PAGE_SIZE))
    page = min(max(page, 1), page_count)
    page_users = users[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(page_users)))) as executor:
        details = list(executor.map(lambda uid: _info(cluster, host, uid), page_users))
    items = [detail or {"uid": uid, "unavailable": True} for uid, detail in zip(page_users, details)]
    return {"host": host, "items": items, "query": query.strip(), "page": page,
            "page_count": page_count, "total": total}


def _detail(cluster, uid: str) -> dict:
    uid = _valid_uid(uid)
    host = _host(cluster)
    detail = _info(cluster, host, uid)
    if detail is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy S3 user")
    return {"host": host, **detail}


@router.get("/api/object-storage/users")
async def users_api(request: Request, query: str = Query("", max_length=MAX_QUERY_LENGTH),
                    page: int = Query(1, ge=1), user: str = Depends(require_login)):
    del user
    try:
        return await asyncio.to_thread(_inventory, selected_cluster(request), query, page)
    except RgwLogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/api/object-storage/users/{uid}")
async def user_api(request: Request, uid: str, user: str = Depends(require_login)):
    del user
    try:
        return await asyncio.to_thread(_detail, selected_cluster(request), uid)
    except RgwLogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/object-storage/users/actions/preview")
async def user_action_preview(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    action, uid, params = _action_payload(await request.json())
    cluster = selected_cluster(request)
    # Preview is descriptive and intentionally omits SSH/container details.
    inner = build_s3_user_action_command(action, uid, params)
    return {
        "action": action, "uid": uid, "cluster_id": cluster.id,
        "cluster_name": cluster.name, "risk": "medium" if action in {"suspend", "modify"} else "low",
        "confirmation_required": uid,
        "preview": inner,
        "generates_access_key": False,
    }


@router.post("/api/object-storage/users/actions/execute")
async def user_action_execute(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    body = await request.json()
    action, uid, params = _action_payload(body)
    if str(body.get("confirmation") or "") != uid:
        raise HTTPException(status_code=400, detail="Nhập chính xác UID để xác nhận")
    cluster = selected_cluster(request)
    try:
        host = await asyncio.to_thread(_execute, cluster, action, uid, params)
    except RgwLogError as exc:
        logger.warning("s3_user_action actor=%s cluster=%s action=%s uid=%s result=failed", user, cluster.id, action, uid)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("s3_user_action actor=%s cluster=%s action=%s uid=%s host=%s result=success", user, cluster.id, action, uid, host)
    return {"ok": True, "action": action, "uid": uid, "cluster_id": cluster.id}


@router.get("/object-storage/users", response_class=HTMLResponse)
async def users_page(request: Request, user: str = Depends(require_login),
                     query: str = Query("", max_length=MAX_QUERY_LENGTH), page: int = Query(1, ge=1)):
    clusters, cluster = cluster_selection(request)
    inventory = {"items": [], "query": query.strip(), "page": page, "page_count": 1, "total": 0}
    error = None
    try:
        inventory = await asyncio.to_thread(_inventory, cluster, query, page)
    except RgwLogError as exc:
        error = str(exc)
    return templates.TemplateResponse(request, "object_storage_users.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "clusters": clusters,
        "selected_cluster": cluster, "inventory": inventory, "error": error,
        "quote_value": lambda value: quote(value, safe=""),
    })


@router.get("/object-storage/users/{uid}", response_class=HTMLResponse)
async def user_page(request: Request, uid: str, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    detail = None
    error = None
    try:
        detail = await asyncio.to_thread(_detail, cluster, uid)
    except (RgwLogError, HTTPException) as exc:
        error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
    return templates.TemplateResponse(request, "object_storage_user_detail.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "clusters": clusters,
        "selected_cluster": cluster, "uid": uid, "detail": detail, "error": error,
    })
