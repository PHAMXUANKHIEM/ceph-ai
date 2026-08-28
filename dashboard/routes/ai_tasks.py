"""User-requested two-agent development tasks.

This is deliberately separate from the log-driven Code Repair supervisor:
an administrator submits an explicit prompt, then the same bounded planner /
implementer pipeline works on an isolated branch and reports its result.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.settings import settings
from dashboard.routes.auth import is_admin_user, require_login
from dashboard.templating import make_templates


router = APIRouter()
templates = make_templates()
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_ROOT = Path("/var/lib/ceph-ai/ai-tasks")
PROVIDERS = ("auto", "codex", "claude")
ACCOUNT_SOURCES = ("configured", "separate")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
TERMINAL_STATUSES = {"PUSHED", "COMMITTED", "STAGING_VERIFIED", "PROMOTED", "FAILED", "SKIPPED_DUPLICATE"}


def _require_admin(user: str) -> None:
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin mới được chạy AI Development Task")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_dir(task_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f-]{36}", task_id):
        raise HTTPException(status_code=404, detail="Task không tồn tại")
    return TASK_ROOT / task_id


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _task_view(task_id: str) -> dict:
    directory = _task_dir(task_id)
    metadata = _read_json(directory / "task.json")
    if not metadata:
        raise HTTPException(status_code=404, detail="Task không tồn tại")
    state = _read_json(directory / "state.json")
    attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
    latest = max(
        (item for item in attempts.values() if isinstance(item, dict)),
        key=lambda item: str(item.get("finished_at") or item.get("started_at") or ""),
        default=None,
    )
    view = {**metadata}
    if latest:
        view["status"] = latest.get("status", "RUNNING")
        view["result"] = latest
    else:
        view["status"] = metadata.get("status", "QUEUED")
        view["result"] = None
    return view


def _list_tasks() -> list[dict]:
    rows = []
    if not TASK_ROOT.is_dir():
        return rows
    for path in TASK_ROOT.glob("*/task.json"):
        task_id = path.parent.name
        try:
            rows.append(_task_view(task_id))
        except HTTPException:
            continue
    return sorted(rows, key=lambda item: str(item.get("created_at", "")), reverse=True)[:30]


def _profile_value(source: str, profile: str) -> str:
    if source == "configured":
        return "configured"
    value = profile.strip()
    if not PROFILE_RE.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail="Tên account profile chỉ được chứa chữ, số, _ hoặc - (tối đa 48 ký tự)",
        )
    return value


def _validate_model(value: str, field: str) -> str:
    value = value.strip()
    if len(value) > 128 or "\n" in value or "\r" in value:
        raise HTTPException(status_code=400, detail=f"{field} không hợp lệ")
    return value


@router.get("/ai-tasks", response_class=HTMLResponse)
async def ai_tasks_page(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    return templates.TemplateResponse(
        request,
        "ai_tasks.html",
        {
            "user": user,
            "is_admin": True,
            "providers": PROVIDERS,
            "account_sources": ACCOUNT_SOURCES,
            "tasks": _list_tasks(),
            "configured_codex_home": settings.codex_home,
            "configured_claude_config_dir": settings.claude_config_dir,
        },
    )


@router.post("/ai-tasks", response_class=HTMLResponse)
async def create_ai_task(
    request: Request,
    prompt: str = Form(""),
    planner_provider: str = Form("auto"),
    planner_model: str = Form(""),
    planner_account_source: str = Form("configured"),
    planner_account_profile: str = Form(""),
    implementer_provider: str = Form("auto"),
    implementer_model: str = Form(""),
    implementer_account_source: str = Form("configured"),
    implementer_account_profile: str = Form(""),
    max_review_rounds: str = Form("2"),
    push_branch: bool = Form(False),
    user: str = Depends(require_login),
):
    _require_admin(user)
    prompt = prompt.strip()
    if not prompt or len(prompt) > 50_000:
        raise HTTPException(status_code=400, detail="Yêu cầu phải có từ 1 đến 50.000 ký tự")
    if planner_provider not in PROVIDERS or implementer_provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Provider không hợp lệ")
    if planner_account_source not in ACCOUNT_SOURCES or implementer_account_source not in ACCOUNT_SOURCES:
        raise HTTPException(status_code=400, detail="Nguồn tài khoản không hợp lệ")
    try:
        rounds = int(max_review_rounds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Số vòng review phải là số nguyên từ 0 đến 5") from exc
    if not 0 <= rounds <= 5:
        raise HTTPException(status_code=400, detail="Số vòng review phải nằm trong khoảng 0 đến 5")
    planner_profile = _profile_value(planner_account_source, planner_account_profile)
    implementer_profile = _profile_value(implementer_account_source, implementer_account_profile)
    planner_model = _validate_model(planner_model, "Planner model")
    implementer_model = _validate_model(implementer_model, "Implementer model")

    task_id = str(uuid.uuid4())
    directory = TASK_ROOT / task_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    prompt_path = directory / "prompt.txt"
    instructions_path = directory / "instructions.txt"
    state_path = directory / "state.json"
    log_path = directory / "worker.log"
    prompt_path.write_text(prompt)
    instructions_path.write_text(
        """You are implementing an explicit product-development request for the ceph-ai repository.

