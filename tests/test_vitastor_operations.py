import pytest

import vitastor.operations as operations
import vitastor.recovery_ai as recovery_ai


def test_deploy_installs_requested_version(monkeypatch):
    commands = []
    monkeypatch.setattr(operations, "_run", lambda host, user, key, command: commands.append(command) or "ok")
    params = {
        "nodes": [{"host": "node-a", "roles": ["mon"], "disks": []}],
        "version": "3.0.16", "ssh_user": "root", "ssh_key_path": "/key",
        "etcd_prefix": "/vitastor", "osd_network": "10.0.0.0/24", "install_packages": True,
    }

    operations.deploy(params, lambda *_: None)

    assert "apt-get install -y vitastor=3.0.16 etcd" in commands[1]
    assert "https://vitastor.io/debian/pubkey.gpg" in commands[1]
    assert "sources.list.d/vitastor.list" in commands[1]
    assert "make-etcd --copy no" in commands[4]


def test_resume_deploy_reconciles_and_skips_existing_osd_prepare(monkeypatch):
    commands = []
    monkeypatch.setattr(operations, "_run", lambda host, user, key, command: commands.append(command) or "OSD superblock found")
    monkeypatch.setattr(operations, "summarize_deploy_recovery", lambda error, inspection: "Đã đối chiếu xong")
    progress = []
    params = {
        "nodes": [
            {"host": "node-a", "roles": ["mon", "osd"], "disks": ["/dev/vdb"]},
            {"host": "node-b", "roles": ["mon"], "disks": []},
        ],
        "version": "", "ssh_user": "root", "ssh_key_path": "/key",
        "etcd_prefix": "/vitastor", "osd_network": "10.0.0.0/24",
        "install_packages": True, "resume_error": "make-etcd bị treo",
        "resume_progress": [{"id": "packages", "status": "done"}],
    }

    operations.resume_deploy(params, lambda *event: progress.append(event))

    assert any("make-etcd --copy no" in command for command in commands)
    assert any("read-sb --force" in command for command in commands)
    osd_command = next(command for command in commands if "read-sb --force" in command and "systemctl start vitastor.target" in command)
    assert "if test \"$found\" = 0" in osd_command
    assert any(event[0] == "recovery-ai" and event[1] == "done" for event in progress)


def test_recovery_ai_falls_back_when_router_is_not_configured(monkeypatch):
    monkeypatch.setattr(recovery_ai.settings, "vitastor_router_enabled", False)

    result = recovery_ai.summarize_deploy_recovery("package missing", "osd-superblock=found")

    assert result.startswith("AI chưa thể phân tích lúc này")
    assert "package missing" in result


