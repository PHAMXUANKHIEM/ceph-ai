import dashboard.routes.nodes as nodes_route
from config.settings import settings
from watcher.rgw_log import RgwLogError


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure_nodes(monkeypatch):
    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.150")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "10.20.1.83")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "10.20.1.90,10.20.1.91")
    monkeypatch.setattr(settings, "ceph_rgw_container_name", "ceph-rgw-B")


def test_unauthenticated_rgw_log_api_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/api/nodes/10.20.1.90/rgw-log", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_rgw_node_is_selectable_without_legacy_log_panel(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    page = dashboard_client.get("/nodes?host=10.20.1.90")
    assert page.status_code == 200
    assert 'role-badge-rgw">RGW' in page.text
    assert 'id="rgw-log-panel"' not in page.text
    assert "nodes_rgw_log.js" not in page.text


def test_rgw_log_panel_hidden_for_non_rgw_node(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    page = dashboard_client.get("/nodes?host=10.20.1.150")
    assert page.status_code == 200
    assert 'id="rgw-log-panel"' not in page.text


def test_rgw_log_api_returns_lines_for_configured_rgw_host(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    monkeypatch.setattr(nodes_route, "fetch_rgw_log", lambda host, filter_text: "line1\nline2")

    response = dashboard_client.get("/api/nodes/10.20.1.90/rgw-log")

    assert response.status_code == 200
    body = response.json()
    assert body["host"] == "10.20.1.90"
    assert body["lines"] == ["line1", "line2"]


def test_rgw_log_api_passes_filter_query_param_through(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    captured = {}

    def fake_fetch(host, filter_text):
        captured["host"] = host
        captured["filter_text"] = filter_text
        return "matched line"

    monkeypatch.setattr(nodes_route, "fetch_rgw_log", fake_fetch)

    response = dashboard_client.get("/api/nodes/10.20.1.90/rgw-log?filter=bucket-42")

    assert response.status_code == 200
    assert captured == {"host": "10.20.1.90", "filter_text": "bucket-42"}
    assert response.json()["lines"] == ["matched line"]


def test_rgw_log_api_rejects_host_not_in_rgw_node_list_without_calling_fetch(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    calls = []
    monkeypatch.setattr(nodes_route, "fetch_rgw_log", lambda host, filter_text: calls.append(host) or "")

    # 10.20.1.150 is a real configured node (MON) but NOT RGW — must still 404.
    response = dashboard_client.get("/api/nodes/10.20.1.150/rgw-log")

    assert response.status_code == 404
    assert calls == []


def test_rgw_log_api_rejects_host_not_configured_at_all(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/nodes/8.8.8.8/rgw-log")

    assert response.status_code == 404


def test_rgw_log_api_returns_502_when_fetch_fails(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    def failing_fetch(host, filter_text):
        raise RgwLogError(f"{host}: unreachable")

    monkeypatch.setattr(nodes_route, "fetch_rgw_log", failing_fetch)

    response = dashboard_client.get("/api/nodes/10.20.1.90/rgw-log")

    assert response.status_code == 502