Work in the isolated repair worktree. Make a production-quality implementation, add regression tests,
and keep the change scoped to the user's request. Do not edit credentials, .env files, deployment
scripts, migrations, or generated assets unless the request explicitly requires it. Do not weaken tests.
The Planner/Reviewer output is advisory; verify it against the source and tests before editing.
"""
    )
    for path in (prompt_path, instructions_path):
        os.chmod(path, 0o600)
    metadata = {
        "task_id": task_id,
        "status": "QUEUED",
        "created_at": _utc_now(),
        "created_by": user,
        "prompt": prompt,
        "planner_provider": planner_provider,
        "planner_model": planner_model,
        "planner_account_source": planner_account_source,
        "planner_account_profile": planner_profile,
        "implementer_provider": implementer_provider,
        "implementer_model": implementer_model,
        "implementer_account_source": implementer_account_source,
        "implementer_account_profile": implementer_profile,
        "max_review_rounds": rounds,
        "push_branch": bool(push_branch),
    }
    _write_json(state_path, {"task_id": task_id, "attempts": {}})
    _write_json(directory / "task.json", metadata)
    command = [
        sys.executable, "-m", "worker.code_repair",
        "--repo", str(PROJECT_ROOT),
        "--evidence-file", str(prompt_path),
        "--instructions-file", str(instructions_path),
        "--task-kind", "user-request",
        "--state-file", str(state_path),
        "--planner-provider", planner_provider,
        "--planner-model", planner_model,
        "--planner-account-profile", planner_profile,
        "--implementer-provider", implementer_provider,
        "--implementer-model", implementer_model,
        "--implementer-account-profile", implementer_profile,
        "--max-review-rounds", str(rounds),
        "--force",
    ]
    if push_branch:
        command.append("--push")
    try:
        with log_path.open("a") as log_file:
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        metadata.update({"status": "RUNNING", "pid": process.pid})
        _write_json(directory / "task.json", metadata)
    except Exception as exc:
        metadata.update({"status": "FAILED", "error": str(exc), "finished_at": _utc_now()})
        _write_json(directory / "task.json", metadata)
        raise HTTPException(status_code=500, detail="Không khởi động được AI task") from exc
    return RedirectResponse(f"/ai-tasks/{task_id}", status_code=303)


@router.get("/ai-tasks/{task_id}", response_class=HTMLResponse)
async def ai_task_detail(request: Request, task_id: str, user: str = Depends(require_login)):
    _require_admin(user)
    task = _task_view(task_id)
    return templates.TemplateResponse(
        request,
        "ai_task_detail.html",
        {"user": user, "is_admin": True, "task": task},
    )


@router.get("/ai-tasks/{task_id}/status")
async def ai_task_status(task_id: str, user: str = Depends(require_login)):
    _require_admin(user)
    task = _task_view(task_id)
    result = task.get("result") or {}
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "branch": result.get("branch"),
        "commit": result.get("commit"),
        "planner_provider": result.get("planner_provider") or task.get("planner_provider"),
        "implementer_provider": result.get("implementer_provider") or task.get("implementer_provider"),
        "review_rounds": result.get("review_rounds", 0),
        "changed_files": result.get("changed_files") or [],
        "error": result.get("error") or task.get("error"),
        "test_output": result.get("test_output") or "",
    }
