from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from watcher.disk_failure_prediction import predict

router = APIRouter()
templates = make_templates()


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
