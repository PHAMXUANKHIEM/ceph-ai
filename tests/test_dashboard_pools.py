import dashboard.routes.pgs as pools_route


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_normalize_pool_rows_joins_config_capacity_and_iops():
    rows = pools_route._normalize_pool_rows(
        [{"pool_name": "volumes", "size": 3, "pg_num": 128, "crush_rule": 2, "flags_names": "hashpspool,nodelete"}],
        {"pools": [{"name": "volumes", "stats": {"stored": 1073741824, "objects": 42}}]},
        [{"pool_name": "volumes", "client_io_rate": {"read_op_per_sec": 17, "write_op_per_sec": 9}}],
        [{"rule_id": 2, "rule_name": "replicated-ssd"}],
    )

    assert rows == [{
        "name": "volumes",
        "redundancy": "3 replicas",
        "size": 3,
        "protected": True,
        "pgs": 128,
        "crush_rule": "replicated-ssd",
        "used_bytes": 1073741824,
        "objects": 42,
        "read_iops": 17,
        "write_iops": 9,
    }]


def test_pools_page_renders_requested_columns(dashboard_client, monkeypatch):
    payloads = {
        "ceph osd pool ls detail": [{"pool_name": "volumes", "size": 3, "pg_num": 32, "crush_rule": 0}],
        "ceph df detail": {"pools": [{"name": "volumes", "stats": {"stored": 2048, "objects": 5}}]},
        "ceph osd pool stats": [{"pool_name": "volumes", "client_io_rate": {"read_op_per_sec": 4, "write_op_per_sec": 2}}],
        "ceph osd crush rule dump": [{"rule_id": 0, "rule_name": "replicated_rule"}],
    }
    monkeypatch.setattr(pools_route.ceph_client, "run_ceph_json_command", lambda command: ("mon1", payloads[command]))
    _login(dashboard_client)

    response = dashboard_client.get("/pools")

    assert response.status_code == 200
    assert 'id="pools-dashboard-root"' in response.text
    assert 'id="pools-bootstrap-data"' in response.text
    assert '/static/ceph-health/app.js' in response.text
    assert "volumes" in response.text
    assert "replicated_rule" in response.text
    assert "2.0 KiB" in response.text


def test_volume_performance_does_not_render_create_pool(dashboard_client, monkeypatch):
    monkeypatch.setattr("dashboard.routes.volumes._rbd_pools_for_request", lambda request: ["volumes"])
    _login(dashboard_client)
    response = dashboard_client.get("/volumes?pool=volumes")
    assert response.status_code == 200
    assert 'action="/pgs/pools/create"' not in response.text
    assert 'id="pool-create-open"' not in response.text


def test_pools_page_shows_create_success_message(dashboard_client, monkeypatch):
    monkeypatch.setattr(pools_route.ceph_client, "run_ceph_json_command", lambda command: ("mon1", []))
    _login(dashboard_client)
    response = dashboard_client.get("/pools?create_success=1")
    assert response.status_code == 200
    assert '"createSuccess": true' in response.text


def test_pools_react_component_contains_requested_toolbar_and_table():
    source = open("ceph-health-dashboard/src/components/PoolsPage.tsx", encoding="utf-8").read()
    for label in ("Metrics", "Create", "Edit", "Scrub", "Details", "Delete", "Search", "Columns"):
        assert f'label="{label}"' in source
    for heading in ("Pool Name", "Redundancy", "#PGs", "Crush Rule", "Used disk space", "Objects", "Read IOPS", "Write IOPS"):
        assert heading in source
    assert "1 selected" in source
    assert "Rows per page:" in source
    assert 'action="/pgs/pools/create"' in source
    assert 'name="cluster_id"' in source


def test_navigation_places_volume_performance_under_monitoring():
    source = open("dashboard/static/app.js", encoding="utf-8").read()
    assert '{ label: "Monitoring & Metrics", paths: ["/", "/nodes", "/volumes", "/crush-map"] }' in source
    assert '{ label: "Pool", paths: ["/pools", "/pgs", "/trash"] }' in source
    assert 'if (path === "/volumes") link.textContent = "Volume Performance";' in source
