import dashboard.routes.pgs as pools_route


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_normalize_pool_rows_joins_config_capacity_and_iops():
    rows = pools_route._normalize_pool_rows(
        [{"pool_name": "volumes", "size": 3, "pg_num": 128, "crush_rule": 2}],
        {"pools": [{"name": "volumes", "stats": {"stored": 1073741824, "objects": 42}}]},
        [{"pool_name": "volumes", "client_io_rate": {"read_op_per_sec": 17, "write_op_per_sec": 9}}],
        [{"rule_id": 2, "rule_name": "replicated-ssd"}],
    )

    assert rows == [{
        "name": "volumes",
        "redundancy": "3 replicas",
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
    for heading in ("Pool Name", "Redundancy", "PGs", "Crush Rules", "Used disk space", "Object", "Read IOPS", "Write IOPS"):
        assert heading in response.text
    assert "volumes" in response.text
    assert "replicated_rule" in response.text
    assert "2.0 KiB" in response.text


def test_navigation_places_volume_performance_under_monitoring():
    source = open("dashboard/static/app.js", encoding="utf-8").read()
    assert '{ label: "Monitoring & Metrics", paths: ["/", "/nodes", "/volumes", "/crush-map"] }' in source
    assert '{ label: "Pool", paths: ["/pools", "/pgs", "/trash"] }' in source
    assert 'if (path === "/volumes") link.textContent = "Volume Performance";' in source
