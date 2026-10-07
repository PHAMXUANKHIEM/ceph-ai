import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

from config.settings import settings
from dashboard.routes import test_progress as route
from scripts import pytest_progress
from shared import test_progress

T0 = datetime(2026, 10, 5, 3, 0, 0, tzinfo=timezone.utc)


def _report(nodeid, when, outcome):
    return SimpleNamespace(
        nodeid=nodeid, when=when,
        passed=outcome == "passed", failed=outcome == "failed", skipped=outcome == "skipped",
    )


def test_reporter_counts_outcomes_and_writes_atomically(tmp_path):
    path = tmp_path / "runs" / "current.json"
    reporter = pytest_progress.ProgressReporter(path, "pre-push abc123")
    reporter.pytest_collection_finish(SimpleNamespace(items=[1, 2, 3, 4]))
    reporter.pytest_runtest_logstart("t::ok", None)
    reporter.pytest_runtest_logreport(_report("t::ok", "setup", "passed"))
    reporter.pytest_runtest_logreport(_report("t::ok", "call", "passed"))
    reporter.pytest_runtest_logreport(_report("t::bad", "call", "failed"))
    reporter.pytest_runtest_logreport(_report("t::skip", "setup", "skipped"))
    reporter.pytest_runtest_logreport(_report("t::broken", "setup", "failed"))
    reporter.pytest_sessionfinish(None, 1)

    state = json.loads(path.read_text())
    assert (state["total"], state["done"], state["passed"], state["failed"], state["skipped"], state["errors"]) == (4, 4, 1, 1, 1, 1)
    assert state["status"] == "failed"
    assert state["label"] == "pre-push abc123"
    assert [f["nodeid"] for f in state["failures"]] == ["t::bad", "t::broken"]
    assert list(path.parent.iterdir()) == [path]  # no temporary file left behind


def test_plugin_is_inert_without_the_environment_variable(monkeypatch):
    monkeypatch.delenv(pytest_progress.ENV_FILE, raising=False)
    registered = []
    config = SimpleNamespace(pluginmanager=SimpleNamespace(register=lambda *a: registered.append(a)))

    pytest_progress.pytest_configure(config)

    assert registered == []


def _write(path: Path, **state):
    path.write_text(json.dumps(state))


def test_running_state_has_percent_and_eta(tmp_path):
    path = tmp_path / "current.json"
    _write(path, status="running", started_at=T0.isoformat(), updated_at=(T0 + timedelta(minutes=10)).isoformat(),
           total=4000, done=1000)

    run = test_progress.read_local_run(path, now=T0 + timedelta(minutes=10))

    assert run["percent"] == 25.0
    assert run["elapsed_seconds"] == 600
    assert run["eta_seconds"] == 1800


def test_a_silent_running_file_is_reported_as_stalled(tmp_path):
    path = tmp_path / "current.json"
    _write(path, status="running", started_at=T0.isoformat(), updated_at=T0.isoformat(), total=10, done=3)

    assert test_progress.read_local_run(path, now=T0 + timedelta(minutes=5))["status"] == "stalled"


def test_missing_or_corrupt_file_reads_as_no_run(tmp_path):
    assert test_progress.read_local_run(tmp_path / "absent.json") is None
    (tmp_path / "bad.json").write_text("{")
    assert test_progress.read_local_run(tmp_path / "bad.json") is None


def _github(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ci_runs_include_jobs_of_the_newest_run_and_are_cached(monkeypatch):
    test_progress._ci_cache.clear()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"name": "quality", "status": "in_progress", "conclusion": None}]})
        if request.url.path.endswith("/branches/main"):
            return httpx.Response(200, json={"commit": {"sha": "1ecd9ead0000"}})
        assert "branch" not in request.url.params  # GitHub's branch filter returned stale runs (07/10/2026)
        return httpx.Response(200, json={"workflow_runs": [
            {"id": 9, "head_sha": "77770000", "head_branch": "cand/x", "event": "pull_request",
             "created_at": "2026-10-07T10:00:00Z", "status": "completed", "conclusion": "success"},
            {"id": 1, "html_url": "https://ci/1", "head_sha": "94e7e55f0000", "head_branch": "main", "event": "push",
             "created_at": "2026-10-03T04:24:41Z", "status": "completed", "conclusion": "success"},
            {"id": 2, "html_url": "https://ci/2", "head_sha": "1ecd9ead0000", "head_branch": "main", "event": "push",
             "created_at": "2026-10-07T09:29:32Z", "display_title": "docs", "status": "in_progress"},
        ]})

    first = test_progress.fetch_ci_runs("owner/repo", client=_github(handler))
    second = test_progress.fetch_ci_runs("owner/repo", client=_github(handler))

    assert first is second
    assert [run["sha"] for run in first["runs"]] == ["1ecd9ead", "94e7e55f"]  # main only, newest first
    assert first["head_sha"] == "1ecd9ead0000"
    assert first["runs"][0]["jobs"][0]["name"] == "quality"
    assert "jobs" not in first["runs"][1]
    assert len(calls) == 3


