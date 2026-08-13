import pytest

import vitastor.operations as operations


def test_upgrade_is_rolling_and_checks_health_after_each_node(monkeypatch):
    commands = []
    health_checks = []
    monkeypatch.setattr(operations, "_run", lambda host, user, key, command: commands.append((host, command)) or "vitastor 3.0.16")
    monkeypatch.setattr(operations, "_assert_healthy", lambda params: health_checks.append(params["management_host"]))
    progress = []
    params = {
        "nodes": ["node-a", "node-b"], "target_version": "3.0.16",
        "management_host": "node-a", "ssh_user": "root", "ssh_key_path": "/key",
    }
    operations.upgrade(params, lambda *event: progress.append(event))
    upgrades = [(host, command) for host, command in commands if "apt-get update" in command]
    assert [host for host, _ in upgrades] == ["node-a", "node-b"]
    assert len(health_checks) == 4  # preflight, after each node, final verification
    assert progress[-1][0:2] == ("verify", "done")


def test_upgrade_stops_before_next_node_when_health_degrades(monkeypatch):
    upgraded = []
    checks = iter([None, operations.VitastorOperationError("WARNING")])
    monkeypatch.setattr(operations, "_assert_healthy", lambda params: (_ for _ in ()).throw(value) if (value := next(checks)) else None)
    monkeypatch.setattr(operations, "_run", lambda host, user, key, command: upgraded.append(host) if "apt-get update" in command else "3.0.16")
    params = {
        "nodes": ["node-a", "node-b"], "target_version": "3.0.16",
        "management_host": "node-a", "ssh_user": "root", "ssh_key_path": "/key",
    }
    with pytest.raises(operations.VitastorOperationError, match="WARNING"):
        operations.upgrade(params, lambda *_: None)
    assert upgraded == ["node-a"]


def test_full_backup_snapshots_before_qcow2_export(monkeypatch):
    commands = []
    monkeypatch.setattr(operations, "_assert_healthy", lambda params: None)
    monkeypatch.setattr(operations, "_run", lambda host, user, key, command: commands.append(command) or "ok")
    params = {
        "method": "full_qcow2", "image": "vm-100", "snapshot": "daily",
        "destination": "/backup/vm-100.qcow2", "backing_file": "",
        "management_host": "node-a", "ssh_user": "root", "ssh_key_path": "/key",
        "config_path": "/etc/vitastor/vitastor.conf", "etcd_address": "",
        "etcd_prefix": "/vitastor", "exec_mode": "none", "container_name": "",
    }
    operations.backup(params, lambda *_: None)
    snapshot_at = next(i for i, command in enumerate(commands) if "snap-create" in command)
    export_at = next(i for i, command in enumerate(commands) if "qemu-img convert" in command)
    assert snapshot_at < export_at
    assert "vm-100@daily" in commands[export_at]
    assert commands[-1] == "test -s /backup/vm-100.qcow2"


def test_metadata_etcd_backup_uses_snapshot_save(monkeypatch):
    commands = []
    monkeypatch.setattr(operations, "_assert_healthy", lambda params: None)
    monkeypatch.setattr(operations, "_run", lambda host, user, key, command: commands.append(command) or "ok")
    params = {
        "method": "metadata_etcd", "destination": "/backup/etcd.db",
        "management_host": "node-a", "ssh_user": "root", "ssh_key_path": "/key",
        "config_path": "", "etcd_address": "10.0.0.1:2379,10.0.0.2:2379",
        "etcd_prefix": "/vitastor", "exec_mode": "none", "container_name": "",
    }
    operations.backup(params, lambda *_: None)
    assert any("etcdctl --endpoints=10.0.0.1:2379 snapshot save /backup/etcd.db" in command for command in commands)
