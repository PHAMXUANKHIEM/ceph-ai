import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import AuditEntry

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()


# How many rows the Dashboard's Audit Trail preview (dashboard/routes/
# incidents.py's index()) shows — a display limit, not a data limit; the
# full unfiltered/paginated-in-spirit history is always available at
# /audit itself. Kept here (next to the query it bounds) rather than in
# incidents.py so this stays the one place that owns "how audit entries are
# fetched."
DASHBOARD_PREVIEW_LIMIT = 20


def recent_audit_entries(session, limit: int = DASHBOARD_PREVIEW_LIMIT) -> list[AuditEntry]:
    """Same base query audit_trail() below runs with no filters applied
    (order_by(AuditEntry.created_at.desc())), just capped to `limit` — the
    Dashboard page's Audit Trail preview calls this instead of duplicating
    the query, so both places stay backed by the exact same data source."""
    return session.query(AuditEntry).order_by(AuditEntry.created_at.desc()).limit(limit).all()


def _parse_datetime_filter(raw: str) -> datetime | None:
    """Accepts the value an HTML <input type="datetime-local"> submits
    (`YYYY-MM-DDTHH:MM`). Returns None for blank/unparseable input rather
    than raising — an invalid filter should be silently ignored (show
    everything), not 500 the whole page."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@router.get("/audit", response_class=HTMLResponse)
async def audit_trail(
    request: Request,
    user: str = Depends(require_login),
    incident_id: str = "",
    since: str = "",
    until: str = "",
):
    """Story 4.4: read-only history (AD-7 — no update/delete route exists
    for AuditEntry anywhere in this codebase, and this route doesn't add
    one). Filters are optional query params so the base `/audit` URL still
    shows the full trail."""
    incident_id = incident_id.strip()
    since_dt = _parse_datetime_filter(since.strip())
    until_dt = _parse_datetime_filter(until.strip())

    try:
        with db.SessionLocal() as session:
            query = session.query(AuditEntry)
            if incident_id:
                query = query.filter(AuditEntry.incident_id == incident_id)
            if since_dt is not None:
                query = query.filter(AuditEntry.created_at >= since_dt)
            if until_dt is not None:
                query = query.filter(AuditEntry.created_at <= until_dt)
            entries = query.order_by(AuditEntry.created_at.desc()).all()
    except SQLAlchemyError:
        logger.exception("audit_trail: failed to query audit_entries from DB")
        raise HTTPException(
            status_code=503,
            detail="Không kết nối được database — đã chạy `alembic upgrade head` chưa?",
        )

    return templates.TemplateResponse(
        request,
        "audit_trail.html",
        {
            "user": user,
            "entries": entries,
            "filter_incident_id": incident_id,
            "filter_since": since,
            "filter_until": until,
        },
    )