def test_ci_errors_are_reported_not_raised():
    test_progress._ci_cache.clear()

    result = test_progress.fetch_ci_runs("owner/repo", client=_github(lambda request: httpx.Response(403)))

    assert result["runs"] == []
    assert result["error"].startswith("HTTPStatusError")


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_page_and_api_are_admin_only(dashboard_client, monkeypatch, tmp_path):
    path = tmp_path / "current.json"
    _write(path, status="passed", started_at=T0.isoformat(), updated_at=T0.isoformat(), total=2, done=2)
    monkeypatch.setattr(settings, "test_progress_file", str(path))
    monkeypatch.setattr(route.test_progress, "fetch_ci_runs", lambda repo, **_kwargs: {"repo": repo, "runs": [], "error": None})
    _login(dashboard_client)

    assert dashboard_client.get("/test-progress").status_code == 200
    payload = dashboard_client.get("/api/test-progress").json()
    assert payload["local"]["percent"] == 100.0
    assert payload["ci"]["repo"] == settings.ci_github_repo

    monkeypatch.setattr(route.auth, "is_admin_user", lambda _user: False)
    assert dashboard_client.get("/test-progress").status_code == 403
    assert dashboard_client.get("/api/test-progress").status_code == 403


def test_deploy_log_download_is_admin_only(dashboard_client, monkeypatch, tmp_path):
    log = tmp_path / "deploy-phases.log"
    log.write_text("deploy log\n", encoding="utf-8")
    monkeypatch.setattr(route.test_progress, "DEFAULT_DEPLOY_LOG", log)
    _login(dashboard_client)

    response = dashboard_client.get("/api/test-progress/deploy-log")

    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith('filename="deploy-phases.log"')
    assert response.text == "deploy log\n"
    monkeypatch.setattr(route.auth, "is_admin_user", lambda _user: False)
    assert dashboard_client.get("/api/test-progress/deploy-log").status_code == 403


def test_settings_maintenance_links_to_the_page_for_admins(dashboard_client, monkeypatch):
    # The sidebar is rebuilt from a fixed path list in app.js, so the page is
    # reached from Settings → Bảo trì.
    from dashboard.routes import settings as settings_route

    _login(dashboard_client)
    link = '<a href="/test-progress" class="settings-nav-item">Tiến độ test</a>'
    assert link in dashboard_client.get("/settings").text

    monkeypatch.setattr(settings_route.auth, "is_admin_user", lambda _user: False)
    assert link not in dashboard_client.get("/settings").text


def _steps(path):
    return [(step["name"], step["state"]) for step in json.loads(path.read_text())["steps"]]


def test_pipeline_steps_continue_into_the_test_run(tmp_path):
    path = tmp_path / "current.json"
    pytest_progress.record_pipeline(path, "begin", "pre-push abc123")
    pytest_progress.record_pipeline(path, "step", "Quality gate")
    pytest_progress.record_pipeline(path, "step", "Budget")
    assert json.loads(path.read_text())["status"] == "preparing"
    assert _steps(path) == [("Quality gate", "ok"), ("Budget", "running")]

    reporter = pytest_progress.ProgressReporter(path, "pre-push abc123")
    reporter.pytest_collection_finish(SimpleNamespace(items=[1]))
    reporter.pytest_sessionfinish(None, 0)
    pytest_progress.record_pipeline(path, "step", "Push + CI")
    pytest_progress.record_pipeline(path, "finish", "-")

    state = json.loads(path.read_text())
    assert state["status"] == "passed" and state["total"] == 1
    assert _steps(path) == [("Quality gate", "ok"), ("Budget", "ok"), ("Test", "ok"), ("Push + CI", "ok")]


def test_a_failed_step_ends_the_run_with_its_reason(tmp_path):
    path = tmp_path / "current.json"
    pytest_progress.record_pipeline(path, "begin", "pre-push abc123")
    pytest_progress.record_pipeline(path, "step", "Budget")
    pytest_progress.record_pipeline(path, "fail", "bandit 149 > 148")

    state = json.loads(path.read_text())
    assert state["status"] == "aborted" and state["stage"] == "bandit 149 > 148" and state["finished_at"]
    assert _steps(path) == [("Budget", "failed")]


