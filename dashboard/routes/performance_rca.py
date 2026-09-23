from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from dashboard.cluster_scope import cluster_selection
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from watcher.block_storage_diagnosis import build_diagnosis
from watcher.performance_rca import build_report, report
from watcher.performance_simulation import simulate_scenario

router = APIRouter()
templates = make_templates()


@router.get("/api/performance-rca/diagnosis")
async def performance_diagnosis_api(
    request: Request,
    pool: str = Query(min_length=1, max_length=64),
    image: str = Query(min_length=1, max_length=128),
    window_hours: int = Query(default=1, ge=1, le=6),
    _user: str = Depends(require_login),
):
    """Fast, scoped diagnosis from persisted samples only; never queries Ceph.

    The report engine deliberately receives an unavailable live-signal marker
    so no HTTP request opens SSH. A missing or old sample fails closed.
    """
    requested_cluster = (
        request.query_params.get("cluster_id", "").strip()
        or request.query_params.get("cluster", "").strip()
    )
    _clusters, cluster = cluster_selection(request)
    if requested_cluster and cluster.id != requested_cluster:
        raise HTTPException(status_code=404, detail="Cluster không tồn tại hoặc đã bị vô hiệu hóa")

    def load():
        with db.SessionLocal() as session:
            result = build_report(
                session, cluster.id, pool=pool, image=image,
                window_hours=window_hours, live_signals={"status": "unavailable"},
            )
        return build_diagnosis(result, pool=pool, image=image)

    return await run_in_threadpool(load)


@router.get("/api/performance-rca")
async def performance_rca_api(
    request: Request,
    pool: str | None = None,
    image: str | None = None,
    window_hours: int = 1,
    _user: str = Depends(require_login),
):
    _clusters, cluster = cluster_selection(request)
    return await run_in_threadpool(report, cluster, pool=pool, image=image, window_hours=window_hours)


@router.post("/api/performance-rca/simulate")
async def performance_rca_simulation_api(
    request: Request,
    body: dict = Body(...),
    _user: str = Depends(require_login),
):
    """Return a typed, read-only recommendation simulation preview."""
    _clusters, cluster = cluster_selection(request)
    result = await run_in_threadpool(simulate_scenario, body)
    result["cluster_id"] = cluster.id
    result["cluster_scoped"] = True
    return result


@router.get("/performance-rca", response_class=HTMLResponse)
async def performance_rca_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    data = await run_in_threadpool(report, cluster)
    return templates.TemplateResponse(request, "performance_rca.html", {
        "user": user,
        "clusters": clusters,
        "cluster": cluster,
        **data,
    })
