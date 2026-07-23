import dashboard.routes.nodes as nodes_route
from config.settings import settings
from shared import db
from shared.models import NodeDiagnosticRun
from watcher.ceph_client import CephQueryError, DIAGNOSTIC_COMMANDS


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure_nodes(monkeypatch):
    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "10.20.1.83")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "10.20.1.21")


def test_unauthenticated_get_diagnostics_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/api/nodes/10.20.1.150/diagnostics", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_post_diagnostics_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/api/nodes/10.20.1.150/diagnostics", data={"command_id": "ceph_status"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_diagnostics_lists_whitelisted_commands_and_empty_history(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/nodes/10.20.1.150/diagnostics")

    assert response.status_code == 200
    body = response.json()
    returned_ids = {c["command_id"] for c in body["commands"]}
    assert returned_ids == set(DIAGNOSTIC_COMMANDS.keys())
    assert body["runs"] == []


def test_get_diagnostics_rejects_host_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/nodes/8.8.8.8/diagnostics")

    assert response.status_code == 404


def test_mgr_node_is_selectable_and_whitelisted(dashboard_client, monkeypatch):
    # Story: cephadm needs MON+MGR+OSD reachability — MGR nodes must be a
    # first-class part of _configured_nodes(), not just MON/OSD.
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    page = dashboard_client.get("/nodes")
    assert 'role-badge-mgr">MGR' in page.text

    response = dashboard_client.get("/api/nodes/10.20.1.21/diagnostics")
    assert response.status_code == 200


def test_post_diagnostics_rejects_unknown_command_id_without_running_anything(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    calls = []
    monkeypatch.setattr(
        nodes_route, "run_diagnostic_command", lambda host, command_id: calls.append((host, command_id))
    )

    response = dashboard_client.post(
        "/api/nodes/10.20.1.150/diagnostics", data={"command_id": "rm -rf /"}
    )

    assert response.status_code == 400
    assert calls == []


def test_post_diagnostics_rejects_host_not_configured_without_running_anything(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    calls = []
    monkeypatch.setattr(
        nodes_route, "run_diagnostic_command", lambda host, command_id: calls.append((host, command_id))
    )

    response = dashboard_client.post(
        "/api/nodes/8.8.8.8/diagnostics", data={"command_id": "ceph_status"}
    )

    assert response.status_code == 404
    assert calls == []


def test_post_diagnostics_runs_whitelisted_command_and_records_audit_row(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    monkeypatch.setattr(
        nodes_route, "run_diagnostic_command", lambda host, command_id: "cluster:\n  health: HEALTH_OK\n"
    )

    response = dashboard_client.post(
        "/api/nodes/10.20.1.150/diagnostics", data={"command_id": "ceph_status"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["host"] == "10.20.1.150"
    assert body["command_id"] == "ceph_status"
    assert body["command_label"] == DIAGNOSTIC_COMMANDS["ceph_status"]
    assert body["actor"] == "admin"
    assert body["success"] is True
    assert body["output_excerpt"] == "cluster:\n  health: HEALTH_OK\n"

    with db.SessionLocal() as session:
        rows = session.query(NodeDiagnosticRun).all()
    assert len(rows) == 1
    assert rows[0].host == "10.20.1.150"
    assert rows[0].command_id == "ceph_status"
    assert rows[0].success is True

    history = dashboard_client.get("/api/nodes/10.20.1.150/diagnostics").json()
    assert len(history["runs"]) == 1
    assert history["runs"][0]["command_id"] == "ceph_status"


def test_post_diagnostics_records_failed_run_when_command_errors(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    def fake_run(host, command_id):
        raise CephQueryError(f"{host}: command exited 1: unable to find a keyring")

    monkeypatch.setattr(nodes_route, "run_diagnostic_command", fake_run)

    response = dashboard_client.post(
        "/api/nodes/10.20.1.150/diagnostics", data={"command_id": "ceph_status"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "unable to find a keyring" in body["output_excerpt"]

    with db.SessionLocal() as session:
        rows = session.query(NodeDiagnosticRun).all()
    assert len(rows) == 1
    assert rows[0].success is False
