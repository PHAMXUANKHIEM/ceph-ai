from types import SimpleNamespace

import dashboard.routes.volumes as volumes_route
from dashboard import volume_path_discovery


def _cluster(**overrides):
    values = {
        "id": "cluster-paths",
        "openstack_compute_nodes": "compute-1,compute-2",
        "ssh_user": "root",
        "ssh_key_path": "/tmp/id_ed25519",
        "ceph_exec_mode": "none",
        "ceph_container_name": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_path_parsers_normalize_multipath_nvme_and_iscsi():
    multipath = volume_path_discovery._parse_multipath(
        "mpatha (3600abc) dm-0\n"
        "size=10G features='1 queue_if_no_path'\n"
        "`- 3:0:0:1 sda 8:0 active ready running\n"
    )
    nvme = volume_path_discovery._parse_nvme(
        '{"Subsystems":[{"Name":"nvme-subsys0","NQN":"nqn.test","Paths":[{"Name":"nvme0","Transport":"tcp","State":"live","Address":"10.0.0.1"}]}]}'
    )
    iscsi = volume_path_discovery._parse_iscsi(
        "tcp: [7] 10.0.0.2:3260,1 iqn.2026-01.example:storage (non-flash)\n"
    )

    assert multipath["status"] == "observed"
    assert multipath["devices"][0]["status"] == "healthy"
    assert nvme["subsystems"][0]["paths"][0]["state"] == "live"
    assert iscsi["sessions"][0]["state"] == "logged_in"


def test_path_report_is_unsupported_without_configured_compute_nodes():
    result = volume_path_discovery.build_path_report(
        _cluster(openstack_compute_nodes=""), "volume-test",
        {"status": "not_configured", "verified": False}, [],
    )

    assert result["status"] == "unsupported"
    assert result["read_only"] is True
    assert result["mutation_supported"] is False
    assert result["evidence_gaps"]


def test_path_inventory_api_is_cluster_scoped_and_read_only(dashboard_client, monkeypatch):
    cluster = _cluster()
    monkeypatch.setattr(volumes_route, "_allowed_pools_for_request", lambda _request: (cluster, {"images"}))
    monkeypatch.setattr(volumes_route, "configured_compute_nodes", lambda _cluster: ["compute-1"])
    monkeypatch.setattr(
        volumes_route,
        "discover_cinder_volume",
        lambda *_args: {"status": "managed", "verified": True, "volume_id": "volume-id"},
    )
    monkeypatch.setattr(
        volumes_route,
        "collect_host_path_evidence",
        lambda *_args: {"host": "compute-1", "multipath": {"status": "empty", "devices": []}, "nvmeof": {"status": "empty", "subsystems": []}, "iscsi": {"status": "empty", "sessions": []}, "errors": [], "read_only": True, "mutation_supported": False},
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get("/api/volumes/images/inventory/volume-test/paths")

    assert response.status_code == 200
    body = response.json()
    assert body["cluster_id"] == "cluster-paths"
    assert body["status"] == "observed"
    assert body["mutation_supported"] is False
