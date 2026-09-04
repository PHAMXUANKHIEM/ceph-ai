"""Cinder Volume Backup inventory shared by the Ceph and Vitastor UIs."""

import asyncio
from math import ceil
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.cinder_discovery import delete_cinder_volume_backup, discover_cinder_volume_backups
from dashboard.cluster_scope import cluster_selection
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.vitastor import require_vitastor_login
from dashboard.templating import make_templates
from shared import db
from shared.models import Cluster, VitastorCluster

router = APIRouter(tags=["cinder-backups"])
templates = make_templates()


def _default_openstack_cluster():
    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter(Cluster.is_default.is_(True)).first()
        if cluster:
            session.expunge(cluster)
        return cluster


def _list_query(request: Request, cluster) -> str:
    values = {
        "id": request.query_params.get("id", "").strip(),
        "backend": request.query_params.get("backend", "").strip(),
        "volume_type": request.query_params.get("volume_type", "").strip(),
        "page": request.query_params.get("page", "1").strip() or "1",
    }
    if cluster:
        values["cluster"] = str(cluster.id)
    return urlencode({key: value for key, value in values.items() if value})


def _message_redirect(request: Request, path: str, *, success: str = "", error: str = "") -> RedirectResponse:
    allowed = {"cluster", "id", "backend", "volume_type", "page"}
    values = {key: value for key, value in request.query_params.items() if key in allowed and value}
    if success:
        values["success"] = success
    if error:
        values["error"] = error
    query = urlencode(values)
    return RedirectResponse(path + (f"?{query}" if query else ""), status_code=303)


