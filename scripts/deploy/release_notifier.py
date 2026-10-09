#!/usr/bin/python3.11
"""Ask the operator on Telegram to approve each candidate change (08/10/2026).

Runs on the host every few minutes (ceph-ai-release-notifier.timer). It never
merges or deploys on its own:

1. every ``cand/*`` branch without a pull request gets one, so its CI runs;
2. a candidate PR whose CI is green on its head is posted once per head
   commit to the Telegram chat with "Duyệt merge + deploy" and "Bỏ qua"
   buttons, the changed files and a warning for sensitive paths. The tap is
   handled by the Telegram gateway (shared/release_approval.py), which merges
   the PR and records the approval;
3. when main's head is a merge an operator approved, its push CI is green, it
   is not what runs now, no deploy is queued or running and every container is
   healthy, a deploy request is queued for ceph-ai-deploy-request, which
   repeats its own checks (CI, never a rollback, image present).

Standard library only.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess  # nosec B404
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.deploy import deploy_request_runner as runner  # noqa: E402
from scripts.selfcheck.ceph_ai_selfcheck import read_env, send_telegram  # noqa: E402

GITHUB_REPO = runner.GITHUB_REPO
WORKFLOW_PATH = runner.WORKFLOW_PATH
CONFIG_DIR = Path("/var/lib/ceph-ai/config")
TOKEN_FILE = CONFIG_DIR / "github-ci-token"
STATE_FILE = CONFIG_DIR / "release-notifier-state.json"
BRANCH_PREFIX = "cand/"
SENSITIVE_PREFIXES = ("alembic/", "scripts/deploy/", "worker/policy/", ".github/", "Dockerfile", "Containerfile")
SENSITIVE_FILES = {"compose.yaml", "requirements.txt", "requirements-dev.txt", "requirements.lock",
                   "uv.lock", "pyproject.toml", "container-up", "container-down", "config/settings.py"}


class GitHubError(RuntimeError):
    pass


def _api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, object]:
    connection = http.client.HTTPSConnection("api.github.com", timeout=20)
    headers = {"User-Agent": "ceph-ai-release-notifier", "Accept": "application/vnd.github+json",
               "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    payload = json.dumps(body).encode() if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, f"/repos/{GITHUB_REPO}{path}", body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            return response.status, (json.loads(raw) if raw else None)
        except ValueError:
            return response.status, None
    finally:
        connection.close()


def _get(path: str, token: str):
    status, data = _api("GET", path, token)
    if status != 200:
        raise GitHubError(f"GET {path} -> HTTP {status}")
    return data


def _ci_state(sha: str, token: str, event: str) -> str:
    runs = [run for run in (_get(f"/actions/runs?head_sha={sha}&per_page=20", token) or {}).get("workflow_runs") or []
            if run.get("path") == WORKFLOW_PATH and run.get("event") == event]
    if not runs:
        return "none"
    if runs[0].get("status") != "completed":
        return "running"
    return "success" if runs[0].get("conclusion") == "success" else "failure"


def _load_state() -> dict:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(state: dict) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, STATE_FILE)


def sensitive_files(files: list[dict]) -> list[str]:
    names = [str(item.get("filename") or "") for item in files]
    return sorted({name for name in names if name in SENSITIVE_FILES or name.startswith(SENSITIVE_PREFIXES)})


def send_approval_card(env: dict[str, str], text: str, number: int, head: str) -> bool:
    """Post to the operators' chat with inline buttons (handled by the Telegram gateway)."""
    token, chat_id = env.get("TELEGRAM_CHATBOX_BOT_TOKEN", ""), env.get("TELEGRAM_CHATBOX_CHAT_ID", "")
    if not token or not chat_id:
        return False
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Duyệt merge + deploy", "callback_data": f"relapprove:{number}:{head[:12]}"},
        {"text": "⏭ Bỏ qua", "callback_data": f"relskip:{number}:{head[:12]}"},
    ]]}
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000],
                                   "reply_markup": json.dumps(keyboard)}).encode()
    connection = http.client.HTTPSConnection("api.telegram.org", timeout=15)
    try:
        connection.request("POST", f"/bot{token}/sendMessage", body=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        return connection.getresponse().status == 200
    except OSError:
        return False
    finally:
        connection.close()


def open_missing_pull_requests(token: str) -> list[int]:
    pulls = _get("/pulls?state=open&base=main&per_page=100", token) or []
    with_pr = {pull["head"]["ref"] for pull in pulls}
    opened = []
    for branch in _get("/branches?per_page=100", token) or []:
        name = branch.get("name", "")
        if not name.startswith(BRANCH_PREFIX) or name in with_pr:
            continue
        message = str(((_get(f"/commits/{branch['commit']['sha']}", token) or {}).get("commit") or {}).get("message")
                      or name)
        title, _, body = message.partition("\n")
        status, data = _api("POST", "/pulls", token, {
            "title": title[:200], "head": name, "base": "main",
            "body": (body.strip() or title) + "\n\n_Mở tự động; chờ operator duyệt trên Telegram._"})
        if status == 201 and isinstance(data, dict):
            opened.append(int(data["number"]))
    return opened


def announce_green_pull_requests(token: str, env: dict[str, str], state: dict) -> list[int]:
    announced = state.setdefault("announced", {})
    posted = []
    for pull in _get("/pulls?state=open&base=main&per_page=100", token) or []:
        number, head = pull["number"], pull["head"]["sha"]
        if not pull["head"]["ref"].startswith(BRANCH_PREFIX) or announced.get(str(number)) == head:
            continue
        if _ci_state(head, token, "pull_request") != "success":
            continue
        files = _get(f"/pulls/{number}/files?per_page=100", token) or []
        sensitive = sensitive_files(files)
        added = sum(int(item.get("additions") or 0) for item in files)
        removed = sum(int(item.get("deletions") or 0) for item in files)
        body = str(pull.get("body") or "").split("\n\n_Mở tự động")[0].strip()
        text = (f"🟢 PR #{number} đã qua CI, chờ duyệt\n{pull['title']}\n{pull['html_url']}\n\n"
                + (body[:900] + ("…" if len(body) > 900 else "") + "\n\n" if body else "")
                + f"{len(files)} file (+{added}/-{removed})"
                + (f"\n⚠️ Đụng phần nhạy cảm: {', '.join(sensitive[:8])} — xem kỹ trước khi duyệt." if sensitive else ""))
        if send_approval_card(env, text, number, head):
            announced[str(number)] = head
            posted.append(number)
    return posted


def _approval(merge_sha: str) -> dict | None:
    try:
        return json.loads((runner.REQUEST_DIR / "approved" / f"{merge_sha}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _containers_healthy() -> bool:
    result = runner._run(["/usr/bin/podman", "ps", "-a", "--format", "{{.Names}} {{.Status}}"])
    rows = [line for line in result.stdout.splitlines() if line.startswith("ceph-ai_") and "code-repair" not in line]
    return result.returncode == 0 and bool(rows) and all("(healthy)" in row for row in rows)


# How far back on main to look for an approved, green merge.
DEPLOY_LOOKBACK_COMMITS = 30


def _deployable_commit(token: str, running: str | None) -> str | None:
    """The newest operator-approved merge on main whose push CI is green, newer than ``running``.

    Not only main's head: CI on main takes ~41 minutes, and on 09/10/2026 six
    approved merges landed 3-20 minutes apart, so whenever one turned green the
    head had already moved to a commit still under test and nothing deployed
    for 1h45. Deploying the newest green approved merge ships everything up to
    it; the merges after it follow once their own CI is green.
    """
    for commit in _get(f"/commits?sha=main&per_page={DEPLOY_LOOKBACK_COMMITS}", token) or []:
        sha = str(commit.get("sha") or "")
        if not sha or sha == running:
            return None  # everything older is already running
        if _approval(sha) is not None and _ci_state(sha, token, "push") == "success":
            return sha
    return None


def deploy_approved_head(token: str, env: dict[str, str], state: dict) -> str | None:
    request_dir = runner.REQUEST_DIR
    if (request_dir / "pending.json").exists():
        return None
    try:
        status = json.loads((request_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    if status.get("state") in ("checking", "running"):
        return None
    head = _deployable_commit(token, runner.running_revision())
    if head is None or head in state.setdefault("deploy_requested", []):
        return None  # nothing approved and green beyond the running revision, or already requested once
    approval = _approval(head) or {}
    if not _containers_healthy():
        if state.get("unhealthy_noted") != head:
            send_telegram(env, f"⏸ Chưa deploy {head[:8]} (PR #{approval.get('pr')} đã duyệt): có container chưa healthy.")
            state["unhealthy_noted"] = head
        return None
    request = {"sha": head, "requested_by": f"telegram:{approval.get('approved_by')}",
               "requested_at": datetime.now(timezone.utc).isoformat()}
    temporary = request_dir / ".pending.json.tmp"
    temporary.write_text(json.dumps(request), encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, request_dir / "pending.json")
    state["deploy_requested"] = (state["deploy_requested"] + [head])[-50:]
    send_telegram(env, f"🚀 CI trên main xanh: deploy {head[:8]} (PR #{approval.get('pr')}, "
                       f"duyệt bởi {approval.get('approved_by')}).")
    return head


def main() -> int:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    env = read_env()
    state = _load_state()
    try:
        open_missing_pull_requests(token)
        announce_green_pull_requests(token, env, state)
        deploy_approved_head(token, env, state)
    except (GitHubError, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"release-notifier: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        _save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
