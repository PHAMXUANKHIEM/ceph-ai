"""Live progress of the pre-push test run, the latest production deploy and CI on main (admin only)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import ci_control, release_candidates, test_progress

logger = logging.getLogger(__name__)
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
         "notice": request.query_params.get("ok"), "error": request.query_params.get("err"),
         "notice_url": _github_url(request.query_params.get("url"))},
    )


@router.get("/api/test-progress")
async def test_progress_api(user: str = Depends(require_login)) -> dict:
    _require_admin(user)
    local = test_progress.read_local_run(Path(settings.test_progress_file))
    ci = await asyncio.to_thread(test_progress.fetch_ci_runs, settings.ci_github_repo)
    deploy = test_progress.read_latest_deploy()
    return {"local": local, "deploy": deploy, "ci": ci,
            "deploy_request": ci_control.read_deploy_status(Path(settings.deploy_request_dir)),
            "latest_green_sha": _latest_green_sha(ci), "deployed_sha": _deployed_sha(deploy)}


def _deployed_sha(deploy: dict | None) -> str | None:
    """The revision production runs: the newest deploy that completed successfully."""
    return deploy["sha"] if deploy and deploy.get("status") == "passed" and deploy.get("sha") else None


def _latest_green_sha(ci: dict) -> str | None:
    """The newest run on main, only if it is green (never an older green one)."""
    runs = ci.get("runs") or []
    newest = runs[0] if runs else None
    if newest and newest.get("status") == "completed" and newest.get("conclusion") == "success":
        return newest.get("head_sha") or None
    return None


def _github_url(value: str | None) -> str | None:
    """Only links to this repository on GitHub are shown (the value comes from the query string)."""
    prefix = f"https://github.com/{settings.ci_github_repo}/"
    return value if value and value.startswith(prefix) and "\"" not in value and "<" not in value else None


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
    branch = ref.strip() or "main"
    started = datetime.now(timezone.utc)
    try:
        await asyncio.to_thread(ci_control.dispatch_ci, settings.ci_github_repo, settings.ci_github_workflow,
                                branch, token)
    except ci_control.CiControlError as exc:
        return _back(err=str(exc))
    url = await asyncio.to_thread(test_progress.find_dispatched_run, settings.ci_github_repo, branch, started)
    test_progress.clear_ci_cache()
    if url:
        return _back(ok=f"Đã chạy CI cho nhánh {branch}.", url=url)
    return _back(ok=f"Đã yêu cầu GitHub chạy CI cho nhánh {branch}; lượt chạy sẽ hiện ở thẻ CI sau ít giây.")


@router.post("/test-progress/deploy")
async def request_deploy(sha: str = Form(""), confirmation: str = Form(""), user: str = Depends(require_login)):
    _require_admin(user)
    ci = await asyncio.to_thread(test_progress.fetch_ci_runs, settings.ci_github_repo)
    if sha != _latest_green_sha(ci):
        return _back(err="Chỉ deploy được commit mới nhất trên main có CI xanh.")
    if sha == _deployed_sha(test_progress.read_latest_deploy()):
        return _back(err=f"{sha[:8]} đã được deploy và đang chạy; chưa có bản mới hơn.")
    try:
        ci_control.request_deploy(Path(settings.deploy_request_dir), sha=sha, confirmation=confirmation, user=user)
    except (ci_control.CiControlError, OSError) as exc:
        return _back(err=str(exc) if isinstance(exc, ci_control.CiControlError) else "Không ghi được yêu cầu deploy.")
    return _back(ok=f"Đã gửi yêu cầu deploy {sha[:8]}; theo dõi ở thẻ Deploy lên production.")


# --- PR release flow: candidate branches -------------------------------------------------

_summary_tasks: set[asyncio.Task] = set()


def _token() -> str | None:
    return ci_control.read_token(Path(settings.ci_github_token_file))


async def _summarize(candidate: dict) -> None:
    """Write the AI summary of one candidate's head commit (commit bodies if the AI fails)."""
    from shared.claude_cli import run_claude_prompt

    notes = Path(settings.release_notes_dir)
    sha = candidate["head_sha"]
    try:
        patch = await asyncio.to_thread(release_candidates.fetch_patch, settings.ci_github_repo,
                                        candidate["branch"], _token())
        text = await run_claude_prompt(release_candidates.summary_prompt(candidate, patch), timeout=180)
        release_candidates.write_summary(notes, sha, text or release_candidates.fallback_summary(candidate),
                                         source="ai" if text else "commit")
    except Exception:  # noqa: BLE001 - the commit bodies are an honest fallback
        logger.warning("release: AI summary for %s failed, using commit messages", sha[:8], exc_info=True)
        release_candidates.write_summary(notes, sha, release_candidates.fallback_summary(candidate), source="commit")
    finally:
        release_candidates.release_summary(sha)


