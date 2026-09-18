"""Read-only, cluster-scoped RGW S3 user inventory with secret-safe output."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime
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
from shared.ceph_query_cache import (
    get_cached as get_persistent_cache,
    invalidate as invalidate_persistent_cache,
    store as store_persistent_cache,
)
from shared import db
from shared.models import ObjectStorageAuditEntry
from shared.object_storage_cache import (
    get_or_load,
    invalidate as invalidate_object_storage_cache,
    state as cache_state,
)
from watcher.rgw_access_log import (
    RgwLogError,
    fetch_s3_user_info,
    fetch_s3_user_info_with,
    fetch_s3_user_info_batch,
    fetch_s3_user_info_batch_with,
    fetch_s3_user_list,
    fetch_s3_user_list_with,
    fetch_s3_user_bucket_list,
    fetch_s3_user_bucket_list_with,
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
PAGE_SIZE = 10
USER_PAGE_SIZES = {10, 25, 50, 100}
AUDIT_MAX_ROWS = 500
MAX_QUERY_LENGTH = 120
USER_LIST_TTL_SECONDS = 60
USER_LIST_STALE_TTL_SECONDS = 300
USER_PAGE_TTL_SECONDS = 60
USER_PAGE_STALE_TTL_SECONDS = 300
USER_SEARCH_TTL_SECONDS = 120
USER_SEARCH_STALE_TTL_SECONDS = 7200
USER_SNAPSHOT_MAX_SECONDS = 120
USER_SNAPSHOT_BATCH_SIZE = 50
USER_PERSISTED_MAX_AGE_SECONDS = 86400
USER_PERSISTED_NAMESPACE = "s3-user-snapshot"
logger = logging.getLogger(__name__)
USER_ACTIONS = {"create", "modify", "suspend", "enable", "delete"}
_DEFAULT_FETCH_S3_USER_LIST = fetch_s3_user_list
_DEFAULT_FETCH_S3_USER_LIST_WITH = fetch_s3_user_list_with
_DEFAULT_FETCH_S3_USER_INFO = fetch_s3_user_info
_DEFAULT_FETCH_S3_USER_INFO_WITH = fetch_s3_user_info_with


def _host(cluster) -> str:
    hosts = _rgw_hosts(cluster)
    if not hosts:
        raise RgwLogError("Chưa cấu hình node RGW cho cluster đang chọn.")
    return hosts[0]


def _rgw_hosts(cluster) -> list[str]:
    nodes = configured_nodes() if cluster.is_default else configured_nodes(cluster)
    return [str(node["host"]) for node in nodes if "RGW" in node["roles"]]


def _list(cluster, host: str) -> list[str]:
    if cluster.is_default:
        return fetch_s3_user_list(host)
    user, key, mode, _container = resolve_ssh_creds(cluster)
    return fetch_s3_user_list_with(host, user, key, mode, cluster.ceph_rgw_container_name)


def _list_from_available_rgw(cluster) -> tuple[str, list[str]]:
    """Read the shared user list from the first reachable RGW node."""
    errors = []
    for host in _rgw_hosts(cluster):
        try:
            return host, _list(cluster, host)
        except Exception as exc:
            errors.append(f"{host}: {type(exc).__name__}")
            logger.warning("s3_user_inventory_rgW_failed host=%s error=%s", host, type(exc).__name__)
    detail = "; ".join(errors) or "không có node RGW"
    raise RgwLogError(f"Không đọc được danh sách S3 user từ các node RGW ({detail})")


def _info(cluster, host: str, uid: str) -> dict | None:
    raw = fetch_s3_user_info(host, uid) if cluster.is_default else None
    if not cluster.is_default:
        user, key, mode, _container = resolve_ssh_creds(cluster)
        raw = fetch_s3_user_info_with(host, uid, user, key, mode, cluster.ceph_rgw_container_name)
    return summarize_s3_user(raw) if raw else None


def _info_batch(cluster, host: str, uids: list[str]) -> dict[str, dict | None]:
    # Keep the single-user path when tests or callers inject the existing
    # helpers. This preserves the established seam while production uses the
    # batched command below.
    if cluster.is_default and fetch_s3_user_info is not _DEFAULT_FETCH_S3_USER_INFO:
        return {uid: _info(cluster, host, uid) for uid in uids}
    if not cluster.is_default and fetch_s3_user_info_with is not _DEFAULT_FETCH_S3_USER_INFO_WITH:
        return {uid: _info(cluster, host, uid) for uid in uids}
    raw_by_uid = (
        fetch_s3_user_info_batch(host, uids)
        if cluster.is_default
        else fetch_s3_user_info_batch_with(
            host, uids, *resolve_ssh_creds(cluster)[:3], cluster.ceph_rgw_container_name
        )
    )
    return {uid: summarize_s3_user(raw_by_uid.get(uid)) if raw_by_uid.get(uid) else None for uid in uids}


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


def _execute(cluster, action: str, uid: str, params: dict) -> tuple[str, dict | None]:
    host = _host(cluster)
    if cluster.is_default:
        credential = execute_s3_user_action(host, action, uid, params)
    else:
        ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
        credential = execute_s3_user_action_with(
            host, action, uid, params, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name
        )
    return host, credential


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
    cluster_id = None
    with db.SessionLocal() as session:
        row = session.get(ObjectStorageAuditEntry, audit_id)
        if row is None:
            return
        cluster_id = row.cluster_id
        row.result = result
        row.error_message = error
        row.completed_at = datetime.utcnow()
        session.commit()
    if result == "succeeded" and cluster_id:
        for namespace in ("s3-user-list", "s3-user-pages", "s3-user-search"):
            invalidate_object_storage_cache(cluster_id, namespace)
        for key in ("list", "page", "index"):
            try:
                invalidate_persistent_cache(USER_PERSISTED_NAMESPACE, f"{cluster_id}:{key}")
            except Exception:
                logger.exception("cannot invalidate persistent S3 user snapshot cluster=%s key=%s", cluster_id, key)


def _audit_rows(cluster_id: str, limit: int = AUDIT_MAX_ROWS) -> list[dict]:
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


def _uses_mocked_inventory_helpers() -> bool:
    """Keep unit-test seams synchronous while production refreshes in background."""
    return (
        fetch_s3_user_list is not _DEFAULT_FETCH_S3_USER_LIST
        or fetch_s3_user_list_with is not _DEFAULT_FETCH_S3_USER_LIST_WITH
        or fetch_s3_user_info is not _DEFAULT_FETCH_S3_USER_INFO
        or fetch_s3_user_info_with is not _DEFAULT_FETCH_S3_USER_INFO_WITH
    )


def _persistent_fallback(key: str, default: dict, *, max_age_seconds: int = USER_PERSISTED_MAX_AGE_SECONDS) -> tuple[dict, dict]:
    """Read a durable S3 snapshot without making a network call."""
    if _uses_mocked_inventory_helpers():
        return default, {"available": False, "age_seconds": None}
    try:
        cached = get_persistent_cache(
            USER_PERSISTED_NAMESPACE,
            key,
            max_age_seconds=max_age_seconds,
        )
    except Exception:
        logger.exception("cannot read persistent S3 user snapshot key=%s", key)
        return default, {"available": False, "age_seconds": None}
    if cached is None or not isinstance(cached[0], dict):
        return default, {"available": False, "age_seconds": None}
    return cached[0], {"available": True, "age_seconds": cached[1]}


def _store_persistent_snapshot(key: str, value: dict) -> None:
    try:
        store_persistent_cache(USER_PERSISTED_NAMESPACE, key, value)
    except Exception:
        # The live inventory remains valid even if the durable cache volume is
        # temporarily unavailable.
        logger.exception("cannot persist S3 user snapshot key=%s", key)


def _load_user_list(cluster) -> dict:
    """Load only user IDs for the fast inventory path."""
    host, uids = _list_from_available_rgw(cluster)
    return {"host": host, "uids": uids}


def _user_page_key(cluster, uids: list[str], host: str | None = None) -> str:
    scope = f"{host or _host(cluster)}\0" + "\0".join(uids)
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
    return f"{cluster.id}:page:{digest}"


def _load_user_page(cluster, uids: list[str], host: str | None = None) -> dict:
    """Load metadata only for the users visible on the current page."""
    host = host or _host(cluster)
    details = _info_batch(cluster, host, uids)
    return {
        "host": host,
        "items": [details.get(uid) or {"uid": uid, "unavailable": True} for uid in uids],
    }


def _load_user_search_index(cluster) -> dict:
    """Build the expensive name/email index only when such a search is used."""
    deadline = time.monotonic() + USER_SNAPSHOT_MAX_SECONDS
    host, users = _list_from_available_rgw(cluster)
    details_by_uid: dict[str, dict | None] = {}
    # Keep each remote command bounded to avoid oversized shell commands.
    for offset in range(0, len(users), USER_SNAPSHOT_BATCH_SIZE):
        if time.monotonic() >= deadline:
            raise RgwLogError("Vượt quá thời gian đồng bộ S3 user ở chế độ nền.")
        details_by_uid.update(_info_batch(
            cluster, host, users[offset:offset + USER_SNAPSHOT_BATCH_SIZE]
        ))
    return {
        "host": host,
        "items": [
            details_by_uid.get(uid) or {"uid": uid, "unavailable": True}
            for uid in users
        ],
    }


def _cached_user_list(cluster) -> dict:
    key = f"{cluster.id}:list"
    fallback, _state = _persistent_fallback(key, {"host": None, "uids": []})

    def load() -> dict:
        value = _load_user_list(cluster)
        _store_persistent_snapshot(key, value)
        return value

    return get_or_load(
        "s3-user-list",
        key,
        load,
        ttl_seconds=USER_LIST_TTL_SECONDS,
        stale_ttl_seconds=USER_LIST_STALE_TTL_SECONDS,
        background_on_miss=not _uses_mocked_inventory_helpers(),
        fallback=fallback,
        replace_cluster=True,
    )


def _cached_user_page(cluster, uids: list[str], host: str | None = None) -> dict:
    key = _user_page_key(cluster, uids, host)
    persisted_key = f"{cluster.id}:page"
    persisted, _state = _persistent_fallback(persisted_key, {})
    fallback = (
        {"host": persisted.get("host"), "items": list(persisted.get("items") or [])}
        if persisted.get("uids") == uids else {"host": None, "items": []}
    )

    def load() -> dict:
        value = _load_user_page(cluster, uids, host)
        _store_persistent_snapshot(persisted_key, {**value, "uids": list(uids)})
        return value

    return get_or_load(
        "s3-user-pages",
        key,
        load,
        ttl_seconds=USER_PAGE_TTL_SECONDS,
        stale_ttl_seconds=USER_PAGE_STALE_TTL_SECONDS,
        background_on_miss=not _uses_mocked_inventory_helpers(),
        fallback=fallback,
        replace_cluster=True,
    )


def _cached_user_search_index(cluster) -> dict:
    key = f"{cluster.id}:index"
    fallback, _state = _persistent_fallback(
        key, {"host": None, "items": []}, max_age_seconds=USER_PERSISTED_MAX_AGE_SECONDS
    )

    def load() -> dict:
        value = _load_user_search_index(cluster)
        _store_persistent_snapshot(key, value)
        return value

    return get_or_load(
        "s3-user-search",
        key,
        load,
        ttl_seconds=USER_SEARCH_TTL_SECONDS,
        stale_ttl_seconds=USER_SEARCH_STALE_TTL_SECONDS,
        background_on_miss=not _uses_mocked_inventory_helpers(),
        fallback=fallback,
        replace_cluster=True,
    )


def _inventory(cluster, query: str, page: int, page_size: int = PAGE_SIZE) -> dict:
    user_list = _cached_user_list(cluster)
    list_key = f"{cluster.id}:list"
    list_state = cache_state("s3-user-list", list_key)
    _list_snapshot, list_persisted_state = _persistent_fallback(
        list_key, {"host": None, "uids": []}
    )
    user_ids = list(user_list.get("uids") or [])
    normalized = query.strip().casefold()
    search_state = None
    page_state = None
    page_persisted_state = {"available": False, "age_seconds": None}
    search_pending = False
    search_persisted_state = {"available": False, "age_seconds": None}
    search_host = None
    indexed_items: list[dict] | None = None
    if normalized:
        uid_matches = [uid for uid in user_ids if normalized in uid.casefold()]
        if uid_matches:
            user_ids = uid_matches
        elif list_state["refreshing"] and not user_ids:
            # Do not start the expensive display-name/email index while the
            # cheap UID list is still cold. The next poll can resolve a UID
            # match without launching two competing RGW scans.
            indexed_items = []
            search_pending = True
        else:
            index = _cached_user_search_index(cluster)
            search_host = index.get("host")
            search_key = f"{cluster.id}:index"
            search_state = cache_state("s3-user-search", search_key)
            _search_snapshot, search_persisted_state = _persistent_fallback(
                search_key, {"host": None, "items": []}
            )
            indexed_items = [
                item for item in (index.get("items") or [])
                if normalized in " ".join(
                    str(item.get(field) or "")
                    for field in ("uid", "display_name", "email")
                ).casefold()
            ]
            search_pending = not bool(index.get("items")) and bool(search_state["refreshing"])
            user_ids = [str(item.get("uid")) for item in indexed_items if item.get("uid")]

    if indexed_items is not None:
        all_items = indexed_items
    else:
        all_items = None
    total = len(user_ids) if all_items is None else len(all_items)
    page_size = page_size if page_size in USER_PAGE_SIZES else PAGE_SIZE
    page_count = max(1, ceil(total / page_size))
    page = min(max(page, 1), page_count)
    page_uids = user_ids[(page - 1) * page_size:page * page_size]
    if all_items is None:
        page_host = user_list.get("host")
        page_snapshot = _cached_user_page(cluster, page_uids, page_host) if page_uids else {"host": None, "items": []}
        page_state = cache_state("s3-user-pages", _user_page_key(cluster, page_uids, page_host)) if page_uids else None
        _page_snapshot, page_persisted_state = (
            _persistent_fallback(f"{cluster.id}:page", {}) if page_uids
            else ({}, {"available": False, "age_seconds": None})
        )
        page_users = list(page_snapshot.get("items") or [])
    else:
        page_users = all_items[(page - 1) * page_size:page * page_size]
    available = [item for item in page_users if not item.get("unavailable")]
    states = [list_state, search_state, page_state]
    active_states = [state for state in states if state is not None]
    return {
        "host": user_list.get("host") or search_host,
        "items": page_users,
        "query": query.strip(),
        "page": page,
        "page_size": page_size,
        "page_count": page_count,
        "total": total,
        "summary_scope": "current_page",
        "active_count": sum(1 for detail in available if not detail["suspended"]),
        "key_count_total": sum(detail["key_count"] for detail in available),
        "refreshing": any(bool(state["refreshing"]) for state in active_states) or search_pending,
        "searching": search_pending,
        "refresh_error": any(bool(state["error"]) for state in active_states),
        "stale": bool(
            any(
                state["available"] and state["age_seconds"] is not None
                and state["age_seconds"] >= ttl
                for state, ttl in (
                    (list_state, USER_LIST_TTL_SECONDS),
                    (search_state, USER_SEARCH_TTL_SECONDS),
                    (page_state, USER_PAGE_TTL_SECONDS),
                    (list_persisted_state, USER_LIST_TTL_SECONDS),
                    (search_persisted_state, USER_SEARCH_TTL_SECONDS),
                    (page_persisted_state, USER_PAGE_TTL_SECONDS),
                )
                if state is not None
            )
        ),
    }


def _cached_inventory(cluster, query: str, page: int, page_size: int = PAGE_SIZE) -> dict:
    return _inventory(cluster, query, page, page_size)


def _detail(cluster, uid: str) -> dict:
    uid = _valid_uid(uid)
    errors = []
    for host in _rgw_hosts(cluster):
        try:
            detail = _info(cluster, host, uid)
            if detail is None:
                continue
            return {"host": host, **detail}
        except Exception as exc:
            errors.append(f"{host}: {type(exc).__name__}")
    if errors:
        raise RgwLogError("Không đọc được metadata S3 user từ các node RGW")
    raise HTTPException(status_code=404, detail="Không tìm thấy S3 user")

def _buckets(cluster, uid: str) -> list[str]:
    uid = _valid_uid(uid)
    for host in _rgw_hosts(cluster):
        try:
            if cluster.is_default:
                return fetch_s3_user_bucket_list(host, uid)
            ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
            return fetch_s3_user_bucket_list_with(
                host, uid, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name
            )
        except Exception:
            continue
    raise RgwLogError("Không đọc được bucket của S3 user từ các node RGW")


@router.get("/api/object-storage/users")
async def users_api(request: Request, query: str = Query("", max_length=MAX_QUERY_LENGTH),
                    page: int = Query(1, ge=1), page_size: int = Query(0, ge=0, le=100),
                    user: str = Depends(require_login)):
    del user
    try:
        effective_page_size = page_size if page_size in USER_PAGE_SIZES else PAGE_SIZE
        return await asyncio.to_thread(
            _cached_inventory, selected_cluster(request), query, page, effective_page_size
        )
    except RgwLogError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc


@router.get("/api/object-storage/users/{uid}")
async def user_api(request: Request, uid: str, user: str = Depends(require_login)):
    del user
    try:
        return await asyncio.to_thread(_detail, selected_cluster(request), uid)
    except RgwLogError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc


@router.get("/api/object-storage/users/{uid}/buckets")
async def user_buckets_api(request: Request, uid: str, user: str = Depends(require_login)):
    del user
    try:
        return {"uid": _valid_uid(uid), "buckets": await asyncio.to_thread(_buckets, selected_cluster(request), uid)}
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
        "cluster_name": cluster.name, "risk": "high" if action == "delete" else ("medium" if action in {"suspend", "modify"} else "low"),
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
        host, credential = await asyncio.to_thread(_execute, cluster, action, uid, params)
    except RgwLogError as exc:
        safe_error = _safe_error(exc)
        await asyncio.to_thread(_finish_audit, audit_id, "failed", safe_error)
        logger.warning("s3_user_action actor=%s cluster=%s action=%s uid=%s result=failed", user, cluster.id, action, uid)
        raise HTTPException(status_code=502, detail=safe_error) from exc
    await asyncio.to_thread(_finish_audit, audit_id, "succeeded")
    logger.info("s3_user_action actor=%s cluster=%s action=%s uid=%s host=%s result=success", user, cluster.id, action, uid, host)
    response = {"ok": True, "action": action, "uid": uid, "cluster_id": cluster.id, "request_id": audit_id}
    if credential is not None:
        response["credential"] = credential
        response["secret_shown_once"] = True
    return response


@router.get("/api/object-storage/audit")
async def audit_api(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    cluster = selected_cluster(request)
    return {"entries": await asyncio.to_thread(_audit_rows, cluster.id)}


@router.post("/api/object-storage/audit/purge")
async def purge_audit_api(request: Request, user: str = Depends(require_login)):
    """Delete the Object Storage mutation audit for the selected cluster.

    This is deliberately cluster-scoped. The page can be switched between
    clusters, so a purge must never erase another cluster's audit trail.
    Requiring an explicit confirmation token protects the endpoint from an
    accidental POST or a stale browser click.
    """
    _require_admin(user)
    body = await request.json()
    if str(body.get("confirmation") or "") != "DELETE_ALL_AUDIT":
        raise HTTPException(status_code=400, detail="Thiếu xác nhận xóa toàn bộ audit")
    cluster = selected_cluster(request)
    with db.SessionLocal() as session:
        deleted = (
            session.query(ObjectStorageAuditEntry)
            .filter_by(cluster_id=cluster.id)
            .delete(synchronize_session=False)
        )
        session.commit()
    return {"ok": True, "deleted": deleted, "cluster_id": cluster.id}


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
                     query: str = Query("", max_length=MAX_QUERY_LENGTH), page: int = Query(1, ge=1),
                     page_size: int = Query(0, ge=0, le=100)):
    page_size = page_size if page_size in USER_PAGE_SIZES else PAGE_SIZE
    clusters, cluster = cluster_selection(request)
    inventory = {"items": [], "query": query.strip(), "page": page, "page_size": page_size, "page_count": 1, "total": 0}
    error = None
    try:
        inventory = await asyncio.to_thread(_cached_inventory, cluster, query, page, page_size)
    except RgwLogError as exc:
        error = str(exc)
    return templates.TemplateResponse(request, "object_storage_users.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "clusters": clusters,
        "selected_cluster": cluster, "inventory": inventory, "error": error,
        "quote_value": lambda value: quote(value, safe=""),
        "audit_entries": await asyncio.to_thread(_audit_rows, cluster.id) if auth.is_admin_user(user) else [],
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
