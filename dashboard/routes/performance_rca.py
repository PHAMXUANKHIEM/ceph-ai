from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from watcher.performance_rca import report

router = APIRouter()
templates = make_templates()


@router.get("/api/performance-rca")
async def performance_rca_api(
    request: Request,
    pool: str | None = None,
    image: str | None = None,
    window_hours: int = 1,
    _user: str = Depends(require_login),
):
    _clusters, cluster = cluster_selection(request)
    return report(cluster, pool=pool, image=image, window_hours=window_hours)


@router.get("/performance-rca", response_class=HTMLResponse)
async def performance_rca_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    data = report(cluster)
    return templates.TemplateResponse(request, "performance_rca.html", {
        "user": user,
        "clusters": clusters,
        "cluster": cluster,
        **data,
    })
