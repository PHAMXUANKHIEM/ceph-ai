"""Live progress of the pre-push test run, the latest production deploy and CI on main (admin only)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import ci_control, test_progress

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
        {"user": user, "is_admin": True, "ci_repo": settings.ci_github_repo,
         "token_configured": ci_control.read_token(Path(settings.ci_github_token_file)) is not None,
         "notice": request.query_params.get("ok"), "error": request.query_params.get("err")},
    )


@router.get("/api/test-progress")
async def test_progress_api(user: str = Depends(require_login)) -> dict:
    _require_admin(user)
    local = test_progress.read_local_run(Path(settings.test_progress_file))
    ci = await asyncio.to_thread(test_progress.fetch_ci_runs, settings.ci_github_repo)
    return {"local": local, "deploy": test_progress.read_latest_deploy(), "ci": ci,
            "deploy_request": ci_control.read_deploy_status(Path(settings.deploy_request_dir)),
            "latest_green_sha": _latest_green_sha(ci)}


def _latest_green_sha(ci: dict) -> str | None:
    """The newest run on main, only if it is green (never an older green one)."""
    runs = ci.get("runs") or []
    newest = runs[0] if runs else None
    if newest and newest.get("status") == "completed" and newest.get("conclusion") == "success":
        return newest.get("head_sha") or None
    return None


def _back(**message: str) -> RedirectResponse:
    return RedirectResponse("/test-progress?" + urlencode(message), status_code=303)


@router.post("/test-progress/ci-token")
async def save_ci_token(token: str = Form(""), user: str = Depends(require_login)):
    _require_admin(user)
    try:
        ci_control.save_token(Path(settings.ci_github_token_file), token)
    except (ci_control.CiControlError, OSError) as exc:
        return _back(err=str(exc) if isinstance(exc, ci_control.CiControlError) else "Không lưu được token.")
    return _back(ok="Đã lưu GitHub token.")


@router.post("/test-progress/ci-run")
async def run_ci(ref: str = Form("main"), user: str = Depends(require_login)):
    _require_admin(user)
    token = ci_control.read_token(Path(settings.ci_github_token_file))
    try:
        await asyncio.to_thread(ci_control.dispatch_ci, settings.ci_github_repo, settings.ci_github_workflow,
                                ref.strip() or "main", token)
    except ci_control.CiControlError as exc:
        return _back(err=str(exc))
    test_progress.clear_ci_cache()
    return _back(ok=f"Đã yêu cầu GitHub chạy CI cho nhánh {ref.strip() or 'main'}.")


@router.post("/test-progress/deploy")
async def request_deploy(sha: str = Form(""), confirmation: str = Form(""), user: str = Depends(require_login)):
    _require_admin(user)
    ci = await asyncio.to_thread(test_progress.fetch_ci_runs, settings.ci_github_repo)
    if sha != _latest_green_sha(ci):
        return _back(err="Chỉ deploy được commit mới nhất trên main có CI xanh.")
    try:
        ci_control.request_deploy(Path(settings.deploy_request_dir), sha=sha, confirmation=confirmation, user=user)
    except (ci_control.CiControlError, OSError) as exc:
        return _back(err=str(exc) if isinstance(exc, ci_control.CiControlError) else "Không ghi được yêu cầu deploy.")
    return _back(ok=f"Đã gửi yêu cầu deploy {sha[:8]}; theo dõi ở thẻ Deploy lên production.")
