import dashboard.routes.bucket_access_log as bal_route
from config.settings import settings
from shared import env_config
from shared import db
from shared.models import BucketLoggingConfig, Cluster, ObjectStorageAuditEntry
from watcher.rgw_access_log import RgwLogError, parse_beast_access_log

_RESTARTED_OK = {"restarted": True, "new_pid": 12345, "error": None}


def _mock_restarts(monkeypatch, watcher_ok=True, worker_ok=True):
    monkeypatch.setattr(
        bal_route,
        "restart_watcher",
        lambda: dict(_RESTARTED_OK, restarted=watcher_ok),
    )
    monkeypatch.setattr(
        bal_route,
        "restart_worker",
        lambda: dict(_RESTARTED_OK, restarted=worker_ok),
    )


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
    assert record["requester"] == "operator"
    assert record["bytes_sent"] == 1024
    assert record["latency_ms"] == 10.0
    assert body["bucket_stats"] is None  # no bucket filter given -> stats never fetched


def test_api_includes_bucket_stats_when_bucket_filter_given(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    monkeypatch.setattr(bal_route, "fetch_bucket_access_log", lambda host, bucket: [])
    captured = {}

    def fake_stats(host, bucket):
        captured["host"] = host
        captured["bucket"] = bucket
        return {"owner": "operator", "creation_time": "2026-08-17T01:02:03Z", "usage": {}, "bucket_quota": {}}

    monkeypatch.setattr(bal_route, "fetch_bucket_stats", fake_stats)

    response = dashboard_client.get("/api/bucket-access-log?host=10.20.1.90&bucket=my-bucket")

    assert response.status_code == 200
    assert captured == {"host": "10.20.1.90", "bucket": "my-bucket"}
    stats = response.json()["bucket_stats"]
    assert stats["owner"] == "operator"
    assert stats["num_objects"] == 0
    assert stats["creation_time"] == "2026-08-17T01:02:03Z"


def test_page_uses_bucket_logging_name_without_duplicate_rgw_config(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    monkeypatch.setattr(bal_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "18.2.4", "is_mixed": False,
    })
    _login(dashboard_client)
    response = dashboard_client.get("/bucket-access-log")
    assert response.status_code == 200
    assert "Bucket Logging" in response.text
    assert "Bucket Access Log —" not in response.text
    assert "<h2>Cấu hình RGW</h2>" not in response.text
    assert "Requester" in response.text
    assert "User-Agent" in response.text
    assert "chưa hỗ trợ native S3 Bucket Logging" in response.text
    assert "HTTP access log của RGW Beast" in response.text
    assert "log nên được đẩy sang bucket đích" not in response.text


def test_bucket_logging_capability_requires_tentacle_20(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    monkeypatch.setattr(bal_route, "fetch_bucket_access_log", lambda host, bucket: [])

    monkeypatch.setattr(bal_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "19.2.3", "is_mixed": False,
    })
    squid = dashboard_client.get("/api/bucket-access-log?host=10.20.1.90").json()["logging_capability"]
    assert squid["native_supported"] is False
    assert squid["mode"] == "beast_access_log"
    assert "Tentacle 20" in squid["reason"]

    monkeypatch.setattr(bal_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "20.2.0", "is_mixed": False,
    })
    tentacle = dashboard_client.get("/api/bucket-access-log?host=10.20.1.90").json()["logging_capability"]
    assert tentacle["native_supported"] is True
    assert tentacle["mode"] == "native_available"


def test_bucket_logging_execute_auto_selects_compatibility_and_persists_config(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    monkeypatch.setattr(bal_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "18.2.4", "is_mixed": False,
    })
    monkeypatch.setattr(bal_route, "_logging_targets", lambda cluster, payload: None)
    body = {"action": "enable", "source_bucket": "application-data",
            "target_bucket": "audit-logs", "prefix": "logs/", "owner": "alice",
            "endpoint": "https://rgw.example.test", "confirmation": "application-data"}

    response = dashboard_client.post("/api/bucket-logging/execute", json=body)

    assert response.status_code == 200
    assert response.json()["mode"] == "compatibility"
    with db.SessionLocal() as session:
        config = session.query(BucketLoggingConfig).one()
        assert config.mode == "compatibility"
        assert config.source_bucket == "application-data"
        assert config.target_bucket == "audit-logs"
        audit = session.get(ObjectStorageAuditEntry, response.json()["request_id"])
        assert audit.result == "succeeded"


