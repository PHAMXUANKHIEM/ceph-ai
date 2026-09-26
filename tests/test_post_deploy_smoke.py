import importlib.util
import json
import os
import sys
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "post_deploy_smoke.py"
spec = importlib.util.spec_from_file_location("post_deploy_smoke", MODULE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class FakeHttp:
    def __init__(self, responses, base_url="http://127.0.0.1:8000"):
        self.responses = responses
        self.base_url = base_url
        self.calls = []

    def request(self, path, *, data=None, headers=None):
        self.calls.append((path, data, headers))
        value = self.responses[path]
        return value.pop(0) if isinstance(value, list) else value

    def cookie(self, name):
        return "csrf-token" if name == "ceph_ai_csrf" else None


HEALTHY = (200, json.dumps({"status": "ok", "services": {
    "watcher": {"healthy": True, "age_seconds": 3}, "worker": {"healthy": True, "age_seconds": 4}}}))
DEGRADED = (503, json.dumps({"status": "degraded", "services": {
    "watcher": {"healthy": True}, "worker": {"healthy": False}}}))


def test_heartbeats_wait_for_services_that_just_restarted():
    client = FakeHttp({"/api/system/health": [DEGRADED, DEGRADED, HEALTHY]})
    ticks = iter([0, 1, 2, 3])
    result = module.check_heartbeats(client, wait_seconds=60, sleep=lambda _s: None, clock=lambda: next(ticks))
    assert result["status"] == "PASSED"
    assert result["ages"] == {"watcher": 3, "worker": 4}


def test_heartbeats_fail_with_the_unhealthy_service_after_the_deadline():
    client = FakeHttp({"/api/system/health": DEGRADED})
    ticks = iter([0, 100])
    result = module.check_heartbeats(client, wait_seconds=60, sleep=lambda _s: None, clock=lambda: next(ticks))
    assert result == {"check": "heartbeats", "status": "FAILED", "detail": "http=503 unhealthy=worker"}


def test_authenticated_health_is_skipped_without_a_smoke_account(tmp_path):
    result = module.check_authenticated_health(FakeHttp({}), tmp_path / "missing")
    assert result["status"] == "SKIPPED"


def test_authenticated_health_logs_in_with_csrf_and_reads_dashboard_health(tmp_path):
    credentials = tmp_path / "smoke-credentials"
    credentials.write_text("smoke:secret\n")
    os.chmod(credentials, 0o600)
    client = FakeHttp({"/login": [(200, "<form>"), (303, "")], "/api/dashboard/health": (200, "{}")})
    result = module.check_authenticated_health(client, credentials)
    assert result["status"] == "PASSED"
    _path, data, headers = client.calls[1]
    assert data == {"username": "smoke", "password": "secret", "_csrf_token": "csrf-token"}
    assert headers["X-CSRF-Token"] == "csrf-token"
    assert headers["Origin"] == "http://127.0.0.1:8000"


def test_authenticated_health_fails_when_login_is_rejected_or_file_is_exposed(tmp_path):
    credentials = tmp_path / "smoke-credentials"
    credentials.write_text("smoke:wrong\n")
    os.chmod(credentials, 0o600)
    client = FakeHttp({"/login": [(200, ""), (401, "")]})
    assert module.check_authenticated_health(client, credentials)["detail"] == "login http=401"
    os.chmod(credentials, 0o644)
    exposed = module.check_authenticated_health(FakeHttp({}), credentials)
    assert exposed["status"] == "FAILED" and "group/other" in exposed["detail"]


def test_release_sha_requires_every_service_to_run_the_deployed_revision():
    labels = {"img-ok": "abc123", "img-old": "def456"}

    def podman(*args):
        if args[0] == "inspect":
            return "img-old" if args[1] == "ceph-ai_worker_1" else "img-ok"
        return labels[args[2]]

    result = module.check_release_sha("abc123", podman=podman)
    assert result["status"] == "FAILED"
    assert result["detail"] == "unexpected revision: worker"
    assert result["revisions"]["dashboard-web"] == "abc123"
    labels["img-old"] = "abc123"
    assert module.check_release_sha("abc123", podman=podman)["status"] == "PASSED"
    assert module.check_release_sha("", podman=podman)["status"] == "FAILED"


def test_main_writes_report_and_fails_on_any_failed_check(tmp_path, monkeypatch):
    passed = lambda name: {"check": name, "status": "PASSED", "detail": "ok"}  # noqa: E731
    monkeypatch.setattr(module, "check_login_page", lambda _c: passed("login_page"))
    monkeypatch.setattr(module, "check_heartbeats", lambda _c, **_k: passed("heartbeats"))
    monkeypatch.setattr(module, "check_authenticated_health", lambda _c, _p: {"check": "authenticated_health", "status": "SKIPPED", "detail": "x"})
    monkeypatch.setattr(module, "check_migration_head", lambda: passed("migration_head"))
    monkeypatch.setattr(module, "check_release_sha", lambda _s: {"check": "release_sha", "status": "FAILED", "detail": "old"})
    output = tmp_path / "smoke.json"
    assert module.main(["--expected-sha", "abc", "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["status"] == "FAILED"
    assert [item["status"] for item in report["checks"]] == ["PASSED", "PASSED", "SKIPPED", "PASSED", "FAILED"]
    monkeypatch.setattr(module, "check_release_sha", lambda _s: passed("release_sha"))
    assert module.main(["--expected-sha", "abc", "--output", str(output)]) == 0


def test_deploy_script_runs_the_full_smoke_on_the_target_host():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/deploy/restart_container_stack.sh").read_text(encoding="utf-8")
    smoke = script.index("start_phase smoke")
    assert script.index('scripts/deploy/post_deploy_smoke.py"', smoke) < script.index("finish_phase", smoke)
    assert '--expected-sha "$(git rev-parse HEAD)"' in script
