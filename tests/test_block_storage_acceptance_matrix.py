"""Acceptance matrix for the read-only Block Storage evidence paths.

These tests deliberately exercise failure and scope boundaries instead of
mocking a successful Ceph/OpenStack response only. No test executes a
destructive Ceph or OpenStack command.
"""

from types import SimpleNamespace

import dashboard.routes.volumes as volumes_route
from dashboard import cinder_discovery
from watcher.block_storage_dependencies import build_pool_dependency_health
from watcher.block_storage_policy import build_durability_policy
from watcher.block_storage_pool_lifecycle import build_pool_lifecycle_inventory
from worker.executor.ssh_executor import ExecutorError


VOLUME_ID = "12345678-1234-4123-8123-1234567890ab"
SERVER_A = "abcdefab-1234-4123-8123-1234567890ab"
SERVER_B = "fedcbafe-1234-4123-8123-1234567890ab"


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"})
    assert response.status_code in {200, 303}


def _cluster(**overrides):
    values = {
        "openstack_controller_nodes": "controller-1",
        "openstack_openrc_path": "/root/admin-openrc",
        "ssh_user": "root",
        "ssh_key_path": "/tmp/id_ed25519",
        "ceph_exec_mode": "none",
        "ceph_container_name": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _replicated_overview(**overrides):
    value = {
        "type": "replicated",
        "replica_size": 3,
        "min_size": 2,
        "crush_rule": 0,
    }
    value.update(overrides)
    return value


def _crush_tree(hosts=("node-a", "node-b", "node-c")):
    nodes = [{
        "id": -1,
        "type": "root",
        "name": "default",
        "children": [-(index + 2) for index in range(len(hosts))],
    }]
    for index, host in enumerate(hosts):
        host_id = -(index + 2)
        osd_id = index
        nodes.extend([
            {"id": host_id, "type": "host", "name": host, "children": [osd_id]},
            {"id": osd_id, "type": "osd", "status": "up"},
        ])
    return {"nodes": nodes}


def test_dependency_health_fails_closed_for_unmapped_osd_and_stale_pg():
    result = build_pool_dependency_health(
        "vms",
        {"status": "HEALTH_OK"},
        {"pg_stats": [{
            "pgid": "3.a",
            "state": "active+stale",
            "acting": [99],
            "up": [99],
        }]},
        _crush_tree(("node-a",)),
    )

    assert result["status"] == "CRITICAL"
    assert result["pg"]["bad_count"] == 1
    assert result["osd"]["down_osd_ids"] == []
    assert result["dependency_scope"]["exact_volume_to_pg"] is False
    assert "chưa map được OSD: osd.99" in result["evidence"]["gaps"]
    assert result["read_only"] is True


def test_durability_policy_blocks_invalid_replica_policy():
    result = build_durability_policy(
        _replicated_overview(replica_size=2, min_size=3),
        {"rules": [{
            "rule_id": 0,
            "rule_name": "replicated_rule",
            "steps": [{"op": "chooseleaf_firstn", "num": 0, "type": "host"}],
        }]},
        _crush_tree(("node-a", "node-b", "node-c")),
    )

    assert result["status"] == "CRITICAL"
    assert any("policy không hợp lệ" in note for note in result["notes"])
    assert result["policy_change_supported"] is False
    assert result["read_only"] is True


def test_durability_policy_ec_missing_profile_fails_closed():
    result = build_durability_policy(
        {
            "type": "erasure",
            "erasure_code_profile": "ec-missing",
            "crush_rule": 0,
        },
        {"rules": [{
            "rule_id": 0,
            "rule_name": "ec_rule",
            "steps": [{"op": "chooseleaf_firstn", "num": 0, "type": "host"}],
        }]},
        _crush_tree(("node-a", "node-b", "node-c")),
        ec_profile=None,
    )

    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert "EC profile thiếu k/m hoặc profile chưa đọc được" in result["evidence"]["gaps"]
    assert result["migration_required"] is False


def test_pool_lifecycle_stale_inventory_cannot_be_treated_as_reviewable():
    result = build_pool_lifecycle_inventory(
        "vms",
        {
            "rbd_enabled": True,
            "application_metadata": {"rbd": {}},
            "pg_num": 32,
            "pgp_num": 32,
            "pg_autoscale_mode": "on",
        },
        volume_count=0,
        dependency={"status": "HEALTHY"},
        inventory_stale=True,
    )

    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["dependencies"]["inventory_stale"] is True
    assert "inventory volume đang stale; dependency count cần refresh" in result["evidence"]["gaps"]
    assert result["operation_guard"]["delete"] == "approval_required"


def test_cinder_multiattach_is_healthy_only_with_capability_and_observed_watchers():
    cinder = {
        "status": "managed",
        "verified": True,
        "volume_status": "in-use",
        "multiattach": True,
        "attachments": [
            {"attachment_id": "a1", "instance_id": SERVER_A},
            {"attachment_id": "a2", "instance_id": SERVER_B},
        ],
    }

    result = cinder_discovery.reconcile_cinder_attachment(
        cinder,
        [{"client": "client.1"}, {"client": "client.2"}],
        [],
    )

    assert result["status"] == "healthy"
    assert result["safe"] is True
    assert result["evidence"]["cinder_attachment_count"] == 2
    assert result["evidence"]["ceph_watcher_count"] == 2


def test_cinder_control_plane_outage_becomes_insufficient_evidence(monkeypatch):
    monkeypatch.setattr(
        cinder_discovery,
        "resolve_ssh_creds",
        lambda cluster: ("root", "/tmp/id_ed25519", "none", ""),
    )
    monkeypatch.setattr(
        cinder_discovery,
        "execute_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(ExecutorError("Cinder timeout")),
    )

    result = cinder_discovery.discover_cinder_volume(_cluster(), f"volume-{VOLUME_ID}")
    row = cinder_discovery.build_cinder_mapping_row(
        f"volume-{VOLUME_ID}", {"pool": "images"}, result,
    )

    assert result["status"] == "error"
    assert result["verified"] is False
    assert row["mapping_status"] == "insufficient_evidence"
    assert row["mutation_supported"] is False


def test_cinder_mapping_rejects_pool_outside_selected_cluster_scope(dashboard_client, monkeypatch):
    cluster = SimpleNamespace(id="cluster-tenant-a", is_default=False)
    monkeypatch.setattr(
        volumes_route, "_allowed_pools_for_request",
        lambda _request: (cluster, {"images"}),
    )
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/backups/cinder-mapping")

    assert response.status_code == 404


def test_cinder_mapping_paginates_bounded_rows_and_preserves_scope(dashboard_client, monkeypatch):
    cluster = SimpleNamespace(id="cluster-cinder-page", is_default=False)
    monkeypatch.setattr(
        volumes_route, "_allowed_pools_for_request",
        lambda _request: (cluster, {"images"}),
    )
    monkeypatch.setattr(
        volumes_route,
        "_cached_rbd_inventory_with_state",
        lambda _cluster, _pool: ([
            {"name": f"volume-{VOLUME_ID}", "image_id": "image-a"},
            {"name": "volume-abcdefab-1234-4123-8123-1234567890ab", "image_id": "image-b"},
        ], {"stale": False, "source": "test", "age_seconds": 0}),
    )
    monkeypatch.setattr(
        volumes_route,
        "discover_cinder_volume",
        lambda _cluster, image: {
            "status": "managed", "verified": True, "volume_id": image,
        },
    )
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/images/cinder-mapping?page=999&page_size=1")

    assert response.status_code == 200
    body = response.json()
    assert body["cluster_id"] == "cluster-cinder-page"
    assert body["page"] == 2
    assert body["pages"] == 2
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["read_only"] is True
    assert body["mutation_supported"] is False


def test_boot_dependency_deleted_consumer_and_glance_outage_are_partial():
    result = cinder_discovery.build_boot_dependency_report(
        {
            "status": "managed",
            "verified": True,
            "volume_id": VOLUME_ID,
            "bootable": True,
            "attachments": [{"instance_id": SERVER_A}],
            "image_metadata": {"image_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        },
        {"status": "error", "items": []},
        [{"status": "not_found", "server_id": SERVER_A}],
        None,
    )

    assert result["status"] == "partial"
    assert result["guards"]["protect_boot_volume"] is True
    assert result["guards"]["direct_delete_supported"] is False
    assert result["boot_volume"]["source"] == "bootable_volume"
    assert result["servers"][0]["status"] == "not_found"
    assert any("Glance" in gap for gap in result["evidence_gaps"])
    assert any("Cinder snapshots" in gap for gap in result["evidence_gaps"])
    assert result["mutation_supported"] is False


def test_boot_dependency_unmanaged_volume_does_not_infer_boot_usage():
    result = cinder_discovery.build_boot_dependency_report(
        {"status": "not_cinder", "verified": False},
        {"status": "ok", "items": []},
        [],
        {"status": "ok", "image_id": "image-1"},
    )

    assert result["status"] == "not_applicable"
    assert result["guards"]["protect_boot_volume"] is False
    assert result["boot_volume"]["bootable"] is None
    assert result["read_only"] is True
    assert result["mutation_supported"] is False


def test_attachment_eventual_consistency_stays_high_risk_and_read_only():
    reconciliation = cinder_discovery.reconcile_cinder_attachment(
        {
            "status": "managed",
            "verified": True,
            "volume_status": "available",
            "attachments": [],
        },
        [{"client": "client.1"}],
        [{"locker_id": "client.1"}],
    )
    remediation = cinder_discovery.build_attachment_remediation(
        {"status": "managed", "verified": True, "attachments": []},
        [{"client": "client.1"}],
        [{"locker_id": "client.1"}],
        reconciliation,
    )

    assert reconciliation["status"] == "stale_attachment"
    assert reconciliation["safe"] is False
    assert remediation["posture"] == "REVIEW_BEFORE_DETACH"
    assert remediation["automatic_remediation"] is False
    assert remediation["direct_lock_removal_supported"] is False