def _csrf_token(request: Request) -> str:
    token = request.session.get("cinder_backup_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["cinder_backup_csrf_token"] = token
    return token


def _valid_csrf_token(request: Request, token: str) -> bool:
    expected = str(request.session.get("cinder_backup_csrf_token") or "")
    supplied = str(token or "")
    return bool(expected and supplied) and secrets.compare_digest(expected, supplied)


def _backup_is_listed(result: dict, backup_id: str) -> bool:
    return any(
        str(item.get("id") or "").casefold() == str(backup_id).casefold()
        for item in result.get("items", [])
        if isinstance(item, dict)
    )


async def _backup_context(request: Request, user: str, cluster, *, product: str, clusters):
    result = await asyncio.to_thread(discover_cinder_volume_backups, cluster, None) if cluster else {
        "items": [], "error": "Chưa có cấu hình OpenStack cho dashboard."
    }
    all_items = result.get("items", [])
    id_filter = request.query_params.get("id", "").strip()
    backend_filter = request.query_params.get("backend", "").strip().casefold()
    volume_type_filter = request.query_params.get("volume_type", "").strip().casefold()
    filtered_items = [
        item for item in all_items
        if (not id_filter or id_filter.casefold() in str(item.get("id") or "").casefold())
        and (not backend_filter or str(item.get("source") or "").casefold() == backend_filter)
        and (not volume_type_filter or str(item.get("volume_type") or "").casefold() == volume_type_filter)
    ]
    volume_types = sorted({
        str(item.get("volume_type") or "—")
        for item in all_items
        if item.get("volume_type")
    }, key=str.casefold)
    page_size = 10
    try:
        requested_page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        requested_page = 1
    page_count = max(1, ceil(len(filtered_items) / page_size))
    page = min(requested_page, page_count)
    start = (page - 1) * page_size
    page_items = filtered_items[start:start + page_size]
    page_filter_values = {
        "id": id_filter,
        "backend": backend_filter,
        "volume_type": request.query_params.get("volume_type", "").strip(),
    }
    if cluster:
        page_filter_values["cluster"] = str(cluster.id)
    page_queries = {
        page_number: urlencode({**page_filter_values, "page": page_number})
        for page_number in range(1, page_count + 1)
    }
    return {
        "request": request,
        "user": user,
        "is_admin": auth.is_admin_user(user) if product == "ceph" else auth.is_vitastor_admin_user(user),
        "clusters": clusters,
        "selected_cluster": cluster,
        "cinder_backups": page_items,
        "cinder_backup_total": len(all_items),
        "cinder_backup_filtered": len(filtered_items),
        "cinder_backup_page": page,
        "cinder_backup_page_count": page_count,
        "cinder_backup_page_queries": page_queries,
        "cinder_backup_volume_types": volume_types,
        "cinder_backup_id_filter": id_filter,
        "cinder_backup_backend_filter": backend_filter,
        "cinder_backup_volume_type_filter": request.query_params.get("volume_type", "").strip(),
        "cinder_backup_list_query": _list_query(request, cluster),
        "cinder_backup_success": request.query_params.get("success", ""),
        "cinder_backup_message": request.query_params.get("error", ""),
        "cinder_backup_error": result.get("error"),
        "cinder_backup_count": result.get("count", 0),
        "product": product,
        "cinder_backup_base_path": "/vitastor/cinder-backups" if product == "vitastor" else "/cinder-backups",
        "cinder_backup_csrf_token": _csrf_token(request),
    }


@router.get("/cinder-backups", response_class=HTMLResponse)
async def cinder_backups_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    context = await _backup_context(request, user, cluster, product="ceph", clusters=clusters)
    return templates.TemplateResponse(request, "cinder_backups.html", context)


@router.post("/cinder-backups/{backup_id}/delete")
async def delete_cinder_backup(
    request: Request,
    backup_id: str,
    confirmation: str = Form(""),
    csrf_token: str = Form(""),
    user: str = Depends(require_login),
):
    path = "/cinder-backups"
    if not auth.is_admin_user(user):
        return _message_redirect(request, path, error="Tài khoản hiện tại không có quyền xóa Cinder backup.")
    if not _valid_csrf_token(request, csrf_token):
        return _message_redirect(request, path, error="Phiên xác nhận không hợp lệ hoặc đã hết hạn.")
    _clusters, cluster = cluster_selection(request)
    if not cluster:
        return _message_redirect(request, path, error="Không xác định được cluster OpenStack.")
    discovered = await asyncio.to_thread(discover_cinder_volume_backups, cluster, None)
    if discovered.get("status") != "ok":
        return _message_redirect(request, path, error=discovered.get("error") or "Không xác minh được backup trong Cinder.")
    if not _backup_is_listed(discovered, backup_id):
        return _message_redirect(request, path, error="Backup không tồn tại trong cluster/OpenStack đang chọn.")
    result = await asyncio.to_thread(delete_cinder_volume_backup, cluster, backup_id, confirmation)
    if result.get("status") != "ok":
        return _message_redirect(request, path, error=result.get("error") or "Xóa Cinder backup thất bại.")
    return _message_redirect(request, path, success=f"Đã gửi yêu cầu xóa backup {backup_id}.")


@router.post("/vitastor/cinder-backups/{backup_id}/delete")
async def delete_vitastor_cinder_backup(
    request: Request,
    backup_id: str,
    confirmation: str = Form(""),
    csrf_token: str = Form(""),
    user: str = Depends(require_vitastor_login),
):
    path = "/vitastor/cinder-backups"
    if not auth.is_vitastor_admin_user(user):
        return _message_redirect(request, path, error="Tài khoản hiện tại không có quyền xóa Cinder backup.")
    if not _valid_csrf_token(request, csrf_token):
        return _message_redirect(request, path, error="Phiên xác nhận không hợp lệ hoặc đã hết hạn.")
    cluster = _default_openstack_cluster()
    if not cluster:
        return _message_redirect(request, path, error="Không xác định được cluster OpenStack.")
    discovered = await asyncio.to_thread(discover_cinder_volume_backups, cluster, None)
    if discovered.get("status") != "ok":
        return _message_redirect(request, path, error=discovered.get("error") or "Không xác minh được backup trong Cinder.")
    if not _backup_is_listed(discovered, backup_id):
        return _message_redirect(request, path, error="Backup không tồn tại trong cluster/OpenStack đang chọn.")
    result = await asyncio.to_thread(delete_cinder_volume_backup, cluster, backup_id, confirmation)
    if result.get("status") != "ok":
        return _message_redirect(request, path, error=result.get("error") or "Xóa Cinder backup thất bại.")
    return _message_redirect(request, path, success=f"Đã gửi yêu cầu xóa backup {backup_id}.")


@router.get("/vitastor/cinder-backups", response_class=HTMLResponse)
async def vitastor_cinder_backups_page(request: Request, user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        clusters = session.query(VitastorCluster).filter(VitastorCluster.is_active.is_(True)).order_by(VitastorCluster.name).all()
        session.expunge_all()
    context = await _backup_context(
        request, user, _default_openstack_cluster(), product="vitastor", clusters=clusters
    )
    return templates.TemplateResponse(request, "vitastor/cinder_backups.html", context)
