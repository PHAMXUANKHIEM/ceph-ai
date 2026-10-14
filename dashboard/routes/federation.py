"""Federation page: join our federation with an activation code, preview and send counts (R8, 14/10/2026).

Admin only. The work is in shared/federation_client.py; this page never sends the
stored login back to the browser, and nothing leaves the install until an admin
presses "Gửi".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import federation_client
from shared.federation_client import FederationError

router = APIRouter()
templates = make_templates()
log = logging.getLogger(__name__)


class EnrollRequest(BaseModel):
    code: str = ""


def _admin(user: str = Depends(require_login)) -> str:
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được quản lý federation.")
    return user


@router.get("/federation", response_class=HTMLResponse)
async def federation_page(request: Request, user: str = Depends(_admin)):
    payload = await run_in_threadpool(federation_client.build_payload)
    return templates.TemplateResponse(request, "federation.html", {
        "user": user, "is_admin": True, "status": federation_client.public_status(), "payload": payload,
    })


@router.get("/api/federation")
def federation_status(user: str = Depends(_admin)) -> dict:
    return federation_client.public_status()


@router.get("/api/federation/preview")
def federation_preview(user: str = Depends(_admin)) -> dict:
    return federation_client.build_payload()


@router.post("/api/federation/enroll")
def federation_enroll(body: EnrollRequest, user: str = Depends(_admin)) -> dict:
    try:
        status = federation_client.enroll(body.code)
    except FederationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("federation_enrolled by %s (customer %s)", user, status.get("customer_id"))
    return status


@router.post("/api/federation/push")
def federation_push(user: str = Depends(_admin)) -> dict:
    try:
        status = federation_client.push()
    except FederationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("federation_pushed by %s (customer %s)", user, status.get("customer_id"))
    return status


@router.post("/api/federation/forget")
def federation_forget(user: str = Depends(_admin)) -> dict:
    federation_client.forget()
    log.info("federation_forgotten by %s", user)
    return federation_client.public_status()
