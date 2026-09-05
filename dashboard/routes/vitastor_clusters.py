"""Vitastor cluster connection inventory and connection checks."""

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.vitastor import require_vitastor_login
from dashboard.templating import make_templates
from shared import db
from shared.models import VitastorCluster, VitastorOperation
from vitastor.client import VALID_EXEC_MODES, VitastorConnectionError, query_status

router = APIRouter(prefix="/vitastor/clusters", tags=["vitastor-clusters"])
templates = make_templates()
IN_FLIGHT_OPERATIONS = ("PENDING_APPROVAL", "RUNNING")


def _require_admin(user: str) -> None:
    if not auth.is_vitastor_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ Vitastor Admin được quản lý kết nối cụm")


def _context(user: str, *, error: str | None = None, success: str | None = None, form_values: dict | None = None) -> dict:
    with db.SessionLocal() as session:
        clusters = session.query(VitastorCluster).order_by(VitastorCluster.created_at.desc()).all()
        session.expunge_all()
    return {"user": user, "clusters": clusters, "error": error, "success": success, "form_values": form_values or {}, "exec_modes": sorted(VALID_EXEC_MODES)}


def _connection_args(cluster: VitastorCluster) -> tuple:
    return (cluster.management_host, cluster.ssh_user, cluster.ssh_key_path, cluster.etcd_address, cluster.etcd_prefix, cluster.config_path, cluster.exec_mode, cluster.container_name)


def _has_in_flight_operation(session, cluster_id: str) -> bool:
    return session.query(VitastorOperation.id).filter(
        VitastorOperation.cluster_id == cluster_id,
        VitastorOperation.status.in_(IN_FLIGHT_OPERATIONS),
    ).first() is not None


@router.get("", response_class=HTMLResponse)
async def clusters_page(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user))


@router.post("/create", response_class=HTMLResponse)
async def create_cluster(
    request: Request, user: str = Depends(require_vitastor_login), name: str = Form(""),
    management_host: str = Form(""), etcd_address: str = Form(""),
    etcd_prefix: str = Form("/vitastor"), config_path: str = Form(""),
    ssh_user: str = Form(""), ssh_key_path: str = Form(""),
    exec_mode: str = Form("none"), container_name: str = Form(""),
):
    _require_admin(user)
    submitted = {
        "name": name.strip(), "management_host": management_host.strip(),
        "etcd_address": etcd_address.strip(), "etcd_prefix": etcd_prefix.strip() or "/vitastor",
        "config_path": config_path.strip(), "ssh_user": ssh_user.strip(),
        "ssh_key_path": ssh_key_path.strip(), "exec_mode": exec_mode.strip() or "none",
        "container_name": container_name.strip(),
    }
    error = None
    if not all((submitted["name"], submitted["management_host"], submitted["ssh_user"], submitted["ssh_key_path"])):
        error = "Vui lòng điền tên cụm, management host, SSH user và SSH key path."
    elif not submitted["etcd_address"] and not submitted["config_path"]:
        error = "Cần khai báo Etcd address hoặc Config path."
    elif submitted["exec_mode"] not in VALID_EXEC_MODES:
        error = "Kiểu chạy Vitastor không hợp lệ."
    elif submitted["exec_mode"] != "none" and not submitted["container_name"]:
        error = "Chạy bằng container cần khai báo tên container."
    if error:
        return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, error=error, form_values=submitted))
    try:
        status = await asyncio.to_thread(query_status, submitted["management_host"], submitted["ssh_user"], submitted["ssh_key_path"], submitted["etcd_address"], submitted["etcd_prefix"], submitted["config_path"], submitted["exec_mode"], submitted["container_name"])
    except VitastorConnectionError as exc:
        return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, error=f"Không kết nối được tới cụm Vitastor: {exc}", form_values=submitted))
    with db.SessionLocal() as session:
        if session.query(VitastorCluster).filter_by(name=submitted["name"]).first():
            return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, error=f"Tên cụm {submitted['name']!r} đã tồn tại.", form_values=submitted))
        session.add(VitastorCluster(**submitted, is_active=True, last_status_json=json.dumps(status), last_checked_at=datetime.utcnow(), created_by=user))
        session.commit()
    return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, success=f"Đã kết nối và thêm cụm Vitastor {submitted['name']!r}."))


@router.post("/{cluster_id}/check", response_class=HTMLResponse)
async def check_cluster(request: Request, cluster_id: str, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        cluster = session.get(VitastorCluster, cluster_id)
        if cluster is None:
            return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, error="Không tìm thấy cụm."))
        args, cluster_name = _connection_args(cluster), cluster.name
    try:
        status = await asyncio.to_thread(query_status, *args)
    except VitastorConnectionError as exc:
        return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, error=f"Kiểm tra {cluster_name!r} thất bại: {exc}"))
    with db.SessionLocal() as session:
        cluster = session.get(VitastorCluster, cluster_id)
        cluster.last_status_json, cluster.last_checked_at = json.dumps(status), datetime.utcnow()
        session.commit()
    return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, success=f"Kết nối cụm {cluster_name!r} hoạt động."))


@router.post("/{cluster_id}/toggle-active", response_class=HTMLResponse)
async def toggle_cluster(request: Request, cluster_id: str, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        cluster = session.get(VitastorCluster, cluster_id)
        if cluster is None:
            return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, error="Không tìm thấy cụm."))
        if cluster.is_active and _has_in_flight_operation(session, cluster_id):
            return templates.TemplateResponse(
                request, "vitastor/clusters.html",
                _context(user, error="Không thể vô hiệu hoá cụm khi đang có operation Vitastor chờ duyệt hoặc đang chạy."),
            )
        cluster.is_active = not cluster.is_active
        session.commit()
    return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user))


@router.post("/{cluster_id}/delete", response_class=HTMLResponse)
async def delete_cluster(request: Request, cluster_id: str, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        cluster = session.get(VitastorCluster, cluster_id)
        if cluster is None:
            return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, error="Không tìm thấy cụm."))
        if _has_in_flight_operation(session, cluster_id):
            return templates.TemplateResponse(
                request, "vitastor/clusters.html",
                _context(user, error="Không thể xoá cụm khi đang có operation Vitastor chờ duyệt hoặc đang chạy."),
            )
        name = cluster.name
        session.delete(cluster)
        session.commit()
    return templates.TemplateResponse(request, "vitastor/clusters.html", _context(user, success=f"Đã xoá kết nối cụm {name!r}."))
