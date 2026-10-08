"""Telegram approval of candidate pull requests (operator decision 08/10/2026).

The host notifier (scripts/deploy/release_notifier.py) posts each candidate
PR whose CI is green with a "Duyệt merge + deploy" button. When an operator
taps it, the Telegram gateway calls :func:`approve`, which re-checks the PR
(head still the announced commit, CI green on it, mergeable), squash-merges
it pinned to that commit and writes an approval record for the merge commit.
The notifier deploys main only when main's head carries such a record and its
CI is green. Nothing merges or deploys without that tap.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

GITHUB_API = "https://api.github.com"
WORKFLOW_PATH = ".github/workflows/ci-cd.yml"
APPROVE_PREFIX = "relapprove:"
SKIP_PREFIX = "relskip:"
_DATA_RE = re.compile(r"^(\d{1,7}):([0-9a-f]{7,40})$")


class ApprovalError(RuntimeError):
    """Shown to the operator in Telegram as is."""


def callback_data(prefix: str, number: int, head_sha: str) -> str:
    return f"{prefix}{number}:{head_sha[:12]}"


def parse(data: str, prefix: str) -> tuple[int, str]:
    match = _DATA_RE.match(data[len(prefix):])
    if not match:
        raise ApprovalError("Nút duyệt không hợp lệ.")
    return int(match.group(1)), match.group(2)


def approvals_dir(deploy_request_dir: str) -> Path:
    return Path(deploy_request_dir) / "approved"


def _headers(token: str) -> dict[str, str]:
    return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28"}


def _check(response: httpx.Response, what: str) -> dict:
    if response.status_code >= 400:
        raise ApprovalError(f"GitHub từ chối {what} (HTTP {response.status_code}).")
    return response.json() if response.content else {}


def approve(repo: str, token: str, *, number: int, short_sha: str, actor: str, deploy_request_dir: str,
            client: httpx.Client | None = None) -> dict:
    """Merge PR ``number`` if its head still starts with ``short_sha`` and is green."""
    http = client or httpx.Client(timeout=20)
    try:
        pull = _check(http.get(f"{GITHUB_API}/repos/{repo}/pulls/{number}", headers=_headers(token)), "đọc PR")
        if pull.get("merged"):
            raise ApprovalError(f"PR #{number} đã được merge rồi.")
        head = str(pull.get("head", {}).get("sha") or "")
        if pull.get("state") != "open" or not head.startswith(short_sha):
            raise ApprovalError(f"PR #{number} đã đổi kể từ lúc gửi duyệt; chờ thẻ mới.")
        if not str(pull.get("head", {}).get("ref") or "").startswith("cand/"):
            raise ApprovalError("Chỉ duyệt được PR từ nhánh cand/*.")
        runs = _check(http.get(f"{GITHUB_API}/repos/{repo}/actions/runs",
                               params={"head_sha": head, "event": "pull_request", "per_page": 5},
                               headers=_headers(token)), "đọc CI").get("workflow_runs") or []
        runs = [run for run in runs if run.get("path") == WORKFLOW_PATH]
        if not runs or runs[0].get("status") != "completed" or runs[0].get("conclusion") != "success":
            raise ApprovalError(f"CI của PR #{number} chưa xanh.")
        if pull.get("mergeable") is False:
            raise ApprovalError(f"PR #{number} xung đột với main; cần rebase.")
        merged = _check(http.put(f"{GITHUB_API}/repos/{repo}/pulls/{number}/merge", headers=_headers(token), json={
            "merge_method": "squash", "sha": head, "commit_title": f"{str(pull.get('title'))[:180]} (#{number})"}),
            "merge")
        http.delete(f"{GITHUB_API}/repos/{repo}/git/refs/heads/{pull['head']['ref']}", headers=_headers(token))
    finally:
        if client is None:
            http.close()
    merge_sha = str(merged.get("sha") or "")
    record = {"pr": number, "title": pull.get("title"), "head": head, "merge_sha": merge_sha,
              "approved_by": actor, "approved_at": datetime.now(timezone.utc).isoformat()}
    directory = approvals_dir(deploy_request_dir)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{merge_sha}.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, directory / f"{merge_sha}.json")
    return record
