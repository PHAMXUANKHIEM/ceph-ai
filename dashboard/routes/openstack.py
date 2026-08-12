import asyncio
import re
import shlex

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.cluster_scope import cluster_connection, cluster_selection
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from watcher.ceph_client import CephQueryError, build_exec_command, run_ceph_json_command_with
from worker.executor.ssh_executor import ExecutorError, execute_command


router = APIRouter()
templates = make_templates()
_ENTITY_RE = re.compile(r"^client\.[A-Za-z0-9_.-]+$")
_POOL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _pool_names(payload: dict | list) -> list[str]:
    rows = payload if isinstance(payload, list) else payload.get("pools", []) if isinstance(payload, dict) else []
    return sorted({
        str(row.get("pool_name") or row.get("poolname"))
        for row in rows if isinstance(row, dict) and (row.get("pool_name") or row.get("poolname"))
    })


def _auth_rows(payload: dict | list) -> list[dict]:
    rows = payload if isinstance(payload, list) else payload.get("auth_dump", []) if isinstance(payload, dict) else []
    return sorted(
        [row for row in rows if isinstance(row, dict) and str(row.get("entity", "")).startswith("client.")],
        key=lambda row: str(row.get("entity", "")),
    )


def _caps_command(entity: str, pool: str, access: str, current_caps: dict) -> str:
    if not _ENTITY_RE.fullmatch(entity) or not _POOL_RE.fullmatch(pool):
        raise ValueError("User hoặc pool không hợp lệ")
    raw_caps = current_caps if isinstance(current_caps, dict) else {}
    caps = {str(k): str(v) for k, v in raw_caps.items() if v is not None}
    profile = "rbd-read-only" if access == "read" else "rbd"
    pool_cap = f"profile {profile} pool={pool}"
    existing_osd = [part.strip() for part in caps.get("osd", "").split(",") if part.strip()]
    if pool_cap not in existing_osd:
        existing_osd.append(pool_cap)
    caps["osd"] = ", ".join(existing_osd)
    caps.setdefault("mon", "profile rbd")
    pieces = ["ceph", "auth", "caps", entity]
    for subsystem in ("mon", "mgr", "osd", "mds"):
        if caps.get(subsystem):
            pieces.extend((subsystem, caps[subsystem]))
    return " ".join(shlex.quote(piece) for piece in pieces)


def _create_auth_command(entity: str, pool: str, access: str) -> str:
    if not _ENTITY_RE.fullmatch(entity) or not _POOL_RE.fullmatch(pool):
        raise ValueError("User hoặc pool không hợp lệ")
    profile = "rbd-read-only" if access == "read" else "rbd"
    pieces = [
        "ceph", "auth", "get-or-create", entity,
        "mon", "profile rbd",
        "osd", f"profile {profile} pool={pool}",
    ]
    return " ".join(shlex.quote(piece) for piece in pieces)


async def _auth_page_context(request: Request, user: str, active_view: str) -> dict:
    clusters, cluster = cluster_selection(request)
    connection = cluster_connection(cluster)
    pools: list[str] = []
    users: list[dict] = []
    error = None
    try:
        (_host, pool_payload), (_host2, auth_payload) = await asyncio.gather(
            asyncio.to_thread(run_ceph_json_command_with, *connection, "ceph osd pool ls detail"),
            asyncio.to_thread(run_ceph_json_command_with, *connection, "ceph auth ls"),
        )
        pools = _pool_names(pool_payload)
        users = _auth_rows(auth_payload)
    except CephQueryError as exc:
        error = str(exc)
    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "clusters": clusters,
        "selected_cluster": cluster,
        "pools": pools,
        "auth_users": users,
        "error": error,
        "success": request.query_params.get("saved") == "1",
        "created": request.query_params.get("created") == "1",
        "active_view": active_view,
    }


@router.get("/openstack/auth-pool", response_class=HTMLResponse)
async def auth_pool_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        request, "openstack_auth_pool.html", await _auth_page_context(request, user, "auth-pool")
    )


@router.get("/openstack/auth-user/create", response_class=HTMLResponse)
async def create_auth_user_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        request, "openstack_auth_pool.html", await _auth_page_context(request, user, "create-user")
    )


@router.post("/openstack/auth-pool")
async def grant_auth_pool(
    request: Request,
    user: str = Depends(require_login),
    entity: str = Form(""),
    pool: str = Form(""),
    access: str = Form("write"),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được cấp quyền OpenStack")
    clusters, cluster = cluster_selection(request)
    connection = cluster_connection(cluster)
    if access not in {"read", "write"}:
        raise HTTPException(status_code=400, detail="Quyền truy cập không hợp lệ")
    try:
        _host, pool_payload = await asyncio.to_thread(
            run_ceph_json_command_with, *connection, "ceph osd pool ls detail"
        )
        _host, auth_payload = await asyncio.to_thread(
            run_ceph_json_command_with, *connection, "ceph auth ls"
        )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không đọc được cấu hình Ceph: {exc}") from exc
    if pool not in _pool_names(pool_payload):
        raise HTTPException(status_code=400, detail="Pool không tồn tại trong cụm đang chọn")
    auth_by_entity = {str(row.get("entity")): row for row in _auth_rows(auth_payload)}
    if entity not in auth_by_entity:
        raise HTTPException(status_code=400, detail="Ceph auth user không tồn tại")
    try:
        inner_command = _caps_command(entity, pool, access, auth_by_entity[entity].get("caps") or {})
        command = build_exec_command(connection[4], connection[1], inner_command)
        await asyncio.to_thread(
            execute_command, connection[0][0], command, user=connection[2], key_path=connection[3]
        )
    except (ValueError, ExecutorError, IndexError) as exc:
        raise HTTPException(status_code=502, detail=f"Không cấp được quyền: {exc}") from exc
    return RedirectResponse(f"/openstack/auth-pool?cluster={cluster.id}&saved=1", status_code=303)


@router.post("/openstack/auth-user/create")
async def create_auth_user(
    request: Request,
    user: str = Depends(require_login),
    entity_name: str = Form(""),
    pool: str = Form(""),
    access: str = Form("write"),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được tạo Ceph auth user")
    _clusters, cluster = cluster_selection(request)
    connection = cluster_connection(cluster)
    entity = entity_name.strip()
    if not entity.startswith("client."):
        entity = f"client.{entity}"
    if access not in {"read", "write"}:
        raise HTTPException(status_code=400, detail="Quyền truy cập không hợp lệ")
    try:
        _host, pool_payload = await asyncio.to_thread(
            run_ceph_json_command_with, *connection, "ceph osd pool ls detail"
        )
        _host, auth_payload = await asyncio.to_thread(
            run_ceph_json_command_with, *connection, "ceph auth ls"
        )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không đọc được cấu hình Ceph: {exc}") from exc
    if pool not in _pool_names(pool_payload):
        raise HTTPException(status_code=400, detail="Pool không tồn tại trong cụm đang chọn")
    if entity in {str(row.get("entity")) for row in _auth_rows(auth_payload)}:
        raise HTTPException(status_code=409, detail="Ceph auth user đã tồn tại")
    try:
        inner_command = _create_auth_command(entity, pool, access)
        command = build_exec_command(connection[4], connection[1], inner_command)
        await asyncio.to_thread(
            execute_command, connection[0][0], command, user=connection[2], key_path=connection[3]
        )
    except (ValueError, ExecutorError, IndexError) as exc:
        raise HTTPException(status_code=502, detail=f"Không tạo được auth user: {exc}") from exc
    return RedirectResponse(
        f"/openstack/auth-user/create?cluster={cluster.id}&created=1", status_code=303
    )
