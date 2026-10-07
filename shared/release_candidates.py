"""Candidate changes waiting for the operator (PR release flow, 07/10/2026).

Every change is pushed as its own ``cand/*`` branch cut from main. This
module turns those branches into the "Ứng viên" list on /test-progress:

* each branch gets a Pull Request against main (opened here, so its CI runs);
* each head commit gets a short AI summary of what it adds or changes in
  Ceph AI, written once per commit and cached on disk;
* the operator ticks the PRs to ship; they are squash-merged one by one into
  main (CI on main then builds the deployable image) and their branches are
  deleted.

GitHub writes need the dashboard token with Contents, Pull requests and
Actions read/write on the repository. Reads also work without it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
BRANCH_PREFIX = "cand/"
CACHE_SECONDS = 120
SUMMARY_DIFF_CHARS = 12000
_HTTP_TIMEOUT_SECONDS = 15
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()
_summarizing: set[str] = set()
_summarizing_lock = threading.Lock()


class ReleaseError(RuntimeError):
    """A GitHub call the operator asked for did not succeed; the text is shown as is."""


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _call(http: httpx.Client, method: str, url: str, token: str | None, **kwargs: Any) -> Any:
    response = http.request(method, url, headers=_headers(token), **kwargs)
    if response.status_code == 403 and token:
        raise ReleaseError("GitHub từ chối (403): token cần quyền Contents, Pull requests và Actions "
                           "(Read and write) cho repo này.")
    if response.status_code >= 400:
        message = ""
        try:
            message = str(response.json().get("message") or "")
        except ValueError:
            pass
        raise ReleaseError(f"GitHub trả HTTP {response.status_code}: {message[:200]}")
    return response.json() if response.content else None


def _client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    return (client, False) if client is not None else (httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS), True)


# --- reading ------------------------------------------------------------------------

def _ci_state(http: httpx.Client, repo: str, sha: str, token: str | None) -> dict[str, Any]:
    payload = _call(http, "GET", f"{GITHUB_API}/repos/{repo}/actions/runs?head_sha={sha}&per_page=5", token)
    runs = (payload or {}).get("workflow_runs") or []
    if not runs:
        return {"status": "none", "conclusion": None, "url": None}
    newest = runs[0]
    return {"status": newest.get("status"), "conclusion": newest.get("conclusion"), "url": newest.get("html_url")}


def _candidate(http: httpx.Client, repo: str, branch: dict, pulls: dict[str, dict], token: str | None,
               notes_dir: Path) -> dict[str, Any]:
    name = branch["name"]
    compare = _call(http, "GET", f"{GITHUB_API}/repos/{repo}/compare/main...{name}", token) or {}
    commits = compare.get("commits") or []
    head = branch["commit"]["sha"]
    pull = pulls.get(name)
    detail = _call(http, "GET", f"{GITHUB_API}/repos/{repo}/pulls/{pull['number']}", token) if pull else None
    return {
        "branch": name,
        "head_sha": head,
        "title": (commits[-1]["commit"]["message"].splitlines()[0] if commits else name),
        "commits": [{"sha": item["sha"], "message": item["commit"]["message"],
                     "date": item["commit"]["committer"]["date"]} for item in commits],
        "files": [{"name": item.get("filename"), "status": item.get("status"),
                   "additions": item.get("additions", 0), "deletions": item.get("deletions", 0)}
                  for item in compare.get("files") or []],
        "ahead_by": compare.get("ahead_by", 0),
        "behind_by": compare.get("behind_by", 0),
        "pull": ({"number": pull["number"], "url": pull["html_url"],
                  "mergeable": (detail or {}).get("mergeable"),
                  "mergeable_state": (detail or {}).get("mergeable_state")} if pull else None),
        "ci": _ci_state(http, repo, head, token),
        "summary": read_summary(notes_dir, head),
        "updated_at": commits[-1]["commit"]["committer"]["date"] if commits else None,
    }


def list_candidates(repo: str, token: str | None, notes_dir: Path, *, client: httpx.Client | None = None,
                    use_cache: bool = True) -> dict[str, Any]:
    """Every ``cand/*`` branch ahead of main, newest first, with PR, CI and summary."""
    key = f"{repo}:{bool(token)}"
    with _cache_lock:
        cached = _cache.get(key)
        if use_cache and cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return _with_fresh_summaries(cached[1], notes_dir)
    http, own = _client(client)
    try:
        branches = _call(http, "GET", f"{GITHUB_API}/repos/{repo}/branches?per_page=100", token) or []
        pulls = {pull["head"]["ref"]: pull for pull in
                 _call(http, "GET", f"{GITHUB_API}/repos/{repo}/pulls?state=open&base=main&per_page=100", token) or []}
        items = [_candidate(http, repo, branch, pulls, token, notes_dir) for branch in branches
                 if branch["name"].startswith(BRANCH_PREFIX)]
        result: dict[str, Any] = {
            "candidates": sorted((item for item in items if item["ahead_by"] > 0),
                                 key=lambda item: item["updated_at"] or "", reverse=True),
            "error": None,
        }
    except (httpx.HTTPError, ValueError, KeyError, ReleaseError) as exc:
        result = {"candidates": [], "error": str(exc)[:300]}
    finally:
        if own:
            http.close()
    with _cache_lock:
        _cache[key] = (time.monotonic(), result)
    return result


def _with_fresh_summaries(result: dict[str, Any], notes_dir: Path) -> dict[str, Any]:
    for item in result.get("candidates", []):
        item["summary"] = read_summary(notes_dir, item["head_sha"])
    return result


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --- AI summaries ---------------------------------------------------------------------

def _note_path(notes_dir: Path, sha: str) -> Path:
    if not _SHA_RE.match(sha):
        raise ValueError("invalid commit sha")
    return notes_dir / f"{sha}.json"


def read_summary(notes_dir: Path, sha: str) -> dict[str, Any] | None:
    try:
        return json.loads(_note_path(notes_dir, sha).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summary_prompt(candidate: dict[str, Any], patch: str) -> str:
    """Ask for an operator-facing summary; the diff is redacted before it is sent."""
    from shared.ai_redaction import redact_text

    messages = redact_text("\n\n".join(commit["message"] for commit in candidate["commits"]))
    files = "\n".join(f"- {item['name']} (+{item['additions']}/-{item['deletions']})" for item in candidate["files"])
    return (
        "Bạn tóm tắt một thay đổi mã nguồn của Ceph AI (công cụ giám sát và tự xử lý sự cố cho cụm Ceph) "
        "cho người vận hành sắp quyết định có đưa nó vào CI và deploy hay không. Viết tiếng Việt, "
        "4–7 gạch đầu dòng ngắn, không markdown đậm: (1) thêm hoặc sửa tính năng gì, người dùng thấy gì khác; "
        "(2) phần nào của tool bị ảnh hưởng; (3) rủi ro hoặc điều cần kiểm tra; (4) việc cần làm sau deploy "
        "(cấu hình .env, bật cờ...) nếu có. Chỉ dựa vào dữ liệu dưới đây; không bịa.\n\n"
        f"Nhánh: {candidate['branch']}\n\nCommit message:\n{messages}\n\n"
        f"File thay đổi:\n{files}\n\nDiff (có thể bị cắt):\n{redact_text(patch[:SUMMARY_DIFF_CHARS])}"
    )


def fetch_patch(repo: str, branch: str, token: str | None, *, client: httpx.Client | None = None) -> str:
    http, own = _client(client)
    try:
        response = http.get(f"{GITHUB_API}/repos/{repo}/compare/main...{branch}",
                            headers={**_headers(token), "Accept": "application/vnd.github.diff"})
        response.raise_for_status()
        return response.text
    finally:
        if own:
            http.close()


def fallback_summary(candidate: dict[str, Any]) -> str:
    """Commit bodies, when the AI cannot be reached."""
    return "\n\n".join(commit["message"] for commit in candidate["commits"])[:3000]


def write_summary(notes_dir: Path, sha: str, text: str, *, source: str) -> dict[str, Any]:
    note = {"sha": sha, "text": text.strip(), "source": source,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = _note_path(notes_dir, sha)
    notes_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(note, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return note


def claim_summary(sha: str) -> bool:
    """One summary job per commit at a time."""
    with _summarizing_lock:
        if sha in _summarizing:
            return False
        _summarizing.add(sha)
        return True


def release_summary(sha: str) -> None:
    with _summarizing_lock:
        _summarizing.discard(sha)


# --- writing ------------------------------------------------------------------------

def open_pull_request(repo: str, token: str, candidate: dict[str, Any], *,
                      client: httpx.Client | None = None) -> dict[str, Any]:
    summary = (candidate.get("summary") or {}).get("text") or fallback_summary(candidate)
    http, own = _client(client)
    try:
        return _call(http, "POST", f"{GITHUB_API}/repos/{repo}/pulls", token, json={
            "title": candidate["title"][:200], "head": candidate["branch"], "base": "main",
            "body": f"{summary}\n\n_Mở tự động từ Ceph AI Dashboard (Tiến độ test) cho nhánh `{candidate['branch']}`._",
        })
    finally:
        if own:
            http.close()


def merge_pull_request(repo: str, token: str, *, number: int, head_sha: str, branch: str, title: str,
                       client: httpx.Client | None = None) -> dict[str, Any]:
    """Squash-merge one PR, only if its head is still the commit the operator saw."""
    if not branch.startswith(BRANCH_PREFIX) or not _SHA_RE.match(head_sha):
        raise ReleaseError("Chỉ merge được nhánh cand/* với commit cụ thể.")
    http, own = _client(client)
    try:
        result = _call(http, "PUT", f"{GITHUB_API}/repos/{repo}/pulls/{number}/merge", token, json={
            "merge_method": "squash", "sha": head_sha, "commit_title": f"{title[:180]} (#{number})",
        })
        try:
            _call(http, "DELETE", f"{GITHUB_API}/repos/{repo}/git/refs/heads/{branch}", token)
        except ReleaseError:
            logger.warning("release: merged #%s but could not delete %s", number, branch)
        return {"number": number, "merged_sha": (result or {}).get("sha")}
    finally:
        if own:
            http.close()
