import dashboard.routes.nodes as nodes_route
from config.settings import settings
from watcher.node_metrics import NodeMetricsError


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
