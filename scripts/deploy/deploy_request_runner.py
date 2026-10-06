#!/usr/bin/python3.11
"""Run a deploy an admin requested from the Dashboard (operator decision 2026-10-06).

The Dashboard writes /var/lib/ceph-ai/deploy-requests/pending.json after the
admin confirms "DEPLOY <sha8>"; ceph-ai-deploy-request.path starts this
script on the host. It trusts nothing in the request except the commit:

1. the request is consumed first (one request is handled once);
2. CI/CD for exactly that commit must have completed successfully;
3. the commit must be a descendant of what runs now (never a rollback or a
   side branch), the /root/ceph-ai checkout must be clean, and the image
   must be in GHCR;
4. then the documented immutable deploy runs (deploy_preflight.sh, then
   restart_container_stack.sh), and status.json reports the outcome, which
   the Dashboard shows. Telegram gets start/success/failure.

Standard library only.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess  # nosec B404
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.selfcheck.ceph_ai_selfcheck import read_env, send_telegram  # noqa: E402

REPO_DIR = Path(os.environ.get("CEPH_AI_DEPLOY_REPO", "/root/ceph-ai"))
REQUEST_DIR = Path(os.environ.get("CEPH_AI_DEPLOY_REQUEST_DIR", "/var/lib/ceph-ai/deploy-requests"))
ARTIFACTS = Path("/var/lib/ceph-ai/release-artifacts")
GITHUB_REPO = "PHAMXUANKHIEM/ceph-ai"
IMAGE_REPO = "phamxuankhiem/ceph-ai"
WORKFLOW_PATH = ".github/workflows/ci-cd.yml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STEP_TIMEOUT_SECONDS = 3600


class Refused(Exception):
    """The request fails a check; the message is shown to the admin."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(state: str, sha: str, message: str, **extra: object) -> None:
    status = {"state": state, "sha": sha, "message": message, "updated_at": _now(), **extra}
    temporary = REQUEST_DIR / ".status.json.tmp"
    temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, REQUEST_DIR / "status.json")


def _run(argv: list[str], *, timeout: int = 120, env: dict[str, str] | None = None,
         stdout=subprocess.PIPE) -> subprocess.CompletedProcess:
    # Absolute binaries, no shell; every argument is built here or validated.
    return subprocess.run(  # nosec B603
        argv, stdout=stdout, stderr=subprocess.STDOUT, text=True, timeout=timeout,
        check=False, cwd=REPO_DIR, env=env,
    )


def _https(method: str, host: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict, dict]:
    connection = http.client.HTTPSConnection(host, timeout=20)
    try:
        connection.request(method, path, headers={"User-Agent": "ceph-ai-deploy-request", **(headers or {})})
        response = connection.getresponse()
        body = response.read()
        payload = json.loads(body) if body and response.status == 200 and method == "GET" else {}
        return response.status, payload, {key.lower(): value for key, value in response.getheaders()}
    finally:
        connection.close()


def take_request() -> dict:
    """Read and remove pending.json, so a request is never handled twice."""
    path = REQUEST_DIR / "pending.json"
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        path.unlink(missing_ok=True)
        raise Refused(f"yêu cầu không đọc được ({type(exc).__name__})") from exc
    path.unlink(missing_ok=True)
    if not isinstance(request, dict) or not SHA_RE.match(str(request.get("sha", ""))):
        raise Refused("yêu cầu không có commit hợp lệ")
    return request


def ci_green(sha: str) -> None:
    status, payload, _ = _https("GET", "api.github.com", f"/repos/{GITHUB_REPO}/actions/runs?head_sha={sha}&per_page=10",
                                {"Accept": "application/vnd.github+json"})
    if status != 200:
        raise Refused(f"không đọc được CI trên GitHub (HTTP {status})")
    runs = [run for run in payload.get("workflow_runs") or [] if run.get("path") == WORKFLOW_PATH]
    if not runs or runs[0].get("status") != "completed" or runs[0].get("conclusion") != "success":
        raise Refused(f"CI của {sha[:8]} chưa xanh")


