"""Unified, read-only event timeline (AI roadmap Phase 6.1)."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection
from dashboard.routes.auth import require_login
from dashboard.routes import auth
from dashboard.templating import make_templates
from shared import db
from shared.unified_timeline import DEFAULT_LIMIT, MAX_LIMIT, build_unified_timeline

router = APIRouter()
templates = make_templates()


def _parse_filter(value: str, *, field: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} không phải ISO-8601 hợp lệ") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _query_context(request: Request, since: str, until: str, limit: int):
    clusters, selected = cluster_selection(request)
    since_dt = _parse_filter(since, field="since")
    until_dt = _parse_filter(until, field="until")
    if since_dt is None and until_dt is None:
        since_dt = datetime.utcnow() - timedelta(hours=24)
    if since_dt is not None and until_dt is not None and since_dt > until_dt:
        raise HTTPException(status_code=400, detail="since phải nhỏ hơn hoặc bằng until")
    try:
        bounded_limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="limit không hợp lệ") from exc
    with db.SessionLocal() as session:
        events = build_unified_timeline(
            session, cluster_id=selected.id, is_default=selected.is_default,
            since=since_dt, until=until_dt, limit=bounded_limit,
        )
    return clusters, selected, since_dt, until_dt, bounded_limit, events


@router.get("/event-timeline", response_class=HTMLResponse)
async def event_timeline_page(
    request: Request, since: str = "", until: str = "", limit: int = DEFAULT_LIMIT,
    user: str = Depends(require_login),
):
    clusters, selected, since_dt, until_dt, bounded_limit, events = _query_context(
        request, since, until, limit
    )
    return templates.TemplateResponse(request, "event_timeline.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "clusters": clusters,
        "selected_cluster": selected, "events": events, "since": since,
        "until": until, "limit": bounded_limit, "since_dt": since_dt,
        "until_dt": until_dt,
    })


@router.get("/api/event-timeline")
async def event_timeline_api(
    request: Request, since: str = "", until: str = "", limit: int = DEFAULT_LIMIT,
    user: str = Depends(require_login),
):
    _clusters, selected, since_dt, until_dt, bounded_limit, events = _query_context(
        request, since, until, limit
    )
    return {
        "cluster_id": selected.id,
        "cluster_name": selected.name,
        "since": since_dt.isoformat() if since_dt else None,
        "until": until_dt.isoformat() if until_dt else None,
        "limit": bounded_limit,
        "events": events,
    }
