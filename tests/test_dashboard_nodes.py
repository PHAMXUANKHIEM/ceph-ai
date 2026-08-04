import json

import pytest

import dashboard.routes.nodes as nodes_route
from config.settings import settings
from shared import db as db_module
from shared.models import Action, ActionStatus, AuditEntry, Incident, IncidentStatus
from watcher.ceph_client import CephQueryError
from watcher.node_metrics import NodeMetricsError


@pytest.fixture(autouse=True)
def _fast_list_osds_default(monkeypatch):
    """2026-08-04: GET /nodes now also calls watcher.ceph_client.list_osds()
    (BlueStore quick-fix picker) on every page load. Left unmocked, this
    hits the real ceph_client.run_ceph_json_command against this suite's
    fake conftest.py mon IPs, costing real seconds (paramiko's own connect
    timeout x however many mon nodes) per test — same slowness class fixed
    for watcher/volume_monitor.py and watcher/device_health_monitor.py in
    tests/test_watcher_main.py. Defaults to a fast empty list; the tests
    below that actually exercise the OSD picker override this explicitly."""
    monkeypatch.setattr(nodes_route, "list_osds", lambda: [])


def _login(client):
    # dashboard_client fixture (conftest.py) pins these credentials.
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure_nodes(monkeypatch):
    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "10.20.1.83,10.20.1.150")  # .150 is both MON+OSD


