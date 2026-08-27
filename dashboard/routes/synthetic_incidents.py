import asyncio
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.clusters import list_active_clusters
from shared.models import Cluster, Incident
from shared.synthetic_incidents import SCENARIOS, SyntheticInjectionError, cleanup, create
from watcher import publisher

router = APIRouter()
templates = make_templates()


def _page_context(request: Request, user: str, *, message: str = "", error: str = "") -> dict:
    with db.SessionLocal() as session:
        clusters = list_active_clusters(session)
        rows = []
        incidents = (
            session.query(Incident)
            .filter(Incident.signal_evidence_json.like('%synthetic_injection%'))
            .order_by(Incident.created_at.desc()).limit(30).all()
        )
        for incident in incidents:
            try:
                evidence = json.loads(incident.signal_evidence_json or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(evidence, dict) or evidence.get("synthetic_injection") is not True:
                continue
            cluster = session.get(Cluster, incident.cluster_id) if incident.cluster_id else None
            rows.append({"incident": incident, "cluster_name": cluster.name if cluster else "-",
                         "scenario": evidence.get("scenario", "-"), "run_id": evidence.get("run_id", "-")})
    selected = next((c for c in clusters if c.is_default), clusters[0] if clusters else None)
    return {"user": user, "is_admin": auth.is_admin_user(user), "clusters": clusters,
            "selected_cluster": selected, "scenarios": list(SCENARIOS.values()),
            "synthetic_rows": rows, "message": message, "error": error}


@router.get("/synthetic-incidents", response_class=HTMLResponse)
async def synthetic_incidents_page(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được chạy synthetic test")
    return templates.TemplateResponse(
        request, "synthetic_incidents.html",
        _page_context(request, user, message=request.query_params.get("message", ""),
                      error=request.query_params.get("error", "")),
    )


@router.post("/synthetic-incidents/inject")
async def inject_synthetic_incident(request: Request, cluster_id: str = Form(...), scenario: str = Form(...),
                                    publish: str = Form("0"), user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được chạy synthetic test")
    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy cluster")
        try:
            incident, envelope = create(session, cluster=cluster, scenario_id=scenario, actor=user)
            session.commit()
            incident_id = incident.id
        except SyntheticInjectionError as exc:
            session.rollback()
            return templates.TemplateResponse(request, "synthetic_incidents.html",
                                              _page_context(request, user, error=str(exc)))
    if publish == "1":
        try:
            await asyncio.to_thread(lambda: asyncio.run(publisher.publish_incident(envelope)))
        except Exception as exc:
            return templates.TemplateResponse(request, "synthetic_incidents.html",
                                              _page_context(request, user, error=f"Đã tạo Incident nhưng publish thất bại: {exc}"))
    mode = "đã gửi vào AI queue" if publish == "1" else "đã tạo, chưa gửi queue"
    return RedirectResponse(f"/synthetic-incidents?message=Incident+{incident_id}+{mode}", status_code=303)


@router.post("/synthetic-incidents/cleanup")
async def cleanup_synthetic_incidents(request: Request, cluster_id: str = Form(...), run_id: str = Form(""),
                                      user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được cleanup synthetic test")
    with db.SessionLocal() as session:
        changed = cleanup(session, cluster_id=cluster_id, run_id=run_id.strip() or None)
        session.commit()
    return RedirectResponse(f"/synthetic-incidents?message=Đã cleanup {changed} synthetic Incident", status_code=303)
