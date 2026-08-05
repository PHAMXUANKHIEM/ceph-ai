"""Route tests for Epic 10 Story 10.2 -- dashboard/routes/test_runner.py's
4 new config endpoints (GET/POST /api/test-runner/config, POST .../config/
baseline/{baseline_key}, POST .../ssh-check). Follows this project's
existing FastAPI route-testing conventions (see test_dashboard_backups.py,
test_dashboard_upgrade_procedure.py): the `dashboard_client` fixture
(isolated in-memory DB + fixed test credentials) and a `_login` helper that
posts to /login before each authenticated request.

Story 10.6 (below, from "-- Story 10.6" section) adds tests for the 4 new
run-engine endpoints (GET /tests, POST .../run, GET .../result, POST .../
override). Those tests fake the whole test-case registry (TEST_CASES_BY_ID)
rather than exercising a real Group A-D TestCase over SSH -- real classes
would need actual network access and the default 3x/5s retry policy would
make a real-SSH-failure test take 15+ seconds.
"""
import time

import pytest

import dashboard.routes.test_runner as test_runner_route
from shared import db as db_module
# Aliased on import -- pytest auto-collects anything matching Test* once
# bound as a name in this module (same reason Story 10.1's test_ssh_pool.py
# aliases the imported test_all_nodes function).
from shared.models import TestRunnerConfig as RunnerConfigModel
from worker.executor.test_runner import framework as fw


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure_nodes(monkeypatch, settings, *, mon="", mgr="", osd="", rgw=""):
    monkeypatch.setattr(settings, "ceph_mon_nodes", mon)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", mgr)
    monkeypatch.setattr(settings, "ceph_osd_nodes", osd)
    monkeypatch.setattr(settings, "ceph_rgw_nodes", rgw)


def _redirect_baseline_dir(monkeypatch, tmp_path):
    """Points the shared baseline-upload directory at a tmp dir instead of
    the real project-root test_runner_baselines/ -- otherwise running this
    suite would write real files into the checkout. Patches
    `shared.test_runner_baselines.BASELINE_FILES_DIR` (via
    `test_runner_route.baselines`, the same module object) rather than a
    module-local name on test_runner_route itself -- the route file only
    ever reads `baselines.BASELINE_FILES_DIR` (qualified), so patching the
    shared module's own attribute is the one place that actually affects
    every reader (this route file AND worker/executor/test_runner/group_b.py)."""
    monkeypatch.setattr(test_runner_route.baselines, "BASELINE_FILES_DIR", tmp_path / "test_runner_baselines")


# -- GET /api/test-runner/config ---------------------------------------------


def test_unauthenticated_get_config_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/api/test-runner/config", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_config_empty_state_returns_defaults_not_error(dashboard_client, monkeypatch):
    from config.settings import settings

    _configure_nodes(monkeypatch, settings, mon="10.0.0.1")
    _login(dashboard_client)

    response = dashboard_client.get("/api/test-runner/config")

    assert response.status_code == 200
    data = response.json()
    assert data["nodes"] == [{"host": "10.0.0.1", "roles": ["MON"]}]
    assert data["rgw_endpoint_zone_a"] is None
    assert data["rgw_endpoint_zone_b"] is None
    assert data["rgw_endpoint_vip"] is None
    assert data["test_groups"] == []
    assert data["priorities"] == []
    assert data["baseline_files"] == {key: False for key in test_runner_route.baselines.BASELINE_FILE_KEYS}

    with db_module.SessionLocal() as session:
        assert session.query(RunnerConfigModel).first() is None


# -- POST /api/test-runner/config --------------------------------------------


def test_unauthenticated_post_config_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/api/test-runner/config", json={}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_save_then_reload_round_trip(dashboard_client):
    _login(dashboard_client)

    save_response = dashboard_client.post(
        "/api/test-runner/config",
        json={
            "rgw_endpoint_zone_a": "http://rgw-a:7480",
            "rgw_endpoint_zone_b": "http://rgw-b:7480",
            "rgw_endpoint_vip": "http://rgw-vip:7480",
            "test_groups": ["A", "B"],
            "priorities": ["P1"],
        },
    )
    assert save_response.status_code == 200

    reload_response = dashboard_client.get("/api/test-runner/config")
    assert reload_response.status_code == 200
    data = reload_response.json()
    assert data["rgw_endpoint_zone_a"] == "http://rgw-a:7480"
    assert data["rgw_endpoint_zone_b"] == "http://rgw-b:7480"
    assert data["rgw_endpoint_vip"] == "http://rgw-vip:7480"
    assert data["test_groups"] == ["A", "B"]
    assert data["priorities"] == ["P1"]

    # Only one singleton row -- a second save upserts, doesn't add a row.
    with db_module.SessionLocal() as session:
        assert session.query(RunnerConfigModel).count() == 1