def test_recovery_ai_falls_back_when_router_raises(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise TimeoutError("router timeout")

    monkeypatch.setattr(recovery_ai, "_call_router", fail)
    result = recovery_ai.summarize_deploy_recovery("monitor failed", "monitor=inactive")

    assert result.startswith("AI chưa thể phân tích lúc này")
    assert "monitor failed" in result


def test_install_command_defaults_to_repository_latest():
    command = operations._install_command("")

    assert "apt-get install -y vitastor etcd" in command
    assert "package_manager install -y vitastor etcd" in command


def test_package_resume_check_validates_selected_version():
    command = operations._package_ready_command("3.2.1")

    assert "vitastor-disk --help" in command
    assert "grep -F -- ' 3.2.1'" in command


def test_cluster_verify_requires_all_osds_and_monitors():
    command = operations._cluster_verify_command(3, 3)

    assert "osd_up" in command
    assert "expected_osds" in command
    assert "etcd_alive" in command
    assert '"$status_json"' in command


def test_osd_reconcile_rebuilds_missing_udev_mapping():
    command = operations._osd_reconcile_command("/dev/vdb1")

    assert "vitastor-disk read-sb" in command
    assert "udevadm trigger --subsystem-match=block" in command
    assert "udevadm settle" in command


def test_config_match_requires_service_readable_permissions():
    command = operations._config_matches_command({"etcd_prefix": "/vitastor"})

    assert "stat -c %U /etc/vitastor/vitastor.conf" in command
    assert "stat -c %G /etc/vitastor/vitastor.conf" in command
    assert 'test "$(stat -c %a /etc/vitastor/vitastor.conf)" = 640' in command


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


def test_delete_does_not_swallow_service_control_errors(monkeypatch):
    commands = []
    monkeypatch.setattr(
        operations, "_run",
        lambda host, user, key, command: commands.append(command) or "ok",
    )
    params = {
        "nodes": [{"host": "node-a", "disks": []}], "wipe_disks": False,
        "ssh_user": "root", "ssh_key_path": "/key",
    }

    operations.delete(params, lambda *_: None)

    stop_command = commands[1]
    cleanup_command = commands[2]
    assert "set -eu" in stop_command
    assert "|| true" not in stop_command
    assert "vitastor-osd@*.service" in stop_command
    assert "set -eu" in cleanup_command
    assert "|| true" not in cleanup_command


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


def test_cluster_metadata_backup_is_timestamped_and_contains_recovery_artifacts(monkeypatch):
    commands = []
    monkeypatch.setattr(operations, "_assert_healthy", lambda params: None)
    monkeypatch.setattr(operations, "_run", lambda host, user, key, command: commands.append(command) or "/backup/vitastor/metadata/20260914T093015Z")
    params = {
        "method": "metadata_cluster", "destination": "/backup/vitastor/metadata",
        "management_host": "node-a", "ssh_user": "root", "ssh_key_path": "/key",
        "config_path": "/etc/vitastor/vitastor.conf",
        "etcd_address": "10.0.0.1:2379,10.0.0.2:2379", "etcd_prefix": "/vitastor",
        "exec_mode": "none", "container_name": "",
    }

    operations.backup(params, lambda *_: None)

    preflight = commands[0]
    assert "existing_parent" in preflight
    assert "mkdir -p /backup/vitastor/metadata" in preflight
    assert "test \"$existing_parent\" != /" in preflight
    bundle = commands[-1]
    assert "snapshot save \"$tmp/etcd-snapshot.db\"" in bundle
    assert 'cp -- /etc/vitastor/vitastor.conf "$tmp/vitastor.conf"' in bundle
    for artifact in ("status.json", "df.json", "pools.json", "images.json", "osds.json", "osd-tree.txt", "users.json", "SHA256SUMS"):
        assert f"$tmp/{artifact}" in bundle
    assert 'mv "$tmp" "$final"' in bundle
    assert 'final="$base/${stamp}-$$"' in bundle
    assert "vitastor-disk" not in bundle


def test_qemu_uri_preserves_custom_etcd_prefix():
    uri = operations._qemu_uri({
        "etcd_address": "10.0.0.1:2379", "etcd_prefix": "/custom",
        "config_path": "",
    }, "vm-100@daily")

    assert "etcd_host=10.0.0.1:2379" in uri
    assert "etcd_prefix=/custom" in uri


def test_failed_export_cleans_up_temporary_snapshot(monkeypatch):
    commands = []

    def fake_run(host, user, key, command):
        commands.append(command)
        if "qemu-img convert" in command:
            raise operations.VitastorOperationError("convert failed")
        return "ok"

    monkeypatch.setattr(operations, "_assert_healthy", lambda params: None)
    monkeypatch.setattr(operations, "_run", fake_run)
    params = {
        "method": "full_qcow2", "image": "vm-100", "snapshot": "daily",
        "destination": "/backup/vm-100.qcow2", "backing_file": "",
        "management_host": "node-a", "ssh_user": "root", "ssh_key_path": "/key",
        "config_path": "/etc/vitastor/vitastor.conf", "etcd_address": "",
        "etcd_prefix": "/vitastor", "exec_mode": "none", "container_name": "",
    }

    with pytest.raises(operations.VitastorOperationError, match="convert failed"):
        operations.backup(params, lambda *_: None)

    assert any("rm vm-100@daily" in command for command in commands)


def test_failed_backup_verification_cleans_up_temporary_snapshot(monkeypatch):
    commands = []

    def fake_run(host, user, key, command):
        commands.append(command)
        if command.startswith("test -s"):
            raise operations.VitastorOperationError("empty backup")
        return "ok"

    monkeypatch.setattr(operations, "_assert_healthy", lambda params: None)
    monkeypatch.setattr(operations, "_run", fake_run)
    params = {
        "method": "raw", "image": "vm-100", "snapshot": "daily",
        "destination": "/backup/vm-100.raw",
        "management_host": "node-a", "ssh_user": "root", "ssh_key_path": "/key",
        "config_path": "/etc/vitastor/vitastor.conf", "etcd_address": "",
        "etcd_prefix": "/vitastor", "exec_mode": "none", "container_name": "",
    }

    with pytest.raises(operations.VitastorOperationError, match="empty backup"):
        operations.backup(params, lambda *_: None)

    assert any("rm vm-100@daily" in command for command in commands)
