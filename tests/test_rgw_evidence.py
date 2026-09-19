import dashboard.routes.object_storage as object_storage_route
from types import SimpleNamespace

from watcher import rgw_evidence


def _cluster():
    return SimpleNamespace(
        id="cluster-1",
        ceph_mon_nodes="mon-1",
        ceph_rgw_nodes="rgw-1",
        ceph_rgw_container_name="ceph-rgw",
        ssh_user="ceph",
        ssh_key_path="/tmp/key",
        ceph_exec_mode="docker",
    )


def test_collect_rgw_evidence_normalizes_topology_sync_and_capacity(monkeypatch):
    monkeypatch.setattr(
        rgw_evidence,
        "configured_nodes",
        lambda _cluster: [{"host": "rgw-1", "roles": ["RGW"]}],
    )

    def fake_mon(_cluster, _command, source):
        payloads = {
            "ceph_orch_ps": {"daemons": [{"daemon_name": "rgw.foo", "hostname": "rgw-1", "status_desc": "running"}]},
            "ceph_mgr_services": {"rgw": "http://rgw-1:7480"},
            "ceph_config_dump": [{"name": "rgw_frontends", "value": "beast port=7480"}],
            "ceph_df": {"pools": [{"name": "default.rgw.buckets.data", "stats": {"bytes_used": 1024, "max_avail": 9999}}]},
        }
        return {"status": "observed", "source": source, "payload": payloads[source]}

    def fake_rgw(_cluster, _hosts, _command, source):
        payloads = {
            "rgw_realm": {"name": "default"},
            "rgw_zonegroup": {"name": "default", "api_name": "default"},
            "rgw_zone": {
                "name": "default",
                "domain_root": "default.rgw.meta",
                "placement_pools": [{"key": "default", "val": {"data_pool": "default.rgw.buckets.data"}}],
            },
            "rgw_sync_status": {"sync_status": "disabled"},
            "rgw_sync_errors": {"errors": []},
            "rgw_period": {"id": "period-1", "epoch": 1, "master_zone": "zone-1"},
        }
        return {"status": "observed", "source": source, "host": "rgw-1", "payload": payloads[source]}

    monkeypatch.setattr(rgw_evidence, "_read_mon_command", fake_mon)
    monkeypatch.setattr(rgw_evidence, "_read_rgw_command", fake_rgw)

    result = rgw_evidence.collect_rgw_evidence(_cluster())

    assert result["status"] == "ready"
    assert result["read_only"] is True
    assert result["action_id"] is None
    assert result["endpoints"]["items"] == ["http://rgw-1:7480"]
    assert result["frontend"]["items"][0]["value"] == "beast port=7480"
    assert result["topology"]["zone"]["status"] == "observed"
    assert result["sync"]["details"]["sync_status"] == "disabled"
    assert result["capacity"]["items"][0]["pool"] == "default.rgw.buckets.data"


def test_collect_rgw_evidence_marks_missing_rgws_without_guessing(monkeypatch):
    monkeypatch.setattr(rgw_evidence, "configured_nodes", lambda _cluster: [])
    monkeypatch.setattr(
        rgw_evidence,
        "_read_mon_command",
        lambda _cluster, _command, source: {
            "status": "not_available", "source": source, "items": [],
        },
    )

    result = rgw_evidence.collect_rgw_evidence(_cluster())

    assert result["status"] == "unavailable"
    assert result["rgw_hosts"] == []
    assert result["topology"]["realm"]["status"] == "not_available"
    assert any("node RGW" in gap for gap in result["evidence_gaps"])


def test_rgw_evidence_api_is_authenticated_and_cluster_scoped(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        object_storage_route,
        "get_rgw_evidence",
        lambda cluster: {
            "status": "partial", "cluster_id": cluster.id, "read_only": True,
            "action_id": None, "evidence_gaps": ["test"],
        },
    )
    response = dashboard_client.get("/api/object-storage/rgw-evidence", follow_redirects=False)
    assert response.status_code in {303, 307}

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/object-storage/rgw-evidence")
    assert response.status_code == 200
    assert response.json()["cluster_id"]
    assert response.json()["read_only"] is True
    assert response.json()["action_id"] is None
