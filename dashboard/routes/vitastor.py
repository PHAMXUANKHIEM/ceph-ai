"""Vitastor UI namespace.

This module intentionally imports no Ceph clients, models, monitors or routes.
Vitastor functionality will grow behind this boundary as a separate product.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes.auth import require_login
from dashboard.templating import make_templates

router = APIRouter(prefix="/vitastor", tags=["vitastor"])
templates = make_templates()


async def require_vitastor_login(request: Request, user: str = Depends(require_login)) -> str:
    if request.session.get("product") != "vitastor":
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
async def vitastor_home(request: Request, user: str = Depends(require_vitastor_login)):
    return templates.TemplateResponse(request, "vitastor/index.html", {"user": user})
