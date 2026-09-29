"""Admin-only, read-only view of configuration-dependent installation paths."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from scripts.architecture_profile import _load_installation_data, build_installation_profile

router = APIRouter()
templates = make_templates()


@router.get("/stream", response_class=HTMLResponse)
async def installation_stream_page(request: Request, user: str = Depends(require_login)):
    """Render the current install's architecture; never probe external services."""
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ tài khoản admin mới được xem luồng cấu hình hệ thống")
    clusters, selected = cluster_selection(request)
    settings, ceph_clusters, vitastor_clusters, providers = _load_installation_data()
    profile = build_installation_profile(settings, ceph_clusters, vitastor_clusters, providers)
    return templates.TemplateResponse(
        request,
        "installation_stream.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "clusters": clusters,
            "selected_cluster": selected,
            "installation_profile": profile,
        },
    )
