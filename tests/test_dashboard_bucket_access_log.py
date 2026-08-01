import dashboard.routes.bucket_access_log as bal_route
from config.settings import settings
from watcher.rgw_access_log import RgwLogError, parse_beast_access_log


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure_nodes(monkeypatch):
    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.150")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "10.20.1.83")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "10.20.1.90,10.20.1.91")
    monkeypatch.setattr(settings, "ceph_rgw_container_name", "ceph-rgw-B")


def test_unauthenticated_get_page_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/bucket-access-log", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_api_redirects_to_login(dashboard_client):
    response = dashboard_client.get(
        "/api/bucket-access-log?host=10.20.1.90", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_page_lists_configured_rgw_hosts(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/bucket-access-log")

    assert response.status_code == 200
    assert "10.20.1.90" in response.text
    assert "10.20.1.91" in response.text
    assert "10.20.1.150" not in response.text  # MON node, not RGW — must not appear


def test_page_shows_empty_state_when_no_rgw_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    _login(dashboard_client)

    response = dashboard_client.get("/bucket-access-log")

    assert response.status_code == 200
    assert "Chưa cấu hình node RGW" in response.text


def test_api_returns_parsed_records_for_configured_rgw_host(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    raw_line = (
        '1 beast: 0x1: 10.20.1.5 - operator [12/Jun/2024:13:11:00.000 +0000] '
        '"GET /my-bucket/photo.jpg HTTP/1.1" 200 1024 - - - latency=0.010s'
    )
    monkeypatch.setattr(
        bal_route, "fetch_bucket_access_log", lambda host, bucket: parse_beast_access_log(raw_line)
    )

    response = dashboard_client.get("/api/bucket-access-log?host=10.20.1.90")

    assert response.status_code == 200
    body = response.json()
    assert body["host"] == "10.20.1.90"
    assert len(body["records"]) == 1
    record = body["records"][0]
    assert record["remote_addr"] == "10.20.1.5"
    assert record["bucket"] == "my-bucket"
    assert record["object"] == "photo.jpg"
    assert record["action"] == "Tải xuống"
    assert record["status"] == 200
    assert record["timestamp"] is not None


def test_api_passes_bucket_query_param_through(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    captured = {}

    def fake_fetch(host, bucket):
        captured["host"] = host
        captured["bucket"] = bucket
        return []

    monkeypatch.setattr(bal_route, "fetch_bucket_access_log", fake_fetch)

    response = dashboard_client.get("/api/bucket-access-log?host=10.20.1.90&bucket=my-bucket")

    assert response.status_code == 200
    assert captured == {"host": "10.20.1.90", "bucket": "my-bucket"}
    assert response.json()["records"] == []


def test_api_rejects_host_not_in_rgw_node_list(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    calls = []
    monkeypatch.setattr(
        bal_route, "fetch_bucket_access_log", lambda host, bucket: calls.append(host) or []
    )

    # 10.20.1.150 is a real configured node (MON) but NOT RGW — must still 404.
    response = dashboard_client.get("/api/bucket-access-log?host=10.20.1.150")

    assert response.status_code == 404
    assert calls == []


def test_api_rejects_host_not_configured_at_all(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/bucket-access-log?host=8.8.8.8")

    assert response.status_code == 404


def test_api_returns_502_when_fetch_fails(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)

    def failing_fetch(host, bucket):
        raise RgwLogError(f"{host}: unreachable")

    monkeypatch.setattr(bal_route, "fetch_bucket_access_log", failing_fetch)

    response = dashboard_client.get("/api/bucket-access-log?host=10.20.1.90")

    assert response.status_code == 502
