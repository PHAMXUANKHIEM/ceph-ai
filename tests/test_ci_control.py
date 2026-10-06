"""Dashboard CI / deploy buttons (shared/ci_control.py, routes, host runner)."""

import json
import stat
from pathlib import Path

import httpx
import pytest

from config.settings import settings
from dashboard.routes import test_progress as route
from scripts.deploy import deploy_request_runner as runner
from shared import ci_control

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
TOKEN = "github_pat_" + "A1" * 20


# --- token and dispatch -------------------------------------------------------------

def test_token_is_validated_and_stored_owner_only(tmp_path):
    path = tmp_path / "token"
    with pytest.raises(ci_control.CiControlError):
        ci_control.save_token(path, "not a token")
    ci_control.save_token(path, f"  {TOKEN}\n")

    assert ci_control.read_token(path) == TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_dispatch_starts_the_workflow_and_reports_refusals():
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(204 if "good" in str(request.url) else 403)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ci_control.dispatch_ci("org/good", "ci-cd.yml", "main", TOKEN, client=client)
    with pytest.raises(ci_control.CiControlError, match="HTTP 403"):
        ci_control.dispatch_ci("org/bad", "ci-cd.yml", "main", TOKEN, client=client)
    with pytest.raises(ci_control.CiControlError, match="token"):
        ci_control.dispatch_ci("org/good", "ci-cd.yml", "main", None, client=client)
    with pytest.raises(ci_control.CiControlError, match="nhánh"):
        ci_control.dispatch_ci("org/good", "ci-cd.yml", "../x", TOKEN, client=client)

    assert seen[0] == ("POST", "https://api.github.com/repos/org/good/actions/workflows/ci-cd.yml/dispatches",
                       f"Bearer {TOKEN}", {"ref": "main"})


# --- deploy requests ----------------------------------------------------------------

def test_a_deploy_request_needs_the_typed_confirmation_and_is_one_at_a_time(tmp_path):
    with pytest.raises(ci_control.CiControlError, match="DEPLOY aaaaaaaa"):
        ci_control.request_deploy(tmp_path, sha=SHA, confirmation="yes", user="admin")

    ci_control.request_deploy(tmp_path, sha=SHA, confirmation="DEPLOY aaaaaaaa", user="admin")
    with pytest.raises(ci_control.CiControlError, match="chờ xử lý"):
        ci_control.request_deploy(tmp_path, sha=SHA, confirmation="DEPLOY aaaaaaaa", user="admin")

    assert ci_control.read_deploy_status(tmp_path)["queued"]["sha"] == SHA


def _ci(conclusion="success", status="completed"):
    return {"runs": [{"head_sha": SHA, "status": status, "conclusion": conclusion}], "error": None}


@pytest.fixture
def admin(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ci_github_token_file", str(tmp_path / "token"))
    monkeypatch.setattr(settings, "deploy_request_dir", str(tmp_path / "requests"))
    monkeypatch.setattr(route.test_progress, "fetch_ci_runs", lambda repo: _ci())
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    return dashboard_client


def test_only_the_newest_green_commit_can_be_deployed(admin, monkeypatch, tmp_path):
    wrong = admin.post("/test-progress/deploy", data={"sha": "b" * 40, "confirmation": "DEPLOY bbbbbbbb"},
                       follow_redirects=False)
    assert "err=" in wrong.headers["location"]

    ok = admin.post("/test-progress/deploy", data={"sha": SHA, "confirmation": "DEPLOY aaaaaaaa"},
                    follow_redirects=False)
    assert "ok=" in ok.headers["location"]
    assert json.loads((tmp_path / "requests" / "pending.json").read_text())["sha"] == SHA

    monkeypatch.setattr(route.test_progress, "fetch_ci_runs", lambda repo: _ci(conclusion="failure"))
    assert admin.get("/api/test-progress").json()["latest_green_sha"] is None


def test_run_ci_without_a_token_explains_what_is_missing(admin):
    response = admin.post("/test-progress/ci-run", data={"ref": "main"}, follow_redirects=False)

    assert "err=" in response.headers["location"]
    assert "token" in admin.get(response.headers["location"]).text


def test_buttons_are_admin_only(admin, monkeypatch):
    monkeypatch.setattr(route.auth, "is_admin_user", lambda _user: False)

    for path, data in (("/test-progress/ci-run", {"ref": "main"}), ("/test-progress/ci-token", {"token": TOKEN}),
                       ("/test-progress/deploy", {"sha": SHA, "confirmation": "DEPLOY aaaaaaaa"})):
        assert admin.post(path, data=data, follow_redirects=False).status_code == 403


# --- host runner ----------------------------------------------------------------------

@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "REQUEST_DIR", tmp_path)
    monkeypatch.setattr(runner, "ARTIFACTS", tmp_path)
    monkeypatch.setattr(runner, "read_env", lambda: {})
    sent = []
    monkeypatch.setattr(runner, "send_telegram", lambda env, text: sent.append(text))
    (tmp_path / "pending.json").write_text(json.dumps({"sha": SHA, "requested_by": "admin"}))
    return tmp_path, sent


