"""Ops Report: operating experience per fault family on production clusters (croit-parity C6)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.ops_metrics import ops_report

router = APIRouter()
templates = make_templates()
WINDOWS = (7, 30, 90)


def _report(days: int) -> list[dict]:
    from watcher.incident_correlation import FAMILY_CODES

    with db.SessionLocal() as session:
        return [row.as_dict() for row in ops_report(session, family_codes=FAMILY_CODES, days=days)]


@router.get("/ops-report", response_class=HTMLResponse)
async def ops_report_page(request: Request, days: int = 30, user: str = Depends(require_login)):
    days = days if days in WINDOWS else 30
    rows = await asyncio.to_thread(_report, days)
    return templates.TemplateResponse(request, "ops_report.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "rows": rows, "days": days, "windows": WINDOWS,
    })
