"""Dashboard buttons for CI and deploy (operator decision 2026-10-06).

* Run CI: a ``workflow_dispatch`` of the CI/CD workflow on GitHub, using a
  token with Actions: write that the admin saves once. The token lives in a
  0600 file and is never sent back to the browser.
* Deploy the newest green build: the Dashboard container cannot deploy the
  host, so it only writes a deploy request file. The host unit
  ``ceph-ai-deploy-request.path`` picks it up and
  ``scripts/deploy/deploy_request_runner.py`` re-checks everything (green CI
  for that exact commit, newer than what runs, clean checkout, image in
  GHCR) before running the documented immutable deploy.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"
_TOKEN_RE = re.compile(r"^(github_pat_[A-Za-z0-9_]{20,255}|gh[pousr]_[A-Za-z0-9]{20,255})$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
PENDING = "pending.json"
STATUS = "status.json"
_HTTP_TIMEOUT_SECONDS = 15


class CiControlError(ValueError):
    """A request the buttons refuse, with a message safe to show."""


# --- GitHub token -------------------------------------------------------------------

def save_token(path: Path, token: str) -> None:
    token = token.strip()
    if not _TOKEN_RE.match(token):
        raise CiControlError("Token GitHub không đúng định dạng (github_pat_… hoặc ghp_…).")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.replace(temporary, path)


def read_token(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if _TOKEN_RE.match(token) else None


def dispatch_ci(repo: str, workflow: str, ref: str, token: str | None, *,
                client: httpx.Client | None = None) -> None:
    """Start the CI/CD workflow on ``ref`` (GitHub answers 204 on success)."""
    if not token:
        raise CiControlError("Chưa lưu GitHub token có quyền Actions: write.")
    if not _REF_RE.match(ref) or ".." in ref:
        raise CiControlError("Tên nhánh không hợp lệ.")
    own = client is None
    http = client or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        response = http.post(
            f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow}/dispatches",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"ref": ref},
        )
    except httpx.HTTPError as exc:
        raise CiControlError(f"Không gọi được GitHub ({type(exc).__name__}).") from exc
    finally:
        if own:
            http.close()
    if response.status_code != 204:
        raise CiControlError(f"GitHub từ chối chạy CI (HTTP {response.status_code}). Kiểm tra quyền của token.")


# --- deploy requests ----------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_deploy(directory: Path, *, sha: str, confirmation: str, user: str) -> dict[str, Any]:
    """Queue a deploy of ``sha`` for the host runner; one request at a time."""
    if not _SHA_RE.match(sha):
        raise CiControlError("Commit không hợp lệ.")
    if confirmation.strip() != f"DEPLOY {sha[:8]}":
        raise CiControlError(f"Gõ đúng 'DEPLOY {sha[:8]}' để xác nhận.")
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / PENDING).exists():
        raise CiControlError("Đang có một yêu cầu deploy chờ xử lý.")
    status = read_deploy_status(directory)
    if status and status.get("state") == "running":
        raise CiControlError("Một lượt deploy đang chạy.")
    request = {"sha": sha, "requested_by": user, "requested_at": _now()}
    temporary = directory / f".{PENDING}.tmp"
    temporary.write_text(json.dumps(request), encoding="utf-8")
    os.replace(temporary, directory / PENDING)
    return request


def read_deploy_status(directory: Path) -> dict[str, Any] | None:
    """The runner's last report, plus whether a request is still queued."""
    status: dict[str, Any] | None = None
    try:
        loaded = json.loads((directory / STATUS).read_text(encoding="utf-8"))
        status = loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        status = None
    if (directory / PENDING).exists():
        try:
            pending = json.loads((directory / PENDING).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending = {}
        status = {**(status or {}), "queued": pending if isinstance(pending, dict) else {}}
    return status