def test_unauthenticated_get_nodes_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/nodes", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_metrics_api_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/api/nodes/10.20.1.150/metrics", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_nodes_page_lists_configured_hosts_deduplicated_with_roles(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/nodes")

    assert response.status_code == 200
    assert "10.20.1.150" in response.text
    assert "10.20.1.249" in response.text
    assert "10.20.1.83" in response.text
    # .150 carries both roles, deduplicated to one chip with two role badges
    # (role-badge-mon / role-badge-osd), not a single "MON/OSD" text chip.
    assert 'role-badge-mon">MON' in response.text
    assert 'role-badge-osd">OSD' in response.text
    # 3 distinct configured hosts (.150, .249, .83) -> exactly 3 pills, even
    # though .150 carries two roles — it must not render as two pills.
    assert response.text.count('class="node-chip-ip"') == 3


def test_nodes_page_with_no_host_selects_nothing_and_shows_empty_state(dashboard_client, monkeypatch):
    # Landing on /nodes with no ?host= must NOT silently pick the first
    # configured node — that used to show the metrics panel (and its charts
    # failing to load) before the operator ever chose a node.
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/nodes")

    assert response.status_code == 200
    assert 'id="metrics-panel"' not in response.text
    assert "Chọn một node để xem thông số" in response.text


def test_nodes_page_with_explicit_host_selects_that_node(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/nodes?host=10.20.1.249")

    assert response.status_code == 200
    assert 'id="metrics-panel"' in response.text
    assert 'data-host="10.20.1.249"' in response.text


def test_nodes_page_rejects_host_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/nodes?host=8.8.8.8")

    assert response.status_code == 404


def test_metrics_api_returns_collected_metrics_for_configured_host(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    fake_metrics = {
        "cpu_percent": 42.0,
        "mem_used_mb": 1024.0,
        "mem_total_mb": 2048.0,
        "mem_percent": 50.0,
        "disk_read_iops": 4.5,
        "disk_write_iops": 8.0,
        "disk_latency_ms": 3.2,
    }
    monkeypatch.setattr(nodes_route, "collect_node_metrics", lambda host: fake_metrics)

    response = dashboard_client.get("/api/nodes/10.20.1.150/metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["host"] == "10.20.1.150"
    assert body["cpu_percent"] == 42.0


def test_metrics_api_rejects_host_not_in_configured_list_without_calling_collector(
    dashboard_client, monkeypatch
):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    calls = []
    monkeypatch.setattr(
        nodes_route, "collect_node_metrics", lambda host: calls.append(host) or {}
    )

    response = dashboard_client.get("/api/nodes/8.8.8.8/metrics")

    assert response.status_code == 404
    assert calls == []  # whitelist check happens before any SSH attempt


def test_metrics_api_returns_502_when_collector_fails(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    def fake_collect(host):
        raise NodeMetricsError(f"{host}: unreachable")

    monkeypatch.setattr(nodes_route, "collect_node_metrics", fake_collect)

    response = dashboard_client.get("/api/nodes/10.20.1.150/metrics")

    assert response.status_code == 502


# --- BlueStore quick-fix OSD picker (2026-08-04) ---------------------------


def test_nodes_page_lists_osds_from_list_osds(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(
        nodes_route,
        "list_osds",
        lambda: [{"osd_id": 7, "crush_host": "rex001", "status": "up"}],
    )
    _login(dashboard_client)

    response = dashboard_client.get("/nodes")

    assert response.status_code == 200
    assert "osd.7" in response.text
    assert "rex001" in response.text


def test_nodes_page_shows_osds_error_when_list_osds_fails(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)

    def raising():
        raise CephQueryError("no MON nodes configured")

    monkeypatch.setattr(nodes_route, "list_osds", raising)
    _login(dashboard_client)

    response = dashboard_client.get("/nodes")

    assert response.status_code == 200
    assert "Không lấy được danh sách OSD" in response.text


# --- POST /nodes/bluestore-quick-fix/propose --------------------------------


def test_unauthenticated_propose_bluestore_quick_fix_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/nodes/bluestore-quick-fix/propose",
        json={"osd_id": 7, "host": "10.20.1.83"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_propose_bluestore_quick_fix_creates_pending_risky_action(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/nodes/bluestore-quick-fix/propose", json={"osd_id": 7, "host": "10.20.1.83"}
    )

    assert response.status_code == 201
    action_id = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.action_id == "bluestore_omap_quick_fix"
        assert action.classification == "RISKY"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert json.loads(action.action_params) == {"osd_id": 7}
        assert json.loads(action.target_nodes) == ["10.20.1.83"]
        assert "cephadm unit --name osd.7 stop" in action.proposed_command

        incident = session.get(Incident, action.incident_id)
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value

        audit_entry = session.query(AuditEntry).filter_by(incident_id=incident.id).one()
        assert audit_entry.event_type == "risky_action_pending_approval"
        assert audit_entry.actor == "admin"


def test_propose_bluestore_quick_fix_rejects_host_not_osd_role(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    # 10.20.1.249 is a real configured node (MON only) but NOT OSD.
    response = dashboard_client.post(
        "/nodes/bluestore-quick-fix/propose", json={"osd_id": 7, "host": "10.20.1.249"}
    )

    assert response.status_code == 400


def test_propose_bluestore_quick_fix_rejects_non_integer_osd_id(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/nodes/bluestore-quick-fix/propose", json={"osd_id": "7", "host": "10.20.1.83"}
    )

    assert response.status_code == 400


def test_propose_bluestore_quick_fix_rejects_second_proposal_for_same_osd(
    dashboard_client, monkeypatch
):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)
    _login(dashboard_client)

    first = dashboard_client.post(
        "/nodes/bluestore-quick-fix/propose", json={"osd_id": 7, "host": "10.20.1.83"}
    )
    assert first.status_code == 201

    second = dashboard_client.post(
        "/nodes/bluestore-quick-fix/propose", json={"osd_id": 7, "host": "10.20.1.83"}
    )

    assert second.status_code == 409


def test_propose_bluestore_quick_fix_allows_different_osd_while_one_pending(
    dashboard_client, monkeypatch
):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)
    _login(dashboard_client)

    first = dashboard_client.post(
        "/nodes/bluestore-quick-fix/propose", json={"osd_id": 7, "host": "10.20.1.83"}
    )
    assert first.status_code == 201

    second = dashboard_client.post(
        "/nodes/bluestore-quick-fix/propose", json={"osd_id": 8, "host": "10.20.1.83"}
    )
    assert second.status_code == 201


def test_propose_bluestore_quick_fix_rejects_unsupported_exec_mode(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(settings, "ceph_exec_mode", "docker", raising=False)
    monkeypatch.setattr(settings, "ceph_container_name", "ceph-mon-B", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/nodes/bluestore-quick-fix/propose", json={"osd_id": 7, "host": "10.20.1.83"}
    )

    assert response.status_code == 400
    assert "cephadm or none" in response.json()["detail"]
