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
#
# Regression (live-verified 2026-07-27): the original implementation ran
# `cephadm rm-cluster` only on first_mon, relying on it to tear down the
# WHOLE cluster. Two real problems found live: (1) `cephadm` binary is only
# reliably present on first_mon — every other host's daemons are managed by
# a transient SSH-delivered agent, never leaving a permanently-installed
# CLI a plain SSH session can find, so `command -v cephadm` genuinely fails
# there; (2) even on first_mon itself, `cephadm rm-cluster --force
# --zap-osds` zapped the local OSD disk but left mon/mgr/crash containers
# running — it did not tear down everything. delete_cluster_cephadm now
# shares the EXACT SAME phase list as delete_cluster_manual below (a
# hand-verified-live systemctl-discovery + rm -rf + ceph-volume-zap
# teardown that needs no per-host cephadm binary at all) — see
# _PHASES_BY_ACTION_ID's own comment for the full story.


def test_delete_cephadm_uses_same_phases_as_manual(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda host, cmd: "")
    write_progress, calls = _make_recording_progress_writer()

    written_fields = {}
    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", written_fields.update)

    result = run(
        "action-1", "delete_cluster_cephadm", _delete_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert set(steps_by_key) == {
        "ssh_check",
        "stop_daemons",
        "remove_state",
        "remove_packages",
        "wipe_osd_disk",
    }
    assert all(step["status"] == "done" for step in steps_by_key.values())
    assert written_fields["CEPH_MON_NODES"] == ""
    assert written_fields["CEPH_EXEC_MODE"] == "none"


def test_delete_cephadm_wipes_each_osd_nodes_own_disk_when_requested(monkeypatch):
    seen_commands = []

    def fake(host, command):
        seen_commands.append((host, command))
        return ""

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

    zap_commands = {host: cmd for host, cmd in seen_commands if "ceph-volume lvm zap" in cmd}
    assert "/dev/vdc" in zap_commands["10.20.1.95"]
    assert "/dev/vdb" in zap_commands["10.20.1.21"]
    # 2026-07-28 fix: the wipe command DOES now reference "cephadm" (a
    # fallback branch for when native ceph-volume isn't installed — see
    # test_delete_cephadm_wipe_falls_back_to_cephadm_ceph_volume below) —
    # this codebase previously asserted the opposite here, which was only
    # ever true because the fallback didn't exist yet, not because
    # `cephadm` involvement was undesirable.
    assert any("cephadm ceph-volume" in cmd for host, cmd in zap_commands.items())


def test_delete_cephadm_wipe_uses_native_ceph_volume_when_present(monkeypatch):
    """When native ceph-volume IS on PATH (e.g. a cephadm cluster where the
    operator also happens to have ceph-osd installed), the wipe command
    must prefer it over the cephadm fallback — both work, but native is
    simpler/faster and doesn't depend on cephadm auto-detecting the fsid."""

    def fake(host, command):
        if "command -v ceph-volume" in command:
            return ""  # exit 0 == found, handled by the shell `if` itself
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    run(
        "action-1",
        "delete_cluster_cephadm",
        _delete_params(wipe_osd_disks=True),
        "incident-1",
        write_progress,
        _never_blocked,
    )

    final = calls[-1][1]
    steps_by_key = {s["step"]: s for s in final}
    assert steps_by_key["wipe_osd_disk"]["status"] == "done"


def test_delete_cephadm_wipe_runs_before_remove_state(monkeypatch):
    """2026-07-28 fix: unlike delete_cluster_manual, delete_cluster_cephadm
    must wipe OSD disks BEFORE remove_state — the cephadm fallback
    (`cephadm ceph-volume -- ...`) auto-detects the cluster's fsid from
    `/var/lib/ceph/<fsid>`, which remove_state deletes."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return ""

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

    zap_index = next(i for i, cmd in enumerate(seen_commands) if "ceph-volume lvm zap" in cmd)
    remove_state_index = next(
        i for i, cmd in enumerate(seen_commands) if "rm -rf /etc/ceph /var/lib/ceph" in cmd
    )
    assert zap_index < remove_state_index


def test_delete_cephadm_ssh_check_failure_stops_before_teardown(monkeypatch):
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
    assert steps_by_key["stop_daemons"]["status"] == "pending"


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
    assert steps_by_key["remove_packages"]["status"] == "done"
    assert steps_by_key["wipe_osd_disk"]["status"] == "done"

    # wipe_osd_disks=False -> ceph-volume must NEVER be invoked.
    assert not any("ceph-volume" in cmd for _host, cmd in seen_commands)
    # /etc/ceph, /var/lib/ceph removal must ALWAYS run regardless of the
    # disk-wipe choice — this is Ceph's own software state, not raw disk data.
    assert any("rm -rf /etc/ceph /var/lib/ceph" in cmd for _host, cmd in seen_commands)
    # Package removal must ALWAYS run too — same reasoning, plus (2026-07-27
    # regression) a node still carrying an earlier cluster's packages blocks
    # a later "Dựng cụm" at a different major version outright.
    assert any("dnf remove -y" in cmd for _host, cmd in seen_commands)
    # "xoá cho kĩ", 2026-07-27: /tmp scratch generation files (a STALE one
    # of these made a later deploy attempt's monmaptool fail outright) and
    # this app's own added Ceph repo files must also be cleaned, so a node
    # is genuinely reusable for a fresh "Dựng cụm" afterward.
    assert any("/tmp/ceph-aiops*" in cmd for _host, cmd in seen_commands)
    assert any("download.ceph.com_rpm-*.repo" in cmd for _host, cmd in seen_commands)
    # 2026-07-29, "xoá mọi thứ liên quan tới Ceph": /usr/local/bin/cephadm
    # (a curl-downloaded script, never package-managed, so remove_packages'
    # dnf/apt glob can never touch it), /var/log/ceph, and cephadm's own
    # leftover systemd unit FILES + a daemon-reload so systemd actually
    # forgets them immediately.
    assert any("/usr/local/bin/cephadm" in cmd for _host, cmd in seen_commands)
    assert any("/var/log/ceph" in cmd for _host, cmd in seen_commands)
    assert any("/etc/systemd/system/ceph-*.target" in cmd for _host, cmd in seen_commands)
    assert any("/etc/systemd/system/ceph-*@*.service*" in cmd for _host, cmd in seen_commands)
    assert any("systemctl daemon-reload" in cmd for _host, cmd in seen_commands)

    assert written_fields["CEPH_MON_NODES"] == ""
    assert written_fields["CEPH_EXEC_MODE"] == "none"


def test_delete_manual_remove_packages_uses_globs_and_tolerates_nothing_installed(monkeypatch):
    """Regression, 2026-07-27 (verified live): a node still carrying an
    earlier cluster's Ceph packages (e.g. Quincy 17.2.7, left behind by a
    prior deploy that was later "Xoá cụm"-ed — _phase_delete_manual_remove_state
    only ever removed /etc/ceph, /var/lib/ceph, never the packages
    themselves) makes a LATER "Dựng cụm" at a DIFFERENT major version fail
    with a dnf NEVRA conflict ("cannot install both ceph-mgr-2:14.2.22...
    and ceph-mgr-2:17.2.7... from @System"). This phase must actually
    uninstall the Ceph package family so "Xoá cụm" really does leave the
    node clean, and must tolerate a node with nothing Ceph-related
    installed at all (glob matches nothing -> dnf would otherwise exit
    non-zero)."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return "no packages installed matching pattern"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)

    nodes = [{"ip": "10.20.1.112", "roles": ["mon", "mgr", "osd"]}]
    cluster_deploy_module._phase_delete_manual_remove_packages(nodes, {}, lambda status: None)

    command = seen_commands[0]
    assert "dnf remove -y 'ceph*'" in command
    assert "librados*" in command
    assert "libcephfs*" in command
    assert "librbd*" in command
    assert "|| true)" in command  # nothing installed to remove must not fail this phase
    # Never touch epel-release — general-purpose repo, not Ceph-specific.
    assert "epel-release" not in command


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


def test_delete_manual_wipes_osd_disk_before_removing_packages(monkeypatch):
    """Regression, 2026-07-28 (live-verified): wipe_osd_disk was originally
    ordered AFTER remove_packages — `ceph-volume lvm zap --destroy` is
    provided by the very packages remove_packages uninstalls, so by the
    time wipe_osd_disk ran, the tool it needs was already gone
    ("bash: ceph-volume: command not found"). wipe_osd_disk must run while
    ceph-volume is still installed; remove_packages goes last."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
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

    zap_index = next(i for i, cmd in enumerate(seen_commands) if "ceph-volume lvm zap" in cmd)
    remove_packages_index = next(i for i, cmd in enumerate(seen_commands) if "dnf remove -y" in cmd)
    assert zap_index < remove_packages_index


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
