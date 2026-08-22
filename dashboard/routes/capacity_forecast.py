from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import Cluster
from watcher.capacity_forecast import forecasts

router = APIRouter()
templates = make_templates()


def _cluster(cluster_id: str | None) -> Cluster:
    with db.SessionLocal() as session:
        query = session.query(Cluster).filter(Cluster.is_active.is_(True))
        row = query.filter(Cluster.id == cluster_id).first() if cluster_id else query.order_by(Cluster.is_default.desc()).first()
        if row is None:
            raise HTTPException(404, "Không tìm thấy cụm Ceph đang hoạt động")
        session.expunge(row)
        return row


@router.get("/api/capacity-forecast")
async def capacity_forecast_api(cluster_id: str | None = None, _user: str = Depends(require_login)):
    cluster = _cluster(cluster_id)
    return {"cluster_id": cluster.id, "cluster_name": cluster.name, **forecasts(cluster.id)}


@router.get("/capacity-forecast", response_class=HTMLResponse)
async def capacity_forecast_page(request: Request, cluster_id: str | None = None, user: str = Depends(require_login)):
    cluster = _cluster(cluster_id)
    with db.SessionLocal() as session:
        clusters = session.query(Cluster).filter(Cluster.is_active.is_(True)).order_by(Cluster.name).all()
        data = forecasts(cluster.id)
        return templates.TemplateResponse(request, "capacity_forecast.html", {
            "user": user, "cluster": cluster, "clusters": clusters, **data,
        })