@router.get("/api/test-progress/candidates")
async def candidates_api(user: str = Depends(require_login)) -> dict:
    _require_admin(user)
    result = await asyncio.to_thread(release_candidates.list_candidates, settings.ci_github_repo, _token(),
                                     Path(settings.release_notes_dir))
    for candidate in result["candidates"]:
        if candidate["summary"] is None and release_candidates.claim_summary(candidate["head_sha"]):
            task = asyncio.create_task(_summarize(candidate))
            _summary_tasks.add(task)
            task.add_done_callback(_summary_tasks.discard)
    return {**result, "token_configured": _token() is not None}


@router.post("/test-progress/candidates/open-prs")
async def open_candidate_prs(user: str = Depends(require_login)):
    """Open a PR (and so a CI run) for every candidate branch that has none."""
    _require_admin(user)
    token = _token()
    if token is None:
        return _back(err="Cần GitHub token (Contents, Pull requests, Actions: Read and write) để mở PR.")
    result = await asyncio.to_thread(release_candidates.list_candidates, settings.ci_github_repo, token,
                                     Path(settings.release_notes_dir), use_cache=False)
    opened = []
    try:
        for candidate in result["candidates"]:
            if candidate["pull"] is None:
                pull = await asyncio.to_thread(release_candidates.open_pull_request, settings.ci_github_repo,
                                               token, candidate)
                opened.append(f"#{pull['number']}")
    except release_candidates.ReleaseError as exc:
        return _back(err=str(exc))
    finally:
        release_candidates.clear_cache()
    return _back(ok=f"Đã mở PR {', '.join(opened)}; CI của từng PR đang chạy." if opened else "Mọi nhánh đều đã có PR.")


@router.post("/test-progress/candidates/merge")
async def merge_candidates(request: Request, user: str = Depends(require_login)):
    """Squash-merge the ticked PRs into main, in the order shown, stopping at the first failure."""
    _require_admin(user)
    form = await request.form()
    picked = [str(value) for value in form.getlist("pick")]
    if not picked:
        return _back(err="Chưa chọn PR nào.")
    if str(form.get("confirmation", "")).strip() != f"MERGE {len(picked)}":
        return _back(err=f"Gõ đúng MERGE {len(picked)} để xác nhận merge {len(picked)} PR vào main.")
    token = _token()
    if token is None:
        return _back(err="Cần GitHub token để merge.")
    result = await asyncio.to_thread(release_candidates.list_candidates, settings.ci_github_repo, token,
                                     Path(settings.release_notes_dir), use_cache=False)
    by_pick = {f"{c['pull']['number']}:{c['head_sha']}": c for c in result["candidates"] if c["pull"]}
    merged: list[str] = []
    try:
        for pick in picked:
            candidate = by_pick.get(pick)
            if candidate is None:
                raise release_candidates.ReleaseError(f"PR {pick.split(':')[0]} đã đổi hoặc không còn mở; tải lại trang.")
            if candidate["ci"].get("conclusion") != "success":
                raise release_candidates.ReleaseError(f"CI của PR #{candidate['pull']['number']} chưa xanh.")
            await asyncio.to_thread(
                release_candidates.merge_pull_request, settings.ci_github_repo, token,
                number=candidate["pull"]["number"], head_sha=candidate["head_sha"], branch=candidate["branch"],
                title=candidate["title"])
            merged.append(f"#{candidate['pull']['number']}")
    except release_candidates.ReleaseError as exc:
        done = f"Đã merge {', '.join(merged)}. " if merged else ""
        return _back(err=f"{done}Dừng lại: {exc}")
    finally:
        release_candidates.clear_cache()
        test_progress.clear_ci_cache()
    return _back(ok=f"Đã merge {', '.join(merged)} vào main; CI trên main đang build bản deploy.")
