"""Read-only, cluster-scoped RGW S3 user inventory with secret-safe output."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from math import ceil
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection, selected_cluster
from dashboard.routes import auth
from dashboard.routes.object_storage import _safe_error
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared import db
from shared.models import ObjectStorageAuditEntry
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
    create_s3_access_key,
    create_s3_access_key_with,
    revoke_s3_access_key,
    revoke_s3_access_key_with,
    build_s3_user_setting_command,
    execute_s3_user_setting,
    execute_s3_user_setting_with,
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


def _start_audit(cluster_id: str, actor: str, action: str, uid: str, preview: str) -> str:
    with db.SessionLocal() as session:
        row = ObjectStorageAuditEntry(
            cluster_id=cluster_id, actor=actor, action=action, target_type="s3_user",
            target_id=uid, preview=preview, result="pending",
        )
        session.add(row)
        session.commit()
        return row.id


def _finish_audit(audit_id: str, result: str, error: str | None = None) -> None:
    with db.SessionLocal() as session:
        row = session.get(ObjectStorageAuditEntry, audit_id)
        if row is None:
            return
        row.result = result
        row.error_message = error
        row.completed_at = datetime.utcnow()
        session.commit()


def _audit_rows(cluster_id: str, limit: int = 50) -> list[dict]:
    with db.SessionLocal() as session:
        rows = session.query(ObjectStorageAuditEntry).filter_by(cluster_id=cluster_id).order_by(
            ObjectStorageAuditEntry.created_at.desc()
        ).limit(limit).all()
        return [{
            "id": row.id, "actor": row.actor, "action": row.action,
            "target_type": row.target_type, "target_id": row.target_id,
            "preview": row.preview, "result": row.result,
            "error": row.error_message,
            "created_at": row.created_at.isoformat() + "Z",
            "completed_at": row.completed_at.isoformat() + "Z" if row.completed_at else None,
        } for row in rows]


def _valid_access_key(value: object) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 128 or any(ord(char) < 33 for char in key):
        raise HTTPException(status_code=400, detail="Access key không hợp lệ")
    return key


def _key_action(cluster, action: str, uid: str, access_key: str = "") -> dict | None:
    host = _host(cluster)
    if cluster.is_default:
        if action == "create_key":
            return create_s3_access_key(host, uid)
        revoke_s3_access_key(host, uid, access_key)
        return None
    ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
    if action == "create_key":
        return create_s3_access_key_with(
            host, uid, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name
        )
    revoke_s3_access_key_with(
        host, uid, access_key, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name
    )
    return None


def _setting_payload(body: dict) -> tuple[str, str, dict]:
    action = str(body.get("action") or "")
    if action not in {"quota_set", "quota_enable", "quota_disable", "cap_add", "cap_remove"}:
        raise HTTPException(status_code=400, detail="Thao tác quota/capability không hợp lệ")
    uid = _valid_uid(str(body.get("uid") or ""))
    if action.startswith("quota_"):
        params = {"scope": str(body.get("scope") or "")}
        if action == "quota_set":
            try:
                params.update(max_size_bytes=int(body.get("max_size_bytes")), max_objects=int(body.get("max_objects")))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Giới hạn quota không hợp lệ") from exc
    else:
        params = {"cap_type": str(body.get("cap_type") or ""), "cap_perm": str(body.get("cap_perm") or "")}
    try:
        build_s3_user_setting_command(action, uid, params)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return action, uid, params


def _setting_action(cluster, action: str, uid: str, params: dict) -> None:
    host = _host(cluster)
    if cluster.is_default:
        execute_s3_user_setting(host, action, uid, params)
        return
    ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
    execute_s3_user_setting_with(
        host, action, uid, params, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name
    )


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
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc


@router.get("/api/object-storage/users/{uid}")
async def user_api(request: Request, uid: str, user: str = Depends(require_login)):
    del user
    try:
        return await asyncio.to_thread(_detail, selected_cluster(request), uid)
    except RgwLogError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc


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
        "generates_access_key": None,
    }


@router.post("/api/object-storage/users/actions/execute")
async def user_action_execute(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    body = await request.json()
    action, uid, params = _action_payload(body)
    if str(body.get("confirmation") or "") != uid:
        raise HTTPException(status_code=400, detail="Nhập chính xác UID để xác nhận")
    cluster = selected_cluster(request)
    preview = build_s3_user_action_command(action, uid, params)
    try:
        audit_id = await asyncio.to_thread(_start_audit, cluster.id, user, action, uid, preview)
    except Exception as exc:
        logger.exception("cannot persist S3 user audit entry")
        raise HTTPException(status_code=503, detail="Không ghi được audit; thao tác đã bị từ chối") from exc
    try:
        host = await asyncio.to_thread(_execute, cluster, action, uid, params)
    except RgwLogError as exc:
        safe_error = _safe_error(exc)
        await asyncio.to_thread(_finish_audit, audit_id, "failed", safe_error)
        logger.warning("s3_user_action actor=%s cluster=%s action=%s uid=%s result=failed", user, cluster.id, action, uid)
        raise HTTPException(status_code=502, detail=safe_error) from exc
    await asyncio.to_thread(_finish_audit, audit_id, "succeeded")
    logger.info("s3_user_action actor=%s cluster=%s action=%s uid=%s host=%s result=success", user, cluster.id, action, uid, host)
    return {"ok": True, "action": action, "uid": uid, "cluster_id": cluster.id, "request_id": audit_id}


@router.get("/api/object-storage/audit")
async def audit_api(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    cluster = selected_cluster(request)
    return {"entries": await asyncio.to_thread(_audit_rows, cluster.id)}


@router.post("/api/object-storage/users/keys/preview")
async def key_action_preview(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    body = await request.json()
    action = str(body.get("action") or "")
    if action not in {"create_key", "revoke_key"}:
        raise HTTPException(status_code=400, detail="Thao tác access key không hợp lệ")
    uid = _valid_uid(str(body.get("uid") or ""))
    access_key = _valid_access_key(body.get("access_key")) if action == "revoke_key" else ""
    cluster = selected_cluster(request)
    preview = (
        f"Tạo access key mới cho S3 user {uid}; secret chỉ hiển thị một lần"
        if action == "create_key" else f"Revoke access key {access_key} của S3 user {uid}"
    )
    return {
        "action": action, "uid": uid, "cluster_id": cluster.id, "cluster_name": cluster.name,
        "risk": "high" if action == "revoke_key" else "medium", "preview": preview,
        "confirmation_required": access_key if action == "revoke_key" else uid,
    }


@router.post("/api/object-storage/users/keys/execute")
async def key_action_execute(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    body = await request.json()
    action = str(body.get("action") or "")
    if action not in {"create_key", "revoke_key"}:
        raise HTTPException(status_code=400, detail="Thao tác access key không hợp lệ")
    uid = _valid_uid(str(body.get("uid") or ""))
    access_key = _valid_access_key(body.get("access_key")) if action == "revoke_key" else ""
    expected = access_key if action == "revoke_key" else uid
    if str(body.get("confirmation") or "") != expected:
        raise HTTPException(status_code=400, detail="Giá trị xác nhận không chính xác")
    cluster = selected_cluster(request)
    preview = (
        f"create S3 access key for uid={uid} (secret redacted)"
        if action == "create_key" else f"revoke S3 access key={access_key} for uid={uid}"
    )
    try:
        audit_id = await asyncio.to_thread(_start_audit, cluster.id, user, action, uid, preview)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Không ghi được audit; thao tác đã bị từ chối") from exc
    try:
        credential = await asyncio.to_thread(_key_action, cluster, action, uid, access_key)
    except RgwLogError as exc:
        safe_error = _safe_error(exc)
        await asyncio.to_thread(_finish_audit, audit_id, "failed", safe_error)
        raise HTTPException(status_code=502, detail=safe_error) from exc
    await asyncio.to_thread(_finish_audit, audit_id, "succeeded")
    response = {"ok": True, "action": action, "uid": uid, "request_id": audit_id}
    if credential is not None:
        response["credential"] = credential
        response["secret_shown_once"] = True
    return response


@router.post("/api/object-storage/users/settings/preview")
async def setting_preview(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    action, uid, params = _setting_payload(await request.json())
    cluster = selected_cluster(request)
    command = build_s3_user_setting_command(action, uid, params)
    effects = {
        "quota_set": "Đặt giới hạn quota; cần enable scope để bắt đầu enforcement.",
        "quota_enable": "Bật enforcement quota cho scope đã chọn.",
        "quota_disable": "Tắt enforcement quota; giới hạn đã cấu hình vẫn được giữ.",
        "cap_add": "Cấp thêm quyền Admin Ops cho S3 user.",
        "cap_remove": "Thu hồi quyền Admin Ops khỏi S3 user.",
    }
    return {
        "action": action, "uid": uid, "cluster_id": cluster.id, "cluster_name": cluster.name,
        "risk": "high" if action in {"quota_disable", "cap_add", "cap_remove"} else "medium",
        "preview": command, "effect": effects[action], "confirmation_required": uid,
    }


@router.post("/api/object-storage/users/settings/execute")
async def setting_execute(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    body = await request.json()
    action, uid, params = _setting_payload(body)
    if str(body.get("confirmation") or "") != uid:
        raise HTTPException(status_code=400, detail="Nhập chính xác UID để xác nhận")
    cluster = selected_cluster(request)
    preview = build_s3_user_setting_command(action, uid, params)
    try:
        audit_id = await asyncio.to_thread(_start_audit, cluster.id, user, action, uid, preview)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Không ghi được audit; thao tác đã bị từ chối") from exc
    try:
        await asyncio.to_thread(_setting_action, cluster, action, uid, params)
    except RgwLogError as exc:
        safe_error = _safe_error(exc)
        await asyncio.to_thread(_finish_audit, audit_id, "failed", safe_error)
        raise HTTPException(status_code=502, detail=safe_error) from exc
    await asyncio.to_thread(_finish_audit, audit_id, "succeeded")
    return {"ok": True, "action": action, "uid": uid, "request_id": audit_id}


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
        "audit_entries": _audit_rows(cluster.id) if auth.is_admin_user(user) else [],
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


@router.get("/object-storage/user-settings", response_class=HTMLResponse)
async def user_settings_page(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    clusters, cluster = cluster_selection(request)
    return templates.TemplateResponse(request, "object_storage_user_settings.html", {
        "user": user, "is_admin": True, "clusters": clusters, "selected_cluster": cluster,
    })
