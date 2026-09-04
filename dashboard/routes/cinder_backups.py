"""Cinder Volume Backup inventory shared by the Ceph and Vitastor UIs."""

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dashboard.cinder_discovery import discover_cinder_volume_backups
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


async def _backup_context(request: Request, user: str, cluster, *, product: str, clusters):
    result = await asyncio.to_thread(discover_cinder_volume_backups, cluster) if cluster else {
        "items": [], "error": "Chưa có cấu hình OpenStack cho dashboard."
    }
    return {
        "request": request,
        "user": user,
        "is_admin": auth.is_admin_user(user) if product == "ceph" else auth.is_vitastor_admin_user(user),
        "clusters": clusters,
        "selected_cluster": cluster,
        "cinder_backups": result.get("items", []),
        "cinder_backup_error": result.get("error"),
        "cinder_backup_count": result.get("count", 0),
        "product": product,
    }


@router.get("/cinder-backups", response_class=HTMLResponse)
async def cinder_backups_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    context = await _backup_context(request, user, cluster, product="ceph", clusters=clusters)
    return templates.TemplateResponse(request, "cinder_backups.html", context)


@router.get("/vitastor/cinder-backups", response_class=HTMLResponse)
async def vitastor_cinder_backups_page(request: Request, user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        clusters = session.query(VitastorCluster).filter(VitastorCluster.is_active.is_(True)).order_by(VitastorCluster.name).all()
        session.expunge_all()
    context = await _backup_context(
        request, user, _default_openstack_cluster(), product="vitastor", clusters=clusters
    )
    return templates.TemplateResponse(request, "vitastor/cinder_backups.html", context)
