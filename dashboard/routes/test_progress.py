"""Live progress of the pre-push test run and of CI on main (admin only)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import test_progress

router = APIRouter()
templates = make_templates()


def _require_admin(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ tài khoản admin mới xem được tiến độ test")


@router.get("/test-progress", response_class=HTMLResponse)
async def test_progress_page(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    return templates.TemplateResponse(
        request, "test_progress.html",
        {"user": user, "is_admin": True, "ci_repo": settings.ci_github_repo},
    )


@router.get("/api/test-progress")
async def test_progress_api(user: str = Depends(require_login)) -> dict:
    _require_admin(user)
    local = test_progress.read_local_run(Path(settings.test_progress_file))
    ci = await asyncio.to_thread(test_progress.fetch_ci_runs, settings.ci_github_repo)
    return {"local": local, "ci": ci}