def test_a_test_run_with_another_label_starts_fresh(tmp_path):
    path = tmp_path / "current.json"
    pytest_progress.record_pipeline(path, "begin", "pre-push old")
    pytest_progress.record_pipeline(path, "step", "Quality gate")

    pytest_progress.ProgressReporter(path, "pre-push new")

    assert _steps(path) == [("Test", "running")]


def test_preparing_runs_get_longer_before_they_count_as_stalled(tmp_path):
    path = tmp_path / "current.json"
    _write(path, status="preparing", started_at=T0.isoformat(), updated_at=T0.isoformat())

    assert test_progress.read_local_run(path, now=T0 + timedelta(minutes=20))["status"] == "preparing"
    assert test_progress.read_local_run(path, now=T0 + timedelta(minutes=31))["status"] == "stalled"


def test_cli_is_inert_without_the_environment_variable(monkeypatch, tmp_path):
    monkeypatch.delenv(pytest_progress.ENV_FILE, raising=False)

    assert pytest_progress.main(["step", "Budget"]) == 0
    assert pytest_progress.main(["oops"]) == 2
    assert not list(tmp_path.glob("*.json"))


def _deploy_log(path, *lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_latest_deploy_lists_its_phases_and_the_previous_deploys(tmp_path):
    log = tmp_path / "deploy-phases.log"
    _deploy_log(
        log,
        "2026-10-05T12:30:00Z phase=preflight status=STARTED detail=",
        "2026-10-05T12:35:01Z phase=complete status=PASSED detail=sha=e8b8331312826648201cad12611869919c37b0c9",
        "2026-10-06T07:48:49Z phase=preflight status=STARTED detail=",
        "2026-10-06T07:48:51Z phase=preflight status=PASSED detail=",
        "2026-10-06T07:49:20Z phase=migration status=STARTED detail=",
        "garbage line",
    )

    deploy = test_progress.read_latest_deploy(log, now=datetime(2026, 10, 6, 7, 50, tzinfo=timezone.utc))

    assert deploy["status"] == "running" and deploy["current"] == "migration" and deploy["sha"] is None
    assert [(phase["name"], phase["status"]) for phase in deploy["phases"]] == [("preflight", "PASSED"), ("migration", "STARTED")]
    assert deploy["previous"] == [{"at": "2026-10-05T12:35:01Z", "status": "PASSED", "sha": "e8b83313"}]


def test_finished_failed_and_silent_deploys(tmp_path):
    log = tmp_path / "deploy-phases.log"
    _deploy_log(log, "2026-10-06T07:48:49Z phase=preflight status=STARTED detail=",
                "2026-10-06T07:52:51Z phase=complete status=PASSED detail=sha=3f081af70b16ed52ff5c97f404ec0eb912aab806")
    done = test_progress.read_latest_deploy(log)
    assert done["status"] == "passed" and done["sha"].startswith("3f081af7") and done["previous"] == []

    _deploy_log(log, "2026-10-06T07:48:49Z phase=preflight status=STARTED detail=",
                "2026-10-06T07:49:20Z phase=migration status=FAILED detail=alembic exited 1")
    assert test_progress.read_latest_deploy(log)["status"] == "failed"

    _deploy_log(log, "2026-10-06T07:48:49Z phase=preflight status=STARTED detail=")
    later = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    assert test_progress.read_latest_deploy(log, now=later)["status"] == "stalled"
    assert test_progress.read_latest_deploy(tmp_path / "missing.log") is None


def test_api_returns_the_latest_deploy(dashboard_client, monkeypatch, tmp_path):
    log = tmp_path / "deploy-phases.log"
    _deploy_log(log, "2026-10-06T07:48:49Z phase=preflight status=STARTED detail=")
    original = test_progress.read_latest_deploy
    monkeypatch.setattr(route.test_progress, "read_latest_deploy", lambda: original(log))
    monkeypatch.setattr(route.test_progress, "fetch_ci_runs", lambda repo, **_kwargs: {"repo": repo, "runs": [], "error": None})
    _login(dashboard_client)

    assert dashboard_client.get("/api/test-progress").json()["deploy"]["phases"][0]["name"] == "preflight"


def test_redesigned_page_keeps_the_candidate_card_and_the_ci_link(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ci_github_token_file", str(tmp_path / "token"))
    _login(dashboard_client)
    run_url = f"https://github.com/{settings.ci_github_repo}/actions/runs/42"

    page = dashboard_client.get("/test-progress?ok=Đã+chạy+CI&url=" + run_url).text

    assert 'id="cand-list"' in page and "/test-progress/candidates/merge" in page
    assert f'href="{run_url}"' in page
    # A notice with a link, and every error, stays until it is closed.
    assert "data-auto-dismiss" not in page.split('class="ci-toast is-ok"')[1].split(">")[0]
    assert "Contents và Pull requests" in page
