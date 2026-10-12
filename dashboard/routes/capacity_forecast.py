from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import selected_cluster
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import Cluster
from watcher.capacity_forecast import capacity_alert_lifecycle, forecasts
from watcher.rbd_logical_forecast import logical_forecasts
from watcher.capacity_failure_simulation import simulate
from watcher.capacity_planner import plan_capacity

router = APIRouter()
templates = make_templates()


def _cluster(request: Request) -> Cluster:
    """The cluster chosen by ``?cluster_id=`` or the session, within the user's cluster grants.

    This page used to look ``cluster_id`` up itself: it ignored the cluster
    selected in the header (always opening on the default) and the per-user
    cluster grants, so any user could read any cluster's forecast by URL
    (Plan readiness-gap-review R1, checklist 9.1).
    """
    return selected_cluster(request)


@router.post("/api/capacity-planner")
async def capacity_planner_api(request: Request, _user: str = Depends(require_login)):
    try:
        payload = await request.json()
        return plan_capacity(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/capacity-forecast")
async def capacity_forecast_api(request: Request, _user: str = Depends(require_login)):
    cluster = _cluster(request)
    return {"cluster_id": cluster.id, "cluster_name": cluster.name,
            **forecasts(cluster.id), "failure_simulation": simulate(cluster.id)}


@router.get("/api/capacity-forecast/rbd-logical")
async def rbd_logical_forecast_api(request: Request, _user: str = Depends(require_login)):
    """Opt-in read-only logical forecast, separate from physical capacity alerts."""
    cluster = _cluster(request)
    return logical_forecasts(cluster.id)


@router.get("/api/capacity-forecast/alerts")
async def capacity_forecast_alerts_api(request: Request, _user: str = Depends(require_login)):
    """Read-only lifecycle projection for physical-capacity forecast alerts."""
    cluster = _cluster(request)
    return {
        "cluster_id": cluster.id,
        "cluster_name": cluster.name,
        "alerts": capacity_alert_lifecycle(cluster.id),
    }


@router.get("/capacity-forecast", response_class=HTMLResponse)
async def capacity_forecast_page(request: Request, user: str = Depends(require_login)):
    cluster = _cluster(request)
    with db.SessionLocal() as session:
        clusters = session.query(Cluster).filter(Cluster.is_active.is_(True)).order_by(Cluster.name).all()
        data = forecasts(cluster.id)
        return templates.TemplateResponse(request, "capacity_forecast.html", {
            "user": user, "cluster": cluster, "clusters": clusters,
            "failure_simulation": simulate(cluster.id), **data,
        })