def test_unchecking_all_test_groups_persists_empty_not_all_selected(dashboard_client):
    _login(dashboard_client)

    dashboard_client.post(
        "/api/test-runner/config",
        json={"test_groups": ["A", "B", "C", "D"], "priorities": ["P1", "P2"]},
    )
    # Now save again with everything unchecked.
    response = dashboard_client.post(
        "/api/test-runner/config",
        json={"test_groups": [], "priorities": []},
    )
    assert response.status_code == 200

    reload_response = dashboard_client.get("/api/test-runner/config")
    data = reload_response.json()
    assert data["test_groups"] == []
    assert data["priorities"] == []


# -- POST /api/test-runner/config/baseline/{baseline_key} -------------------


def _upload_baseline(client, baseline_key, content=b"abc123", filename="ignored-client-name.txt"):
    return client.post(
        f"/api/test-runner/config/baseline/{baseline_key}",
        files={"file": (filename, content, "text/plain")},
    )


def test_unauthenticated_baseline_upload_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/api/test-runner/config/baseline/rbd_rep.sha256",
        files={"file": ("x.txt", b"abc", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_upload_baseline_marks_it_present(dashboard_client, monkeypatch, tmp_path):
    _redirect_baseline_dir(monkeypatch, tmp_path)
    _login(dashboard_client)

    response = _upload_baseline(dashboard_client, "rbd_rep.sha256", content=b"deadbeef  file\n")

    assert response.status_code == 200
    data = response.json()
    assert data["baseline_files"]["rbd_rep.sha256"] is True
    # Every other baseline key still not present.
    assert data["baseline_files"]["cephfs.sha256"] is False

    saved_path = tmp_path / "test_runner_baselines" / "rbd_rep.sha256"
    assert saved_path.read_bytes() == b"deadbeef  file\n"

    # File saved under the fixed baseline_key, not the client's filename.
    assert not (tmp_path / "test_runner_baselines" / "ignored-client-name.txt").exists()

    reload_response = dashboard_client.get("/api/test-runner/config")
    assert reload_response.json()["baseline_files"]["rbd_rep.sha256"] is True


def test_upload_unknown_baseline_key_rejected_with_400(dashboard_client, monkeypatch, tmp_path):
    _redirect_baseline_dir(monkeypatch, tmp_path)
    _login(dashboard_client)

    response = _upload_baseline(dashboard_client, "not_a_real_baseline.txt")

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(RunnerConfigModel).first() is None


def test_upload_all_seven_baseline_keys_succeed(dashboard_client, monkeypatch, tmp_path):
    _redirect_baseline_dir(monkeypatch, tmp_path)
    _login(dashboard_client)

    for key in test_runner_route.baselines.BASELINE_FILE_KEYS:
        response = _upload_baseline(dashboard_client, key, content=key.encode())
        assert response.status_code == 200

    final = dashboard_client.get("/api/test-runner/config").json()
    assert final["baseline_files"] == {key: True for key in test_runner_route.baselines.BASELINE_FILE_KEYS}


# -- POST /api/test-runner/ssh-check -----------------------------------------


def test_unauthenticated_ssh_check_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/api/test-runner/ssh-check", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_ssh_check_zero_configured_nodes_returns_empty_dict(dashboard_client, monkeypatch):
    from config.settings import settings

    _configure_nodes(monkeypatch, settings)  # all blank
    _login(dashboard_client)

    response = dashboard_client.post("/api/test-runner/ssh-check")

    assert response.status_code == 200
    assert response.json() == {}


def test_ssh_check_mixed_reachability_never_raises(dashboard_client, monkeypatch):
    from config.settings import settings

    _configure_nodes(monkeypatch, settings, mon="host-up", osd="host-down")
    _login(dashboard_client)

    def fake_test_all_nodes(hosts, *args, **kwargs):
        assert set(hosts) == {"host-up", "host-down"}
        return {"host-up": True, "host-down": False}

    monkeypatch.setattr(test_runner_route, "test_all_nodes", fake_test_all_nodes)

    response = dashboard_client.post("/api/test-runner/ssh-check")

    assert response.status_code == 200
    assert response.json() == {"host-up": True, "host-down": False}


def test_ssh_check_all_hosts_unreachable_returns_all_false(dashboard_client, monkeypatch):
    from config.settings import settings

    _configure_nodes(monkeypatch, settings, mon="host-a,host-b")
    _login(dashboard_client)

    def fake_test_all_nodes(hosts, *args, **kwargs):
        return {h: False for h in hosts}

    monkeypatch.setattr(test_runner_route, "test_all_nodes", fake_test_all_nodes)

    response = dashboard_client.post("/api/test-runner/ssh-check")

    assert response.status_code == 200
    assert response.json() == {"host-a": False, "host-b": False}


# -- Story 10.6: run-engine endpoints ----------------------------------------


class _FakeOneShot(fw.TestCase):
    id = "TC-FAKE-ONE"
    name = "fake one-shot"
    group = fw.TestGroup.A
    priority = fw.TestPriority.P1
    background = False

    def run(self, ctx):
        return fw.TestResult(
            test_id=self.id,
            status=fw.TestStatus.RUNNING,
            criteria=[fw.CriterionResult("dummy criterion", passed=True)],
            raw_output="one-shot output\n",
        )


class _FakeErrorOneShot(fw.TestCase):
    id = "TC-FAKE-ERROR"
    name = "fake one-shot that raises"
    group = fw.TestGroup.A
    priority = fw.TestPriority.P2
    background = False

    def run(self, ctx):
        raise ValueError("boom -- a genuine bug in this fake TestCase")


class _FakeBackground(fw.TestCase):
    """poll() resolves on its 2nd call -- lets tests observe an
    intermediate RUNNING poll (accumulating raw_output) before the terminal
    PASS."""

    id = "TC-FAKE-BG"
    name = "fake background"
    group = fw.TestGroup.B
    priority = fw.TestPriority.P1
    background = True

    def start(self, ctx, **kwargs):
        return {"polls": 0}

    def poll(self, ctx, state):
        polls = state["polls"] + 1
        new_state = {"polls": polls}
        if polls < 2:
            criteria = [fw.CriterionResult("still waiting", passed=None, detail=f"poll {polls}")]
        else:
            criteria = [fw.CriterionResult("done", passed=True, detail=f"poll {polls}")]
        result = fw.TestResult(
            test_id=self.id,
            status=fw.TestStatus.RUNNING,
            criteria=criteria,
            raw_output=f"poll-{polls}\n",
        )
        return new_state, result


class _FakeDeclinedStart(fw.TestCase):
    id = "TC-FAKE-DECLINED"
    name = "fake background that declines on start"
    group = fw.TestGroup.C
    priority = fw.TestPriority.P3
    background = True

    def start(self, ctx, **kwargs):
        raise fw.TestCaseDeclined(f"{self.id}: not automatable")


@pytest.fixture
def fake_registry(monkeypatch):
    """Swaps the module-level TEST_CASES_BY_ID/_run_states for an isolated
    set -- qualified monkeypatch.setattr(test_runner_route, ...) so every
    route function (which reads the bare module-global name) sees the fake
    versions, same pattern as this file's existing `test_runner_route.
    baselines` patching. `_run_states` is reset to a fresh dict per test so
    no state leaks between tests via the real module-level singleton."""
    fakes = {
        _FakeOneShot.id: _FakeOneShot(),
        _FakeErrorOneShot.id: _FakeErrorOneShot(),
        _FakeBackground.id: _FakeBackground(),
        _FakeDeclinedStart.id: _FakeDeclinedStart(),
    }
    monkeypatch.setattr(test_runner_route, "TEST_CASES_BY_ID", fakes)
    monkeypatch.setattr(test_runner_route, "_run_states", {})
    return fakes


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_list_tests_unauthenticated_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/api/test-runner/tests", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_list_tests_empty_selection_shows_all(dashboard_client, fake_registry):
    _login(dashboard_client)
    response = dashboard_client.get("/api/test-runner/tests")
    assert response.status_code == 200
    ids = {t["id"] for t in response.json()["tests"]}
    assert ids == set(fake_registry.keys())
    not_started = [t for t in response.json()["tests"] if t["id"] == _FakeOneShot.id][0]
    assert not_started["status"] == "not_started"
    assert not_started["overridden"] is False


def test_list_tests_narrows_to_saved_group_selection(dashboard_client, fake_registry):
    _login(dashboard_client)
    dashboard_client.post(
        "/api/test-runner/config",
        json={"test_groups": ["B"], "priorities": []},
    )
    response = dashboard_client.get("/api/test-runner/tests")
    ids = {t["id"] for t in response.json()["tests"]}
    assert ids == {_FakeBackground.id}


def test_run_unknown_id_returns_404(dashboard_client, fake_registry):
    _login(dashboard_client)
    response = dashboard_client.post("/api/test-runner/tests/TC-DOES-NOT-EXIST/run")
    assert response.status_code == 404


def test_run_one_shot_completes_to_pass(dashboard_client, fake_registry):
    _login(dashboard_client)

    run_response = dashboard_client.post(f"/api/test-runner/tests/{_FakeOneShot.id}/run")
    assert run_response.status_code == 200
    # The fake's run() is instant (no real I/O) -- the daemon thread may
    # already have finished by the time this response is serialized, so
    # either RUNNING (still in flight) or the final PASS is a valid
    # immediate response; what matters is it's never left un-started.
    assert run_response.json()["status"] in (fw.TestStatus.RUNNING.value, fw.TestStatus.PASS.value)

    def _finished():
        r = dashboard_client.get(f"/api/test-runner/tests/{_FakeOneShot.id}/result")
        return r.json()["status"] != fw.TestStatus.RUNNING.value

    assert _wait_until(_finished), "one-shot fake test never left RUNNING"

    final = dashboard_client.get(f"/api/test-runner/tests/{_FakeOneShot.id}/result").json()
    assert final["status"] == fw.TestStatus.PASS.value
    assert final["raw_output"] == "one-shot output\n"
    assert final["criteria"] == [{"description": "dummy criterion", "passed": True, "detail": ""}]


def test_run_one_shot_uncaught_exception_maps_to_error(dashboard_client, fake_registry):
    _login(dashboard_client)
    dashboard_client.post(f"/api/test-runner/tests/{_FakeErrorOneShot.id}/run")

    def _finished():
        r = dashboard_client.get(f"/api/test-runner/tests/{_FakeErrorOneShot.id}/result")
        return r.json()["status"] != fw.TestStatus.RUNNING.value

    assert _wait_until(_finished), "fake error test never left RUNNING"
    final = dashboard_client.get(f"/api/test-runner/tests/{_FakeErrorOneShot.id}/result").json()
    assert final["status"] == fw.TestStatus.ERROR.value
    assert "boom" in final["notes"]


def test_run_twice_while_running_returns_409(dashboard_client, fake_registry):
    _login(dashboard_client)
    first = dashboard_client.post(f"/api/test-runner/tests/{_FakeBackground.id}/run")
    assert first.status_code == 200
    second = dashboard_client.post(f"/api/test-runner/tests/{_FakeBackground.id}/run")
    assert second.status_code == 409


def test_background_start_declined_maps_to_skip(dashboard_client, fake_registry):
    _login(dashboard_client)
    response = dashboard_client.post(f"/api/test-runner/tests/{_FakeDeclinedStart.id}/run")
    assert response.status_code == 200
    assert response.json()["status"] == fw.TestStatus.SKIP.value

    result = dashboard_client.get(f"/api/test-runner/tests/{_FakeDeclinedStart.id}/result").json()
    assert result["status"] == fw.TestStatus.SKIP.value


def test_background_poll_accumulates_output_and_resolves(dashboard_client, fake_registry):
    _login(dashboard_client)
    dashboard_client.post(f"/api/test-runner/tests/{_FakeBackground.id}/run")

    first_poll = dashboard_client.get(f"/api/test-runner/tests/{_FakeBackground.id}/result").json()
    assert first_poll["status"] == fw.TestStatus.RUNNING.value
    assert first_poll["raw_output"] == "poll-1\n"

    second_poll = dashboard_client.get(f"/api/test-runner/tests/{_FakeBackground.id}/result").json()
    assert second_poll["status"] == fw.TestStatus.PASS.value
    # accumulated across both polls, not overwritten by the 2nd delta alone
    assert second_poll["raw_output"] == "poll-1\npoll-2\n"

    # terminal -- a 3rd GET must not poll again (no 3rd delta appended)
    third_poll = dashboard_client.get(f"/api/test-runner/tests/{_FakeBackground.id}/result").json()
    assert third_poll["raw_output"] == "poll-1\npoll-2\n"


def test_override_unknown_id_returns_404(dashboard_client, fake_registry):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/api/test-runner/tests/TC-DOES-NOT-EXIST/override", json={"status": "pass"}
    )
    assert response.status_code == 404


def test_override_invalid_status_returns_400(dashboard_client, fake_registry):
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/api/test-runner/tests/{_FakeBackground.id}/override", json={"status": "maybe"}
    )
    assert response.status_code == 400


def test_override_is_sticky_against_further_polling(dashboard_client, fake_registry):
    _login(dashboard_client)
    dashboard_client.post(f"/api/test-runner/tests/{_FakeBackground.id}/run")
    # one real poll first, so there's something to overwrite if the sticky
    # guard didn't work
    dashboard_client.get(f"/api/test-runner/tests/{_FakeBackground.id}/result")

    override_response = dashboard_client.post(
        f"/api/test-runner/tests/{_FakeBackground.id}/override",
        json={"status": "fail", "note": "vận hành xác nhận thất bại thủ công"},
    )
    assert override_response.status_code == 200
    assert override_response.json()["status"] == fw.TestStatus.FAIL.value
    assert override_response.json()["overridden"] is True

    # a further GET must NOT poll again and must not flip status back
    after = dashboard_client.get(f"/api/test-runner/tests/{_FakeBackground.id}/result").json()
    assert after["status"] == fw.TestStatus.FAIL.value
    assert after["overridden"] is True
    assert after["override_note"] == "vận hành xác nhận thất bại thủ công"
    assert after["raw_output"] == "poll-1\n"  # unchanged since the override
