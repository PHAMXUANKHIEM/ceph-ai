from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import CephCapacitySample, Incident, IncidentTimelineEvent
from watcher.capacity_forecast import forecasts

router = APIRouter()
templates = make_templates()


@router.get("/ai-copilot", response_class=HTMLResponse)
async def copilot_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    cutoff = datetime.utcnow() - timedelta(hours=24)
    with db.SessionLocal() as session:
        recent = session.query(Incident).filter(
            Incident.cluster_id == cluster.id, Incident.detected_at >= cutoff,
        ).order_by(Incident.detected_at.desc()).limit(12).all()
        open_count = session.query(Incident).filter(
            Incident.cluster_id == cluster.id,
            Incident.status.in_(["OPEN", "DIAGNOSED", "PENDING_APPROVAL", "EXECUTING", "VERIFYING"]),
        ).count()
        event_count = session.query(IncidentTimelineEvent).join(
            Incident, Incident.id == IncidentTimelineEvent.incident_id,
        ).filter(Incident.cluster_id == cluster.id).count()
        sample_count = session.query(CephCapacitySample).filter_by(cluster_id=cluster.id).count()
        session.expunge_all()
    capacity = forecasts(cluster.id)
    return templates.TemplateResponse(request, "copilot.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "clusters": clusters,
        "selected_cluster": cluster, "recent_incidents": recent, "open_count": open_count,
        "event_count": event_count, "sample_count": sample_count, "capacity": capacity,
    })
