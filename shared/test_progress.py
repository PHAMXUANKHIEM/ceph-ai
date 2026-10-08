"""Read model for the Dashboard's /test-progress page.

Two sources, both read-only:

* the local pre-push test run, written by scripts/pytest_progress.py;
* the repository's GitHub Actions runs on main (public API, no token).
  Unauthenticated calls are limited to 60 per hour, so results are cached
  for CI_CACHE_SECONDS and only the newest run's jobs are fetched.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

DEFAULT_PROGRESS_FILE = Path("/var/lib/ceph-ai/test-runs/current.json")
GITHUB_API = "https://api.github.com"
CI_CACHE_SECONDS = 120
# A running file not updated for this long belongs to a killed run.
STALE_AFTER_SECONDS = 180
# The quality gate and budget write nothing while they work (up to 25 min each).
PREPARING_STALE_AFTER_SECONDS = 1800
_HTTP_TIMEOUT_SECONDS = 10

_ci_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_ci_lock = threading.Lock()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_local_run(path: Path = DEFAULT_PROGRESS_FILE, now: datetime | None = None) -> dict[str, Any] | None:
    """The latest local run with derived percent/ETA, or None if none ran."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    now = now or datetime.now(timezone.utc)
    started = _parse_time(state.get("started_at"))
    updated = _parse_time(state.get("updated_at"))
    finished = _parse_time(state.get("finished_at"))
    total = int(state.get("total") or 0)
    done = int(state.get("done") or 0)
    elapsed = ((finished or now) - started).total_seconds() if started else None
    state["percent"] = round(100 * done / total, 1) if total else 0.0
    state["elapsed_seconds"] = round(elapsed) if elapsed is not None else None
    state["eta_seconds"] = (
        round(elapsed / done * (total - done))
        if elapsed and done and total > done and state.get("status") == "running"
        else None
    )
    silent = (now - updated).total_seconds() if updated else 0
    if state.get("status") in {"running", "collecting"} and silent > STALE_AFTER_SECONDS:
        state["status"] = "stalled"
    elif state.get("status") == "preparing" and silent > PREPARING_STALE_AFTER_SECONDS:
        state["status"] = "stalled"
    return state


DEFAULT_DEPLOY_LOG = Path("/var/lib/ceph-ai/release-artifacts/deploy-phases.log")
# restart_container_stack.sh bounds each step; a deploy silent this long died.
DEPLOY_STALE_AFTER_SECONDS = 3600
_DEPLOY_LINE = re.compile(r"^(?P<at>\S+) phase=(?P<phase>[a-z_]+) status=(?P<status>[A-Z]+) detail=(?P<detail>.*)$")


def _deploy_events(path: Path) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-2000:]
    except OSError:
        return []
    return [match.groupdict() for match in map(_DEPLOY_LINE.match, lines) if match]


def _deploy_status(phases: list[dict[str, Any]], complete: dict[str, str] | None, last_at: datetime | None,
                   now: datetime) -> str:
    if complete is not None:
        return "passed" if complete["status"] == "PASSED" else "failed"
    if any(phase["status"] == "FAILED" for phase in phases):
        return "failed"
    if last_at and (now - last_at).total_seconds() > DEPLOY_STALE_AFTER_SECONDS:
        return "stalled"
    return "running"


def read_latest_deploy(path: Path = DEFAULT_DEPLOY_LOG, now: datetime | None = None) -> dict[str, Any] | None:
    """The newest deploy (from its preflight to complete) and the few before it.

    restart_container_stack.sh appends `<time> phase=<p> status=<s> detail=<d>`
    to the deploy event log; a deploy starts at `phase=preflight status=STARTED`.
    """
    events = _deploy_events(path)
    starts = [index for index, event in enumerate(events)
              if event["phase"] == "preflight" and event["status"] == "STARTED"]
    if not starts:
        return None
    now = now or datetime.now(timezone.utc)
    current = events[starts[-1]:]
    phases: dict[str, dict[str, Any]] = {}
    for event in current:
        if event["phase"] == "complete":
            continue
        entry = phases.setdefault(event["phase"], {"name": event["phase"], "started_at": event["at"]})
        entry["status"] = event["status"]
        entry["detail"] = event["detail"] or None
        if event["status"] != "STARTED":
            entry["finished_at"] = event["at"]
    complete = next((event for event in reversed(current) if event["phase"] == "complete"), None)
    ordered = list(phases.values())
    status = _deploy_status(ordered, complete, _parse_time(current[-1]["at"]), now)
    sha = (complete or {}).get("detail", "").removeprefix("sha=") or None
    completed = [{"at": event["at"], "status": event["status"], "sha": event["detail"].removeprefix("sha=")[:8]}
                 for event in events if event["phase"] == "complete"]
    history = (completed[:-1] if complete else completed)[-5:]  # the deploys before this one
    return {
        "status": status,
        "started_at": current[0]["at"],
        "finished_at": complete["at"] if complete else None,
        "sha": sha,
        "phases": ordered,
        "current": next((phase["name"] for phase in reversed(ordered) if phase["status"] == "STARTED"), None),
        "previous": list(reversed(history)),
    }


