import asyncio
import threading
from time import monotonic

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import ceph_features
from shared.cluster_nodes import resolve_ssh_creds
from watcher.ceph_client import run_ceph_json_command_with
from watcher.disk_failure_prediction import predict

router = APIRouter()
templates = make_templates()

CEPH_FEATURES_TTL_SECONDS = 600
_features_cache: dict[str, tuple[float, dict]] = {}
_features_lock = threading.Lock()


@router.get("/disk-risk", response_class=HTMLResponse)
async def disk_risk_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    return templates.TemplateResponse(request, "disk_risk.html", {
        "user": user, "clusters": clusters, "cluster": cluster, **predict(cluster.id),
    })


@router.get("/api/disk-risk")
async def disk_risk_api(request: Request, _user: str = Depends(require_login)):
    _clusters, cluster = cluster_selection(request)
    return {"cluster_id": cluster.id, "cluster_name": cluster.name, **predict(cluster.id)}


def _collect_ceph_features(cluster) -> dict:
    nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(cluster)

    def runner(command: str):
        return run_ceph_json_command_with(nodes, container_name, ssh_user, ssh_key_path, exec_mode, command)[1]

    return ceph_features.collect(runner)


def _cached_ceph_features(cluster, *, refresh: bool = False) -> dict:
    """At most one collection per cluster every CEPH_FEATURES_TTL_SECONDS."""
    with _features_lock:
        cached = _features_cache.get(cluster.id)
        if cached and not refresh and monotonic() - cached[0] < CEPH_FEATURES_TTL_SECONDS:
            return cached[1]
    report = _collect_ceph_features(cluster)
    with _features_lock:
        _features_cache[cluster.id] = (monotonic(), report)
    return report


@router.get("/api/ceph-features")
async def ceph_features_api(request: Request, _user: str = Depends(require_login)):
    """Read-only status of Ceph's devicehealth / diskprediction_local /
    pg_autoscaler / balancer for the selected cluster (autonomy plan WP4)."""
    _clusters, cluster = cluster_selection(request)
    refresh = request.query_params.get("refresh") == "1"
    report = await asyncio.to_thread(_cached_ceph_features, cluster, refresh=refresh)
    return {"cluster_id": cluster.id, "cluster_name": cluster.name, **report}
