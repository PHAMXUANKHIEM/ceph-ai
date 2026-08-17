import json
from types import SimpleNamespace

from dashboard import cinder_discovery


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