def running_revision() -> str | None:
    try:
        image = (ARTIFACTS / "current-image-ref").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    result = _run(["/usr/bin/podman", "image", "inspect", image,
                   "--format", '{{ index .Labels "org.opencontainers.image.revision" }}'])
    revision = result.stdout.strip() if result.returncode == 0 else ""
    return revision if SHA_RE.match(revision) else None


def checkout_ready(sha: str, running: str | None) -> None:
    _run(["/usr/bin/git", "fetch", "-q", "origin", "main"], timeout=120)
    if _run(["/usr/bin/git", "cat-file", "-e", f"{sha}^{{commit}}"]).returncode != 0:
        raise Refused(f"commit {sha[:8]} chưa có trong checkout")
    if running == sha:
        raise Refused(f"{sha[:8]} đang chạy rồi")
    if running and _run(["/usr/bin/git", "merge-base", "--is-ancestor", running, sha]).returncode != 0:
        raise Refused(f"{sha[:8]} không mới hơn bản đang chạy {running[:8]} (không lùi bản)")
    if _run(["/usr/bin/git", "status", "--porcelain"]).stdout.strip():
        raise Refused("checkout /root/ceph-ai có thay đổi chưa commit")


def registry_digest(sha: str) -> str:
    status, token_payload, _ = _https("GET", "ghcr.io", f"/token?scope=repository:{IMAGE_REPO}:pull")
    token = token_payload.get("token") if status == 200 else None
    if token:
        status, _, headers = _https("HEAD", "ghcr.io", f"/v2/{IMAGE_REPO}/manifests/{sha}", {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, "
                      "application/vnd.docker.distribution.manifest.v2+json",
        })
        digest = headers.get("docker-content-digest", "") if status == 200 else ""
        if DIGEST_RE.match(digest):
            return digest
    raise Refused(f"image {sha[:8]} chưa có trên GHCR")


def deploy(sha: str, digest: str, log_path: Path) -> bool:
    env = {**os.environ, "CEPH_AI_IMAGE": f"ghcr.io/{IMAGE_REPO}@{digest}", "DEPLOY_REF": sha,
           "CEPH_AI_DEPLOY_EVENT_LOG": str(ARTIFACTS / "deploy-phases.log")}
    with log_path.open("a", encoding="utf-8") as log:
        for script in ("scripts/deploy/deploy_preflight.sh", "scripts/deploy/restart_container_stack.sh"):
            try:
                if _run(["/usr/bin/bash", script], timeout=STEP_TIMEOUT_SECONDS, env=env, stdout=log).returncode != 0:
                    return False
            except subprocess.TimeoutExpired:
                return False
    return True


def main() -> int:
    if not (REQUEST_DIR / "pending.json").exists():
        return 0
    env = read_env()
    sha = ""
    try:
        request = take_request()
        sha, user = request["sha"], str(request.get("requested_by") or "?")
        write_status("checking", sha, f"kiểm tra yêu cầu của {user}")
        ci_green(sha)
        checkout_ready(sha, running_revision())
        digest = registry_digest(sha)
    except (Refused, OSError, ValueError) as exc:
        write_status("refused", sha, str(exc))
        send_telegram(env, f"⛔ Yêu cầu deploy {sha[:8] or '?'} bị từ chối: {exc}")
        return 0
    log_path = ARTIFACTS / f"deploy-{sha[:8]}-dashboard.log"
    write_status("running", sha, f"đang deploy theo yêu cầu của {user}", log=str(log_path))
    send_telegram(env, f"🚀 Deploy {sha[:8]} theo yêu cầu của {user} trên Dashboard")
    started = time.time()
    ok = deploy(sha, digest, log_path)
    minutes = (time.time() - started) / 60
    write_status("succeeded" if ok else "failed", sha, f"{minutes:.0f} phút", log=str(log_path))
    send_telegram(env, f"{'✅ Đã deploy' if ok else '❌ Deploy thất bại'} {sha[:8]} sau {minutes:.0f} phút"
                  + ("" if ok else f". Log: {log_path}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