def _status(directory):
    return json.loads((directory / "status.json").read_text())


def test_runner_refuses_a_commit_whose_ci_is_not_green(host, monkeypatch):
    directory, sent = host

    def not_green(sha):
        raise runner.Refused(f"CI của {sha[:8]} chưa xanh")

    monkeypatch.setattr(runner, "ci_green", not_green)
    monkeypatch.setattr(runner, "deploy", lambda *a: pytest.fail("must not deploy"))

    assert runner.main() == 0
    assert not (directory / "pending.json").exists()  # consumed once
    assert _status(directory)["state"] == "refused" and sent and sent[0].startswith("⛔")


def test_runner_deploys_after_every_check_passes(host, monkeypatch):
    directory, sent = host
    calls = []
    monkeypatch.setattr(runner, "ci_green", lambda sha: None)
    monkeypatch.setattr(runner, "running_revision", lambda: "c" * 40)
    monkeypatch.setattr(runner, "checkout_ready", lambda sha, running: calls.append(("checkout", running)))
    monkeypatch.setattr(runner, "registry_digest", lambda sha: "sha256:" + "d" * 64)
    monkeypatch.setattr(runner, "deploy", lambda sha, digest, log: calls.append(("deploy", sha, digest)) or True)

    assert runner.main() == 0
    assert calls == [("checkout", "c" * 40), ("deploy", SHA, "sha256:" + "d" * 64)]
    assert _status(directory)["state"] == "succeeded" and sent[-1].startswith("✅")


def test_runner_never_rolls_back(monkeypatch, tmp_path):
    results = {"cat-file": 0, "merge-base": 1, "status": 0, "fetch": 0}

    def fake_run(argv, **kwargs):
        key = next(word for word in ("cat-file", "merge-base", "status", "fetch") if word in argv)
        return type("R", (), {"returncode": results[key], "stdout": ""})()

    monkeypatch.setattr(runner, "_run", fake_run)
    with pytest.raises(runner.Refused, match="không lùi bản"):
        runner.checkout_ready(SHA, "c" * 40)
    with pytest.raises(runner.Refused, match="đang chạy rồi"):
        runner.checkout_ready(SHA, SHA)


def test_units_are_installed_and_enabled_by_the_deploy():
    systemd = ROOT / "scripts" / "deploy" / "systemd"
    path_unit = (systemd / "ceph-ai-deploy-request.path").read_text()
    service = (systemd / "ceph-ai-deploy-request.service").read_text()
    deploy = (ROOT / "scripts" / "deploy" / "restart_container_stack.sh").read_text()
    assert "PathExists=/var/lib/ceph-ai/deploy-requests/pending.json" in path_unit
    assert "ExecStart=/usr/bin/python3.11 /root/ceph-ai/scripts/deploy/deploy_request_runner.py" in service
    assert "systemctl enable --now ceph-ai-deploy-request.path" in deploy
    assert '"deploy-requests"' in (ROOT / "scripts" / "bootstrap_container_config.py").read_text()
