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