def _get_json(client: httpx.Client, url: str, token: str | None = None) -> Any:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def _summarise_run(run: dict) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "url": run.get("html_url"),
        "sha": str(run.get("head_sha") or "")[:8],
        "head_sha": str(run.get("head_sha") or ""),
        "title": str(run.get("display_title") or "")[:120],
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "event": run.get("event"),
        "branch": run.get("head_branch"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def find_dispatched_run(repo: str, ref: str, since: datetime, *, attempts: int = 6, wait_seconds: float = 2,
                        client: httpx.Client | None = None, sleep=time.sleep, token: str | None = None) -> str | None:
    """URL of the workflow_dispatch run GitHub creates a few seconds after a
    dispatch on ``ref`` (the dispatch API itself returns nothing)."""
    own_client = client is None
    http = client or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        for attempt in range(attempts):
            try:
                payload = _get_json(http, f"{GITHUB_API}/repos/{repo}/actions/runs?event=workflow_dispatch&per_page=10",
                                    token)
            except (httpx.HTTPError, ValueError):
                payload = {}
            for run in payload.get("workflow_runs", []):
                if run.get("head_branch") != ref:
                    continue
                created = _parse_time(run.get("created_at"))
                if created is not None and created >= since - timedelta(seconds=5):
                    return str(run.get("html_url") or "") or None
            if attempt + 1 < attempts:
                sleep(wait_seconds)
        return None
    finally:
        if own_client:
            http.close()


def clear_ci_cache() -> None:
    """Forget cached CI runs (after starting a run, show it on the next refresh)."""
    with _ci_lock:
        _ci_cache.clear()


# GitHub's `branch=` filter on the runs API returned week-old runs first on
# 07/10/2026 (the newest run on main was missing), and the page then offered
# a 2-day-old commit for deploy. Runs are listed unfiltered and narrowed here;
# the branch head comes from the branches API.
_BRANCH_EVENTS = ("push", "workflow_dispatch")


def fetch_ci_runs(repo: str, *, branch: str = "main", limit: int = 5, client: httpx.Client | None = None,
                  token: str | None = None) -> dict[str, Any]:
    """Recent CI runs on ``branch`` (newest first, the newest with its jobs) and the branch's head commit."""
    key = f"{repo}@{branch}:{limit}"
    with _ci_lock:
        cached = _ci_cache.get(key)
        if cached and time.monotonic() - cached[0] < CI_CACHE_SECONDS:
            return cached[1]
    own_client = client is None
    http = client or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        payload = _get_json(http, f"{GITHUB_API}/repos/{repo}/actions/runs?per_page=100", token)
        on_branch = [run for run in payload.get("workflow_runs", [])
                     if run.get("head_branch") == branch and run.get("event") in _BRANCH_EVENTS]
        on_branch.sort(key=lambda run: str(run.get("created_at") or ""), reverse=True)
        runs = [_summarise_run(run) for run in on_branch[:limit]]
        head = (_get_json(http, f"{GITHUB_API}/repos/{repo}/branches/{branch}", token).get("commit") or {}).get("sha")
        if runs:
            jobs = _get_json(http, f"{GITHUB_API}/repos/{repo}/actions/runs/{runs[0]['id']}/jobs", token)
            runs[0]["jobs"] = [
                {
                    "name": job.get("name"),
                    "status": job.get("status"),
                    "conclusion": job.get("conclusion"),
                    "started_at": job.get("started_at"),
                    "completed_at": job.get("completed_at"),
                }
                for job in jobs.get("jobs", [])
            ]
        result: dict[str, Any] = {"repo": repo, "branch": branch, "head_sha": head, "runs": runs, "error": None}
    except (httpx.HTTPError, ValueError) as exc:
        result = {"repo": repo, "branch": branch, "head_sha": None, "runs": [],
                  "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        if own_client:
            http.close()
    with _ci_lock:
        _ci_cache[key] = (time.monotonic(), result)
    return result
