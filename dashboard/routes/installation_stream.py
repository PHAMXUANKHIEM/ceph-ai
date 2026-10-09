"""Admin-only, read-only view of configuration-dependent installation paths."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from scripts.architecture_profile import _load_installation_data, build_installation_profile
import logging

from shared import ai_flow, ceph_topology, db, failure_lab_config
from shared.models import Cluster

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()


def _require_admin(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ tài khoản admin mới được xem luồng cấu hình hệ thống")


def _topology(selected) -> dict | None:
    """The selected Ceph cluster's service topology from stored snapshots."""
    if selected is None:
        return None
    return _with_failure_lab(ceph_topology.build(selected), failure_lab_config.load())


def _with_failure_lab(topology: dict, config: failure_lab_config.LabConfig) -> dict:
    """Mark the topology of the Failure Lab staging cluster (Settings > Cụm Staging)."""
    if config.cluster_id and topology["cluster"]["id"] == config.cluster_id:
        topology["failure_lab"] = {"staging": True, "fsid_pinned": bool(config.fsid),
                                   "fault_enabled": config.fault_enabled, "window": config.window}
    return topology


def _staging_cluster() -> Cluster | None:
    """The active cluster chosen as Failure Lab staging, detached; None when there is none."""
    cluster_id = failure_lab_config.load().cluster_id
    if not cluster_id:
        return None
    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None or not cluster.is_active:
            return None
        session.expunge(cluster)
        return cluster


def _staging_topology(selected) -> dict | None:
    """The staging cluster's topology for its own Stream tab, unless it is the selected cluster."""
    try:
        cluster = _staging_cluster()
        if cluster is None or (selected is not None and str(selected.id) == str(cluster.id)):
            return None
        return _topology(cluster)
    except Exception:
        logger.exception("stream: staging cluster topology unavailable")
        return None


@router.get("/api/stream/staging-topology")
async def staging_topology_api(user: str = Depends(require_login)) -> dict:
    """Read-only topology of the Failure Lab staging cluster; no Ceph command runs."""
    _require_admin(user)
    cluster = _staging_cluster()
    if cluster is None:
        raise HTTPException(status_code=404, detail="Chưa cấu hình cụm Staging (Cài đặt > Cụm Staging)")
    topology = _topology(cluster)
    if topology is None:
        raise HTTPException(status_code=404, detail="Chưa có dữ liệu cụm Staging")
    return topology


@router.get("/api/stream/ceph-topology")
async def ceph_topology_api(request: Request, user: str = Depends(require_login)) -> dict:
    """Read-only Ceph service topology for the Stream page; no Ceph command runs."""
    _require_admin(user)
    _clusters, selected = cluster_selection(request)
    topology = _topology(selected)
    if topology is None:
        raise HTTPException(status_code=404, detail="Chưa có cụm Ceph nào được chọn")
    return topology


def _ai_flow() -> dict | None:
    """The AI flow overview; a broken count must not take the Stream page down."""
    try:
        return ai_flow.build()
    except Exception:
        logger.exception("stream: AI flow overview unavailable")
        return None


@router.get("/api/stream/ai-flow")
async def ai_flow_api(user: str = Depends(require_login)) -> dict:
    """Read-only overview of the AI loop with 24-hour counts; no Ceph command runs."""
    _require_admin(user)
    flow = _ai_flow()
    if flow is None:
        raise HTTPException(status_code=503, detail="Chưa đọc được số liệu luồng AI")
    return flow


@router.get("/stream", response_class=HTMLResponse)
def installation_stream_page(request: Request, user: str = Depends(require_login)):
    """Render the current install's architecture; never probe external services."""
    _require_admin(user)
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
            "ceph_topology": _topology(selected),
            "staging_topology": _staging_topology(selected),
            "ai_flow": _ai_flow(),
        },
    )
