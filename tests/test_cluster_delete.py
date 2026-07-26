import copy
import json

import pytest

import worker.executor.cluster_deploy as cluster_deploy_module
from worker.executor.cluster_deploy import DeployPhaseError, run
from worker.executor.ssh_executor import ExecutorError

_NODES = [
    {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
    {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disk": "/dev/vdc"},
    {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disk": "/dev/vdb"},
]


def _delete_params(**overrides):
    params = {"nodes": copy.deepcopy(_NODES), "wipe_osd_disks": False}
    params.update(overrides)
    return params


def _make_recording_progress_writer():
    calls = []

    def write_progress(action_pk, progress):
        calls.append((action_pk, copy.deepcopy(progress)))

    return write_progress, calls


def _never_blocked(incident_id):
    return False


# --- delete_cluster_cephadm --------------------------------------------------


def test_delete_cephadm_happy_path_clears_env(monkeypatch):
    def fake(host, command):
        if command == "true":
            return ""
        if "fsids=$(cephadm ls" in command:
            return "Da xoa cum cephadm"
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    written_fields = {}
    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", written_fields.update)

    result = run(
        "action-1", "delete_cluster_cephadm", _delete_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    assert all(step["status"] == "done" for step in calls[-1][1])
    # Delete must CLEAR the cluster config, not populate it with new nodes
    # the way a successful deploy does.
    assert written_fields["CEPH_MON_NODES"] == ""
    assert written_fields["CEPH_MGR_NODES"] == ""
    assert written_fields["CEPH_OSD_NODES"] == ""
    assert written_fields["CEPH_EXEC_MODE"] == "none"


def test_delete_cephadm_appends_zap_osds_flag_when_wipe_requested(monkeypatch):
    seen_commands = []

    def fake(host, command):
        seen_commands.append((host, command))
        if command == "true":
            return ""
        return "Da xoa cum cephadm"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run(
        "action-1",
        "delete_cluster_cephadm",
        _delete_params(wipe_osd_disks=True),
        "incident-1",
        write_progress,
        _never_blocked,
    )

    rm_cluster_cmds = [cmd for _host, cmd in seen_commands if "rm-cluster" in cmd]
    assert rm_cluster_cmds  # at least one was actually sent
    assert all("--zap-osds" in cmd for cmd in rm_cluster_cmds)


def test_delete_cephadm_omits_zap_osds_flag_when_not_requested(monkeypatch):
    seen_commands = []

    def fake(host, command):
        seen_commands.append((host, command))
        if command == "true":
            return ""
        return "Da xoa cum cephadm"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "delete_cluster_cephadm", _delete_params(), "incident-1", write_progress, _never_blocked)

    rm_cluster_cmds = [cmd for _host, cmd in seen_commands if "rm-cluster" in cmd]
    assert rm_cluster_cmds
    assert all("--zap-osds" not in cmd for cmd in rm_cluster_cmds)


def test_delete_cephadm_runs_rm_cluster_on_every_node_not_just_first_mon(monkeypatch):
    """Regression (live-verified 2026-07-26): `cephadm rm-cluster` is
    HOST-LOCAL despite taking a cluster-wide fsid — running it only on
    first_mon left the other 2 nodes' OSD disks completely untouched even
    with wipe_osd_disks=True. Must be sent to EVERY configured node."""
    hosts_with_rm_cluster = set()

    def fake(host, command):
        if command == "true":
            return ""
        if "rm-cluster" in command:
            hosts_with_rm_cluster.add(host)
        return "Da xoa cum cephadm"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run(
        "action-1",
        "delete_cluster_cephadm",
        _delete_params(wipe_osd_disks=True),
        "incident-1",
        write_progress,
        _never_blocked,
    )

    assert hosts_with_rm_cluster == {"10.20.1.112", "10.20.1.95", "10.20.1.21"}


def test_delete_cephadm_fails_when_rm_cluster_fails(monkeypatch):
    def fake(host, command):
        if command == "true":
            return ""
        raise ExecutorError("rm-cluster exploded")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "delete_cluster_cephadm", _delete_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["delete_cephadm"]["status"] == "failed"


def test_delete_cephadm_ssh_check_failure_stops_before_deletion(monkeypatch):
    def fake(host, command):
        if command == "true" and host == "10.20.1.95":
            raise ExecutorError("connection refused")
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "delete_cluster_cephadm", _delete_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["ssh_check"]["status"] == "failed"
    assert steps_by_key["delete_cephadm"]["status"] == "pending"


# --- delete_cluster_manual ---------------------------------------------------


def test_delete_manual_happy_path_stops_daemons_and_removes_state_but_keeps_disks(monkeypatch):
    seen_commands = []

    def fake(host, command):
        seen_commands.append((host, command))
        if command == "true":
            return ""
        if "systemctl list-units" in command:
            return "Da dung: ceph-mon@x.service"
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    written_fields = {}
    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", written_fields.update)

    result = run(
        "action-1", "delete_cluster_manual", _delete_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["stop_daemons"]["status"] == "done"
    assert steps_by_key["remove_state"]["status"] == "done"
    assert steps_by_key["wipe_osd_disk"]["status"] == "done"

    # wipe_osd_disks=False -> ceph-volume must NEVER be invoked.
    assert not any("ceph-volume" in cmd for _host, cmd in seen_commands)
    # /etc/ceph, /var/lib/ceph removal must ALWAYS run regardless of the
    # disk-wipe choice — this is Ceph's own software state, not raw disk data.
    assert any("rm -rf /etc/ceph /var/lib/ceph" in cmd for _host, cmd in seen_commands)

    assert written_fields["CEPH_MON_NODES"] == ""
    assert written_fields["CEPH_EXEC_MODE"] == "none"


def test_delete_manual_wipes_each_osd_nodes_own_disk_when_requested(monkeypatch):
    seen_commands = []

    def fake(host, command):
        seen_commands.append((host, command))
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run(
        "action-1",
        "delete_cluster_manual",
        _delete_params(wipe_osd_disks=True),
        "incident-1",
        write_progress,
        _never_blocked,
    )

    zap_commands = {host: cmd for host, cmd in seen_commands if "ceph-volume lvm zap" in cmd}
    assert "/dev/vdc" in zap_commands["10.20.1.95"]
    assert "/dev/vdb" in zap_commands["10.20.1.21"]


def test_delete_manual_wipe_fails_when_osd_disk_missing_on_a_node(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda host, cmd: "")
    write_progress, calls = _make_recording_progress_writer()

    nodes = copy.deepcopy(_NODES)
    del nodes[1]["osd_disk"]  # 10.20.1.95 now has no osd_disk configured

    result = run(
        "action-1",
        "delete_cluster_manual",
        _delete_params(nodes=nodes, wipe_osd_disks=True),
        "incident-1",
        write_progress,
        _never_blocked,
    )

    assert result is False
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["wipe_osd_disk"]["status"] == "failed"


def test_delete_manual_stops_on_first_host_failure(monkeypatch):
    def fake(host, command):
        if command == "true":
            return ""
        if "systemctl list-units" in command and host == "10.20.1.95":
            raise ExecutorError("systemctl unreachable")
        return "Da dung: (none)"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "delete_cluster_manual", _delete_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    stop_step = steps_by_key["stop_daemons"]
    assert stop_step["status"] == "failed"
    hosts_by_ip = {h["host"]: h["status"] for h in stop_step["hosts"]}
    assert hosts_by_ip["10.20.1.112"] == "done"
    assert hosts_by_ip["10.20.1.95"] == "failed"
    assert hosts_by_ip["10.20.1.21"] == "pending"
    assert steps_by_key["remove_state"]["status"] == "pending"
    assert steps_by_key["wipe_osd_disk"]["status"] == "pending"


def test_delete_manual_env_write_failure_does_not_turn_success_into_failure(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda host, cmd: "")

    def _boom(fields):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", _boom)
    write_progress, _calls = _make_recording_progress_writer()

    result = run(
        "action-1", "delete_cluster_manual", _delete_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True


def test_delete_cluster_action_ids_registered_in_policy():
    from worker.policy.gate import VALID_CLUSTER_DEPLOY_ACTION_IDS

    assert "delete_cluster_cephadm" in VALID_CLUSTER_DEPLOY_ACTION_IDS
    assert "delete_cluster_manual" in VALID_CLUSTER_DEPLOY_ACTION_IDS
