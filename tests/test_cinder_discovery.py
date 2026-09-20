import json
from types import SimpleNamespace

import pytest

from dashboard import cinder_discovery
from worker.executor.ssh_executor import ExecutorError


VOLUME_ID = "12345678-1234-4123-8123-1234567890ab"


def _cluster(**overrides):
    values = {
        "openstack_controller_nodes": "controller-1,controller-2",
        "openstack_openrc_path": "/root/admin-openrc",
        "ssh_user": "root",
        "ssh_key_path": "/tmp/id_ed25519",
        "ceph_exec_mode": "none",
        "ceph_container_name": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_discover_cinder_volume_maps_attachments_without_exposing_credentials(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cinder_discovery, "resolve_ssh_creds",
        lambda cluster: ("root", "/tmp/id_ed25519", "none", ""),
    )

    def fake_execute(host, command, user=None, key_path=None):
        calls.append((host, command, user, key_path))
        return json.dumps({
            "id": VOLUME_ID, "name": "database", "status": "in-use",
            "project_id": "project-1", "size": 20, "type": "fast",
            "multiattach": False,
            "attachments": [{"attachment_id": "attach-1", "server_id": "vm-1",
                             "host_name": "compute-1", "device": "/dev/vdb"}],
        })

    monkeypatch.setattr(cinder_discovery, "execute_command", fake_execute)
    result = cinder_discovery.discover_cinder_volume(_cluster(), f"volume-{VOLUME_ID}")

    assert result["verified"] is True
    assert result["project_id"] == "project-1"
    assert result["attachments"][0]["instance_id"] == "vm-1"
    assert calls[0][0] == "controller-1"
    assert "openstack volume show" in calls[0][1]
    assert "admin-openrc" in calls[0][1]


def test_build_cinder_mapping_row_classifies_orphan_and_stays_read_only():
    row = cinder_discovery.build_cinder_mapping_row(
        f"volume-{VOLUME_ID}",
        {"pool": "images", "image_id": VOLUME_ID, "provisioned_size": 20, "used_size": 10},
        {"status": "not_found", "verified": True, "volume_id": VOLUME_ID},
    )

    assert row["mapping_status"] == "orphan"
    assert row["management_source"] == "none"
    assert row["read_only"] is True
    assert row["mutation_supported"] is False


def test_discover_cinder_volume_is_fail_closed_for_unconfigured_or_non_cinder_image():
    assert cinder_discovery.discover_cinder_volume(_cluster(), "custom-image")["status"] == "not_cinder"
    result = cinder_discovery.discover_cinder_volume(
        _cluster(openstack_openrc_path=""), f"volume-{VOLUME_ID}"
    )
    assert result["status"] == "not_configured"
    assert result["verified"] is False


def test_discover_cinder_volume_distinguishes_missing_cinder_record(monkeypatch):
    monkeypatch.setattr(
        cinder_discovery, "resolve_ssh_creds",
        lambda cluster: ("root", "/tmp/id_ed25519", "none", ""),
    )
    monkeypatch.setattr(
        cinder_discovery, "execute_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ExecutorError(f"No volume with a name or ID of '{VOLUME_ID}' exists")
        ),
    )

    result = cinder_discovery.discover_cinder_volume(_cluster(), f"volume-{VOLUME_ID}")

    assert result == {"status": "not_found", "verified": True, "volume_id": VOLUME_ID}


def test_discover_cinder_snapshots_normalizes_inventory(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cinder_discovery, "resolve_ssh_creds",
        lambda cluster: ("root", "/tmp/id_ed25519", "none", ""),
    )

    def fake_execute(host, command, user=None, key_path=None):
        calls.append((host, command))
        return json.dumps([
            {"ID": "snap-1", "Name": "daily", "Status": "available", "Size": 20,
             "Created At": "2026-08-17T01:02:03Z"},
        ])

    monkeypatch.setattr(cinder_discovery, "execute_command", fake_execute)
    result = cinder_discovery.discover_cinder_snapshots(_cluster(), VOLUME_ID)

    assert result == {
        "status": "ok", "count": 1,
        "items": [{"snapshot_id": "snap-1", "name": "daily", "status": "available",
                   "size_gib": 20, "created_at": "2026-08-17T01:02:03Z"}],
    }
    assert "openstack volume snapshot list --volume" in calls[0][1]


def test_discover_cinder_snapshots_degrades_independently(monkeypatch):
    monkeypatch.setattr(
        cinder_discovery, "resolve_ssh_creds",
        lambda cluster: ("root", "/tmp/id_ed25519", "none", ""),
    )
    monkeypatch.setattr(
        cinder_discovery, "execute_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(ExecutorError("Cinder timeout")),
    )

    result = cinder_discovery.discover_cinder_snapshots(_cluster(), VOLUME_ID)

    assert result["status"] == "error"
    assert result["items"] == []
    assert "timeout" in result["error"]


@pytest.mark.parametrize(
    ("cinder", "watchers", "locks", "expected"),
    [
        ({"status": "managed", "verified": True, "volume_status": "in-use",
          "multiattach": False, "attachments": [{"attachment_id": "a1"}]}, [{}], [], "healthy"),
        ({"status": "managed", "verified": True, "volume_status": "in-use",
          "multiattach": False, "attachments": [{"attachment_id": "a1"}]}, [], [], "mismatch"),
        ({"status": "managed", "verified": True, "volume_status": "available",
          "multiattach": False, "attachments": []}, [{}], [], "stale_attachment"),
        ({"status": "not_found", "verified": True}, [], [], "orphan"),
        ({"status": "error", "verified": False, "error": "timeout"}, [], [], "unknown"),
        ({"status": "managed", "verified": True, "volume_status": "in-use",
          "multiattach": False, "attachments": [{"attachment_id": "a1"}, {"attachment_id": "a2"}]},
         [{}], [], "mismatch"),
    ],
)
def test_reconcile_cinder_attachment_is_fail_closed(cinder, watchers, locks, expected):
    result = cinder_discovery.reconcile_cinder_attachment(cinder, watchers, locks)

    assert result["status"] == expected
    assert result["safe"] is (expected == "healthy")


def test_attachment_remediation_never_authorizes_direct_lock_removal():
    result = cinder_discovery.build_attachment_remediation(
        {"status": "managed", "verified": True, "attachments": []},
        [{"client": "client.1"}], [{"locker_id": "client.1"}],
        {"status": "stale_attachment", "safe": False,
         "reason": "Cinder không có attachment nhưng Ceph vẫn còn watcher/lock.",
         "evidence": {"cinder_attachment_count": 0}},
    )

    assert result["posture"] == "REVIEW_BEFORE_DETACH"
    assert result["severity"] == "high"
    assert result["automatic_remediation"] is False
    assert result["direct_lock_removal_supported"] is False
    assert result["stale_age_available"] is False
    assert result["read_only"] is True
