"""Route tests for Epic 10 Story 10.2 -- dashboard/routes/test_runner.py's
4 new config endpoints (GET/POST /api/test-runner/config, POST .../config/
baseline/{baseline_key}, POST .../ssh-check). Follows this project's
existing FastAPI route-testing conventions (see test_dashboard_backups.py,
test_dashboard_upgrade_procedure.py): the `dashboard_client` fixture
(isolated in-memory DB + fixed test credentials) and a `_login` helper that
posts to /login before each authenticated request.
"""
import dashboard.routes.test_runner as test_runner_route
from shared import db as db_module
# Aliased on import -- pytest auto-collects anything matching Test* once
# bound as a name in this module (same reason Story 10.1's test_ssh_pool.py
# aliases the imported test_all_nodes function).
from shared.models import TestRunnerConfig as RunnerConfigModel


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure_nodes(monkeypatch, settings, *, mon="", mgr="", osd="", rgw=""):
    monkeypatch.setattr(settings, "ceph_mon_nodes", mon)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", mgr)
    monkeypatch.setattr(settings, "ceph_osd_nodes", osd)
    monkeypatch.setattr(settings, "ceph_rgw_nodes", rgw)


def _redirect_baseline_dir(monkeypatch, tmp_path):
    """Points the route module's baseline-upload directory at a tmp dir
    instead of the real project-root test_runner_baselines/ -- otherwise
    running this suite would write real files into the checkout."""
    monkeypatch.setattr(test_runner_route, "BASELINE_FILES_DIR", tmp_path / "test_runner_baselines")


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
    assert data["baseline_files"] == {key: False for key in test_runner_route.BASELINE_FILE_KEYS}

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

    for key in test_runner_route.BASELINE_FILE_KEYS:
        response = _upload_baseline(dashboard_client, key, content=key.encode())
        assert response.status_code == 200

    final = dashboard_client.get("/api/test-runner/config").json()
    assert final["baseline_files"] == {key: True for key in test_runner_route.BASELINE_FILE_KEYS}


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
