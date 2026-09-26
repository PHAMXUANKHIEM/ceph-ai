#!/usr/bin/env python3
"""Post-deploy smoke test, run on the target host after the stack restarts.

Plan 3.5. Checks, each reported separately in a secret-free JSON report:

* ``login_page``        GET /login answers 200;
* ``heartbeats``        /api/system/health reports Watcher and Worker healthy
                        (retried until ``--wait-seconds``: services just restarted);
* ``authenticated_health``  logs in with the smoke account (when configured)
                        and GET /api/dashboard/health answers 200;
* ``migration_head``    the database revision equals the Alembic head;
* ``release_sha``       every service container runs an image labelled with
                        the deployed source SHA.

A check that cannot run because it is not configured (the smoke account) is
``SKIPPED`` and does not fail the deploy, but is visible in the evidence.
Any ``FAILED`` check exits 1.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SERVICES = ("dashboard-web", "full-executor", "watcher", "worker", "telegram-ai")
REVISION_LABEL = "org.opencontainers.image.revision"
PASSED, FAILED, SKIPPED = "PASSED", "FAILED", "SKIPPED"


def _result(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, **extra}


class Http:
    """Minimal cookie-aware client; the dashboard runs on the same host."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path: str, *, data: dict[str, str] | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers or {})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:  # nosec B310
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def cookie(self, name: str) -> str | None:
        return next((item.value for item in self.cookies if item.name == name), None)


def check_login_page(client: Http) -> dict[str, Any]:
    try:
        status, _ = client.request("/login")
    except OSError as exc:
        return _result("login_page", FAILED, f"unreachable: {type(exc).__name__}")
    return _result("login_page", PASSED if status == 200 else FAILED, f"http={status}")


def check_heartbeats(client: Http, *, wait_seconds: float, sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    deadline = clock() + wait_seconds
    last = "no response"
    while True:
        try:
            status, body = client.request("/api/system/health")
            payload = json.loads(body)
            services = payload.get("services", {})
            unhealthy = sorted(name for name, value in services.items() if not value.get("healthy"))
            if status == 200 and payload.get("status") == "ok" and {"watcher", "worker"} <= set(services):
                return _result("heartbeats", PASSED, "watcher and worker healthy",
                               ages={name: value.get("age_seconds") for name, value in services.items()})
            last = f"http={status} unhealthy={','.join(unhealthy) or 'none'}"
        except (OSError, ValueError) as exc:
            last = f"{type(exc).__name__}"
        if clock() >= deadline:
            return _result("heartbeats", FAILED, last)
        sleep(5)


def load_smoke_credentials(path: Path) -> tuple[str, str] | None:
    """``username:password`` in a 0600 file owned by the deploying user."""
    if not path.is_file():
        return None
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} must not be readable by group/other")
    username, _, password = path.read_text(encoding="utf-8").strip().partition(":")
    return (username, password) if username and password else None


def check_authenticated_health(client: Http, credentials_file: Path) -> dict[str, Any]:
    try:
        credentials = load_smoke_credentials(credentials_file)
    except PermissionError as exc:
        return _result("authenticated_health", FAILED, str(exc))
    if credentials is None:
        return _result("authenticated_health", SKIPPED,
                       f"no smoke account configured at {credentials_file}")
    try:
        client.request("/login")
        token = client.cookie("ceph_ai_csrf") or ""
        status, _ = client.request(
            "/login",
            data={"username": credentials[0], "password": credentials[1], "_csrf_token": token},
            headers={"X-CSRF-Token": token, "Origin": client.base_url, "Referer": client.base_url + "/login"},
        )
        if status not in (200, 303):
            return _result("authenticated_health", FAILED, f"login http={status}")
        status, body = client.request("/api/dashboard/health")
    except OSError as exc:
        return _result("authenticated_health", FAILED, f"unreachable: {type(exc).__name__}")
    if status != 200:
        return _result("authenticated_health", FAILED, f"/api/dashboard/health http={status}")
    try:
        json.loads(body)
    except ValueError:
        return _result("authenticated_health", FAILED, "health response is not JSON (login may have failed)")
    return _result("authenticated_health", PASSED, "http=200")


def check_migration_head() -> dict[str, Any]:
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from sqlalchemy import create_engine

        from config.settings import settings

        heads = set(ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads())
        engine = create_engine(settings.database_url)
        try:
            with engine.connect() as connection:
                current = set(MigrationContext.configure(connection).get_current_heads())
        finally:
            engine.dispose()
    except Exception as exc:  # the check reports, it never raises
        return _result("migration_head", FAILED, f"{type(exc).__name__}")
    status = PASSED if current == heads and len(heads) == 1 else FAILED
    return _result("migration_head", status, f"database={sorted(current)} head={sorted(heads)}")


def _podman(*args: str) -> str:
    executable = shutil.which("podman")
    if executable is None:
        raise FileNotFoundError("podman is not installed on the target host")
    return subprocess.run(  # nosec B603
        [executable, *args], check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def check_release_sha(expected_sha: str, *, podman: Callable[..., str] = _podman) -> dict[str, Any]:
    if not expected_sha:
        return _result("release_sha", FAILED, "no expected SHA supplied")
    revisions: dict[str, str] = {}
    for service in SERVICES:
        container = f"ceph-ai_{service}_1"
        try:
            image_id = podman("inspect", container, "--format", "{{.Image}}")
            revisions[service] = podman(
                "image", "inspect", image_id, "--format", f'{{{{index .Config.Labels "{REVISION_LABEL}"}}}}',
            )
        except (OSError, subprocess.SubprocessError) as exc:
            revisions[service] = f"<{type(exc).__name__}>"
    wrong = sorted(name for name, revision in revisions.items() if revision != expected_sha)
    status = PASSED if not wrong else FAILED
    detail = "all services run the deployed SHA" if not wrong else f"unexpected revision: {', '.join(wrong)}"
    return _result("release_sha", status, detail, revisions=revisions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--expected-sha", default=os.environ.get("DEPLOY_REF", ""))
    parser.add_argument("--credentials-file", type=Path,
                        default=Path(os.environ.get("CEPH_AI_SMOKE_CREDENTIALS_FILE",
                                                    "/var/lib/ceph-ai/config/smoke-credentials")))
    parser.add_argument("--wait-seconds", type=float, default=180.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    client = Http(args.base_url)
    checks = [
        check_login_page(client),
        check_heartbeats(client, wait_seconds=args.wait_seconds),
        check_authenticated_health(Http(args.base_url), args.credentials_file),
        check_migration_head(),
        check_release_sha(args.expected_sha),
    ]
    failed = [item["check"] for item in checks if item["status"] == FAILED]
    report = {
        "schema": "ceph-ai.post-deploy-smoke.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expected_sha": args.expected_sha,
        "status": FAILED if failed else PASSED,
        "checks": checks,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        os.chmod(args.output, 0o640)
    for item in checks:
        print(f"SMOKE status={item['status']} check={item['check']} detail={item['detail']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