def test_native_bucket_logging_uses_put_bucket_logging(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    monkeypatch.setattr(bal_route.ceph_client, "summarize_cluster_versions", lambda: {
        "current_version": "20.2.0", "is_mixed": False,
    })
    monkeypatch.setattr(bal_route, "_logging_targets", lambda cluster, payload: None)
    calls = []
    import dashboard.routes.object_storage as object_storage_route

    class FakeS3:
        def put_bucket_logging(self, **kwargs): calls.append(kwargs)

    monkeypatch.setattr(object_storage_route, "_with_owner_s3",
                        lambda cluster, payload, callback: callback(FakeS3()))
    body = {"action": "enable", "source_bucket": "application-data",
            "target_bucket": "audit-logs", "prefix": "logs/", "owner": "alice",
            "endpoint": "https://rgw.example.test", "confirmation": "application-data"}

    response = dashboard_client.post("/api/bucket-logging/execute", json=body)

    assert response.status_code == 200
    assert response.json()["mode"] == "native"
    assert calls == [{"Bucket": "application-data", "BucketLoggingStatus": {"LoggingEnabled": {
        "TargetBucket": "audit-logs", "TargetPrefix": "logs/"}}}]


def test_api_bucket_stats_none_for_unknown_bucket(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    monkeypatch.setattr(bal_route, "fetch_bucket_access_log", lambda host, bucket: [])
    monkeypatch.setattr(bal_route, "fetch_bucket_stats", lambda host, bucket: None)

    response = dashboard_client.get("/api/bucket-access-log?host=10.20.1.90&bucket=no-such-bucket")

    assert response.status_code == 200
    assert response.json()["bucket_stats"] is None


def test_api_degrades_gracefully_when_bucket_stats_fetch_fails(dashboard_client, monkeypatch):
    # The access log itself must still come back even if radosgw-admin
    # isn't reachable/installed where expected — a stats failure must not
    # turn into a 502 for the whole request.
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    raw_line = (
        '1 beast: 0x1: 10.20.1.5 - operator [12/Jun/2024:13:11:00.000 +0000] '
        '"GET /my-bucket/photo.jpg HTTP/1.1" 200 1024 - - - latency=0.010s'
    )
    monkeypatch.setattr(
        bal_route, "fetch_bucket_access_log", lambda host, bucket: parse_beast_access_log(raw_line)
    )

    def failing_stats(host, bucket):
        raise RgwLogError("radosgw-admin: command not found")

    monkeypatch.setattr(bal_route, "fetch_bucket_stats", failing_stats)

    response = dashboard_client.get("/api/bucket-access-log?host=10.20.1.90&bucket=my-bucket")

    assert response.status_code == 200
    body = response.json()
    assert body["bucket_stats"] is None
    assert len(body["records"]) == 1


def test_api_passes_bucket_query_param_through(dashboard_client, monkeypatch):
    _configure_nodes(monkeypatch)
    _login(dashboard_client)
    captured = {}

    def fake_fetch(host, bucket):
        captured["host"] = host
        captured["bucket"] = bucket
        return []

    monkeypatch.setattr(bal_route, "fetch_bucket_access_log", fake_fetch)
    monkeypatch.setattr(bal_route, "fetch_bucket_stats", lambda host, bucket: None)

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


def test_secondary_cluster_uses_its_own_rgw_scope(dashboard_client, monkeypatch):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = Cluster(
            name="cluster-rgw-2", ceph_mon_nodes="10.99.0.10", ceph_rgw_nodes="10.99.0.90",
            ceph_container_name="mon-2", ceph_rgw_container_name="rgw-2",
            ssh_user="ceph2", ssh_key_path="/keys/ceph2", ceph_exec_mode="docker",
            is_default=False, is_active=True,
        )
        session.add(cluster)
        session.commit()
        cluster_id = cluster.id
    dashboard_client.get(f"/bucket-access-log?cluster={cluster_id}")
    calls = []
    monkeypatch.setattr(
        bal_route, "fetch_bucket_access_log_with",
        lambda *args: calls.append(args) or [],
    )
    response = dashboard_client.get("/api/bucket-access-log?host=10.99.0.90")
    assert response.status_code == 200
    assert calls[0] == ("10.99.0.90", "", "ceph2", "/keys/ceph2", "docker", "rgw-2")
    assert dashboard_client.get("/api/bucket-access-log?host=10.20.1.90").status_code == 404


# --- POST /bucket-access-log/settings ("Cấu hình RGW" form) ---------------


def test_unauthenticated_settings_post_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/bucket-access-log/settings",
        data={"ceph_rgw_nodes": "10.20.1.90"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_settings_save_persists_rgw_nodes_and_container_name(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "ceph_exec_mode", "docker", raising=False)
    _mock_restarts(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/bucket-access-log/settings",
        data={"ceph_rgw_nodes": "10.20.1.90,10.20.1.91", "ceph_rgw_container_name": "ceph-rgw-B"},
    )

    assert response.status_code == 200
    assert settings.ceph_rgw_nodes == "10.20.1.90,10.20.1.91"
    assert settings.ceph_rgw_container_name == "ceph-rgw-B"
    saved = tmp_env.read_text()
    assert "CEPH_RGW_NODES=10.20.1.90,10.20.1.91" in saved
    assert "CEPH_RGW_CONTAINER_NAME=ceph-rgw-B" in saved


def test_settings_save_requires_container_name_when_exec_mode_needs_one(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "ceph_exec_mode", "docker", raising=False)
    _mock_restarts(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/bucket-access-log/settings",
        data={"ceph_rgw_nodes": "10.20.1.90", "ceph_rgw_container_name": ""},
    )

    assert response.status_code == 200
    # Nothing written — original .env content untouched.
    assert "CEPH_RGW_NODES" not in tmp_env.read_text()


def test_settings_save_does_not_require_container_name_for_cephadm(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)
    _mock_restarts(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/bucket-access-log/settings",
        data={"ceph_rgw_nodes": "10.20.1.90", "ceph_rgw_container_name": ""},
    )

    assert response.status_code == 200
    assert settings.ceph_rgw_nodes == "10.20.1.90"


def test_settings_save_allows_clearing_rgw_nodes(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "ceph_exec_mode", "docker", raising=False)
    _mock_restarts(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/bucket-access-log/settings",
        data={"ceph_rgw_nodes": "", "ceph_rgw_container_name": ""},
    )

    assert response.status_code == 200
    assert settings.ceph_rgw_nodes == ""


def test_settings_save_warns_when_watcher_restart_fails(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)
    _mock_restarts(monkeypatch, watcher_ok=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/bucket-access-log/settings", data={"ceph_rgw_nodes": "10.20.1.90"}
    )

    assert response.status_code == 200
    assert settings.ceph_rgw_nodes == "10.20.1.90"
