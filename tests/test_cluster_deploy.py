import base64
import copy
import json

import pytest

import worker.executor.cluster_deploy as cluster_deploy_module
from worker.executor.cluster_deploy import CLUSTER_DEPLOY_ACTION_IDS, DeployPhaseError, run
from worker.executor.ssh_executor import ExecutorError

_ROCKY_OS_RELEASE = 'ID="rocky"\nVERSION_ID="9.3"\nPRETTY_NAME="Rocky Linux 9.3"\n'
_UBUNTU_OS_RELEASE = 'ID="ubuntu"\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04.3 LTS"\n'
_UNSUPPORTED_OS_RELEASE = 'ID="alpine"\nVERSION_ID="3.19"\nPRETTY_NAME="Alpine Linux"\n'

_NODES = [
    {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
    # Different disk names per node (node1 /dev/vdc, node2 /dev/vdb) — the
    # whole point of per-node osd_disk instead of one cluster-wide value.
    {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disk": "/dev/vdc"},
    {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disk": "/dev/vdb"},
]


def _cephadm_params(**overrides):
    params = {"version": "18.2.8", "nodes": copy.deepcopy(_NODES)}
    params.update(overrides)
    return params


def _default_fake_execute(host, command):
    if command == "true":
        return ""
    if command == "cat /etc/os-release":
        return _ROCKY_OS_RELEASE
    if "blkid" in command:
        return ""  # empty output == no filesystem/LVM signature == safe
    if "lsblk" in command:
        return ""  # empty output == not mounted
    if command.startswith("hostname"):
        return host.replace(".", "-") + ".lab"
    if "ceph -s --format json" in command:
        return json.dumps({"health": {"status": "HEALTH_OK"}})
    return ""


def _make_recording_progress_writer():
    calls = []

    def write_progress(action_pk, progress):
        calls.append((action_pk, copy.deepcopy(progress)))

    return write_progress, calls


def _never_blocked(incident_id):
    return False


# --- ssh_check phase: the safety-critical phase ---------------------------


def test_ssh_check_fails_on_unreachable_host(monkeypatch):
    def fake(host, command):
        if command == "true" and host == "10.20.1.95":
            raise ExecutorError("connection refused")
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    last_action_pk, last_progress = calls[-1]
    assert last_progress[0]["status"] == "failed"
    assert "10.20.1.95" in last_progress[0]["message"]
    # No later phase's status ever left "pending" -> "running": only the
    # ssh_check step should have moved past pending.
    assert all(step["status"] == "pending" for step in last_progress[1:])


def test_ssh_check_fails_on_unrecognized_os_family(monkeypatch):
    def fake(host, command):
        if command == "cat /etc/os-release":
            return _UNSUPPORTED_OS_RELEASE
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    assert "không được hỗ trợ" in calls[-1][1][0]["message"]


def test_ssh_check_accepts_debian_family_os(monkeypatch):
    # Regression guard: apt-family distros must not be rejected by the
    # OS-family check the same way an unsupported one is.
    def fake(host, command):
        if command == "cat /etc/os-release":
            return _UBUNTU_OS_RELEASE
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    assert calls[-1][1][0]["status"] == "done"


def test_ssh_check_fails_when_osd_disk_missing_as_block_device(monkeypatch):
    def fake(host, command):
        if command.startswith("test -b") and host == "10.20.1.95":
            raise ExecutorError("no such device")
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    assert "không tồn tại hoặc không phải là block device" in calls[-1][1][0]["message"]


def test_ssh_check_fails_when_osd_disk_already_has_data(monkeypatch):
    def fake(host, command):
        if "blkid" in command and host == "10.20.1.95":
            return "/dev/vdc: TYPE=\"ext4\""
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    assert "đã có dữ liệu" in calls[-1][1][0]["message"]


def test_ssh_check_fails_when_osd_disk_is_mounted(monkeypatch):
    def fake(host, command):
        if "lsblk" in command and host == "10.20.1.95":
            return "/mnt/data"
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    assert "đang được mount" in calls[-1][1][0]["message"]


def test_ssh_check_failure_sends_no_command_to_any_later_phase(monkeypatch):
    """The single most important safety property of this feature: if the
    system check fails on even one node, NO package/mkfs/orch command may
    ever be sent to ANY node."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        if "blkid" in command and host == "10.20.1.95":
            return "/dev/vdc: TYPE=\"ext4\""
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    forbidden_substrings = ["cephadm bootstrap", "orch host add", "orch apply", "orch daemon add osd"]
    assert not any(
        forbidden in cmd for cmd in seen_commands for forbidden in forbidden_substrings
    )


def test_osd_disk_not_checked_on_non_osd_nodes(monkeypatch):
    # 10.20.1.112 has roles ["mon", "mgr"] only — its disk must never be
    # touched/queried by the safety check.
    checked_hosts = []

    def fake(host, command):
        if "blkid" in command or "lsblk" in command:
            checked_hosts.append(host)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    assert "10.20.1.112" not in checked_hosts


# --- Framework: kill-switch, ordering, stop-on-first-failure --------------


def test_kill_switch_blocks_before_first_phase(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _default_fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, lambda inc: True
    )

    assert result is False
    assert calls[-1][1][0]["status"] == "failed"
    assert "Kill-switch" in calls[-1][1][0]["message"]
    assert all(step["status"] == "pending" for step in calls[-1][1][1:])


def test_kill_switch_checked_fresh_before_each_phase(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _default_fake_execute)
    write_progress, _calls = _make_recording_progress_writer()

    call_count = {"n": 0}

    def check_kill_switch(incident_id):
        call_count["n"] += 1
        # Block on the 3rd phase (orch_host_add) — after ssh_check and
        # bootstrap already ran once each.
        return call_count["n"] >= 3

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, check_kill_switch
    )

    assert result is False
    assert call_count["n"] == 3


def test_stops_at_first_phase_failure_does_not_run_later_phases(monkeypatch):
    def fake(host, command):
        if "cephadm bootstrap" in command:
            raise ExecutorError("bootstrap failed")
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["ssh_check"]["status"] == "done"
    assert steps_by_key["dependencies"]["status"] == "done"
    assert steps_by_key["bootstrap"]["status"] == "failed"
    assert all(
        step["status"] == "pending"
        for key, step in steps_by_key.items()
        if key not in ("ssh_check", "dependencies", "bootstrap")
    )


def test_verify_phase_fails_on_health_err(monkeypatch):
    def fake(host, command):
        if "ceph -s --format json" in command:
            return json.dumps({"health": {"status": "HEALTH_ERR"}})
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    assert calls[-1][1][-1]["status"] == "failed"
    assert "HEALTH_ERR" in calls[-1][1][-1]["message"]


# --- Happy path + .env write -----------------------------------------------


def test_cephadm_happy_path_all_phases_succeed_and_writes_env(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _default_fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    written_fields = {}
    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", written_fields.update)

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    assert all(step["status"] == "done" for step in calls[-1][1])
    assert written_fields["CEPH_MON_NODES"] == "10.20.1.112,10.20.1.95,10.20.1.21"
    assert written_fields["CEPH_MGR_NODES"] == "10.20.1.112,10.20.1.95"
    assert written_fields["CEPH_OSD_NODES"] == "10.20.1.95,10.20.1.21"
    assert written_fields["CEPH_EXEC_MODE"] == "cephadm"


def test_env_write_failure_does_not_turn_a_successful_deploy_into_a_failure(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _default_fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    def raise_io_error(fields):
        raise OSError("disk full")

    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", raise_io_error)

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    assert all(step["status"] == "done" for step in calls[-1][1])


def test_cephadm_bootstrap_ensures_python3_before_invoking_cephadm(monkeypatch):
    """Regression (live-verified 2026-07-26): the downloaded `cephadm`
    script is itself a `#!/usr/bin/python3` file — a node with no
    /usr/bin/python3 made the very first cephadm invocation fail with exit
    126 "bad interpreter", before cephadm's own `install` subcommand ever
    ran. A python3-ensuring command must be sent, and BEFORE the cephadm
    install/bootstrap command, not after."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    python3_ensure_index = next(
        i for i, cmd in enumerate(seen_commands) if "python3" in cmd
    )
    cephadm_install_index = next(
        i for i, cmd in enumerate(seen_commands) if "cephadm add-repo" in cmd
    )
    assert python3_ensure_index < cephadm_install_index
    assert "apt-get install -y python3" in seen_commands[python3_ensure_index]


def test_cephadm_ensures_python3_on_every_node_not_just_first_mon(monkeypatch):
    """Regression (live-verified 2026-07-26): `ceph orch host add` failed
    with "no python3 in (...)" for the SECOND node added, even though
    first_mon itself already had python3 — cephadm's per-host management
    agent is itself a python3 script the ORCHESTRATOR runs via SSH on
    EVERY host it manages, not just first_mon. The python3-ensuring
    command used to live only inside _phase_cephadm_bootstrap (first_mon
    only) — it must be sent to every node, via the shared `dependencies`
    phase which already loops over all of them."""
    hosts_with_python3_ensure = set()

    def fake(host, command):
        if "python3" in command:
            hosts_with_python3_ensure.add(host)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    assert hosts_with_python3_ensure == {"10.20.1.112", "10.20.1.95", "10.20.1.21"}


def test_cephadm_bootstrap_passes_allow_fqdn_hostname_flag(monkeypatch):
    """Regression (live-verified 2026-07-26): cephadm bootstrap hard-refuses
    (exit 1, "hostname is a fully qualified domain name") on a node whose
    `hostname` command returns an FQDN (common on OpenStack-provisioned
    VMs) unless --allow-fqdn-hostname is passed."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    bootstrap_cmd = next(cmd for cmd in seen_commands if "cephadm bootstrap --mon-ip" in cmd)
    assert "--allow-fqdn-hostname" in bootstrap_cmd


def test_cephadm_bootstrap_passes_allow_overwrite_flag(monkeypatch):
    """Regression (live-verified 2026-07-26): cephadm bootstrap hard-refuses
    (exit 1, "/etc/ceph/ceph.conf already exists") if a PREVIOUS deploy
    attempt on the same node got as far as writing that file before failing
    later — expected during iterative testing/retries against the same
    lab node, not just a one-off fluke."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    bootstrap_cmd = next(cmd for cmd in seen_commands if "cephadm bootstrap --mon-ip" in cmd)
    assert "--allow-overwrite" in bootstrap_cmd


def test_cephadm_bootstrap_cleans_up_previous_attempt_before_rebootstrapping(monkeypatch):
    """Regression (live-verified 2026-07-26): a MON container left running
    from an earlier, partially-failed deploy attempt on the same node still
    held the MSGR v2 port ("Cannot bind to IP ... port 3300: Address
    already in use") even after --allow-overwrite fixed the ceph.conf-
    exists error. Must run `cephadm rm-cluster --force` for any existing
    fsid BEFORE the bootstrap command — and must never pass --zap-osds
    (that would destructively touch the OSD disk, out of scope for this
    cleanup)."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    cleanup_index = next(i for i, cmd in enumerate(seen_commands) if "rm-cluster" in cmd)
    bootstrap_index = next(i for i, cmd in enumerate(seen_commands) if "cephadm bootstrap --mon-ip" in cmd)
    assert cleanup_index < bootstrap_index
    assert "--zap-osds" not in seen_commands[cleanup_index]


def test_cephadm_deploy_installs_and_starts_chrony_before_bootstrap(monkeypatch):
    """Regression (live-verified 2026-07-26): cephadm bootstrap's own
    preflight check ("No time sync service is running") failed on a fresh
    node even after chrony was installed, because the dependencies phase
    only installed the package without ever starting its service — the
    cephadm method didn't even run the dependencies phase at all before
    this fix. Must run before `bootstrap`, and must explicitly enable+start
    the service (not just install the package)."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    chrony_index = next(i for i, cmd in enumerate(seen_commands) if "chrony" in cmd)
    bootstrap_index = next(i for i, cmd in enumerate(seen_commands) if "cephadm bootstrap --mon-ip" in cmd)
    assert chrony_index < bootstrap_index
    assert "systemctl enable --now chrony" in seen_commands[chrony_index]
    assert "systemctl enable --now chronyd" in seen_commands[chrony_index]


def test_ceph_deploy_dependencies_clears_stale_ceph_repo_before_installing_chrony(monkeypatch):
    """Regression, 2026-07-27 (live-verified): `dependencies` runs BEFORE
    `repo` in every method's phase list — on a retry after a previous
    attempt's `repo` phase already added a download.ceph.com_rpm-*.repo
    file that later turned out broken (e.g. a 404 for an unrecognized/typo'd
    version), that stale file is STILL enabled here, and dnf/yum refuse to
    do anything (even installing chrony, completely unrelated) while any
    enabled repo fails metadata refresh. Must clear it before attempting
    dnf/yum install, same defensive rm -f the `repo` phase's own command
    already does. Calls the phase function directly (not the full run()
    pipeline) — later phases (mon_init/wait_quorum/...) need real quorum
    responses this test doesn't care about providing."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)

    cluster_deploy_module._phase_ceph_deploy_dependencies(
        copy.deepcopy(_NODES), {}, lambda status: None
    )

    command = next(cmd for cmd in seen_commands if "chrony" in cmd)
    cleanup_pos = command.index("rm -f /etc/yum.repos.d/download.ceph.com_rpm-*.repo")
    install_pos = command.index("dnf install -y chrony")
    assert cleanup_pos < install_pos


def test_cephadm_bootstrap_installs_ceph_common_after_bootstrap(monkeypatch):
    """Regression (live-verified 2026-07-26): `cephadm bootstrap` alone
    leaves `ceph` reachable only via the containerized `cephadm shell`
    wrapper, not directly on PATH — every later phase (orch_host_add/
    orch_apply_mgr/orch_apply_osd/verify) calls `ceph ...` directly and
    failed with "ceph: command not found" (exit 127) right after a
    successful bootstrap. Must install ceph-common AFTER bootstrap
    succeeds, not before (the repo it installs from is only added as part
    of the bootstrap command itself)."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    bootstrap_index = next(i for i, cmd in enumerate(seen_commands) if "cephadm bootstrap --mon-ip" in cmd)
    ceph_common_index = next(i for i, cmd in enumerate(seen_commands) if "ceph-common" in cmd)
    assert bootstrap_index < ceph_common_index


def test_cephadm_add_repo_uses_exact_version_not_release_codename(monkeypatch):
    """cephadm's OWN internal `add-repo --release <codename>` command (used
    to install the `cephadm` package itself) uses `--version <exact
    version>` instead, matching the fix already applied to
    _phase_ceph_deploy_repo's rolling-alias bug in general — even though
    this specific swap did NOT turn out to fix ceph-common's own
    availability (see test_cephadm_ensures_ceph_common_via_own_repo_command
    below for what did)."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    add_repo_cmd = next(cmd for cmd in seen_commands if "cephadm add-repo" in cmd)
    assert "add-repo --version 18.2.8" in add_repo_cmd
    assert "--release" not in add_repo_cmd


def test_build_ceph_package_repo_command_nautilus_uses_codename_not_exact_version():
    """Regression, 2026-07-27: verified live against download.ceph.com —
    unlike every later release (which this command correctly targets by
    exact version — see test_cephadm_add_repo_uses_exact_version_not_release_codename
    above), Nautilus (14.x) was NEVER published under a per-exact-version
    directory at all (rpm-14.2.22/el8/ -> 404) — only the
    rpm-nautilus/debian-nautilus codename alias exists, safe to use forever
    since Nautilus is long EOL."""
    command = cluster_deploy_module._build_ceph_package_repo_command("14.2.22")

    assert "debian-nautilus/" in command
    assert "rpm-nautilus/el$(rpm -E %rhel)/" in command
    assert "14.2.22" not in command


def test_cephadm_ensures_ceph_common_via_own_repo_command(monkeypatch):
    """Regression (live-verified 2026-07-26): `cephadm install ceph-common`
    left `ceph-common` unfindable via yum TWICE in a row — once with
    cephadm's own `add-repo --release <codename>`, and again after
    switching that to `--version <exact version>` (previous test above).
    Rather than keep guessing at cephadm's internal repo-URL logic, this
    method now sets up the repo itself via `_build_ceph_package_repo_command`
    (the SAME already-written command `_phase_ceph_deploy_repo` uses, which
    detects the RHEL major version and arch AT RUNTIME via `rpm -E %rhel`/
    `uname -m` rather than hardcoding one) and installs ceph-common by
    plain `dnf/yum install -y ceph-common`, run AFTER bootstrap succeeds."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    bootstrap_index = next(i for i, cmd in enumerate(seen_commands) if "cephadm bootstrap --mon-ip" in cmd)
    repo_index = next(i for i, cmd in enumerate(seen_commands) if "rpm -E %rhel" in cmd)
    ceph_common_index = next(
        i for i, cmd in enumerate(seen_commands) if "install -y ceph-common" in cmd
    )
    assert bootstrap_index < repo_index < ceph_common_index
    assert not any("cephadm install ceph-common" in cmd for cmd in seen_commands)


def test_cephadm_orch_host_add_authorizes_cephadm_pubkey_before_adding_each_host(monkeypatch):
    """Regression (live-verified 2026-07-26): `ceph orch host add` failed
    with "Permission denied" for the first non-first-mon host — cephadm's
    own orchestrator SSHes from first_mon to every other host using a
    DEDICATED keypair it generates during bootstrap (/etc/ceph/ceph.pub),
    which had never been authorized on that host. Must read that pubkey
    from first_mon and push it into EACH other host's
    /root/.ssh/authorized_keys (via the Worker's own already-proven SSH
    access to that host) BEFORE calling `ceph orch host add` for it."""
    cephadm_pubkey = "ssh-ed25519 AAAAC3Nz-fake-cephadm-key cephadm"
    seen_commands = []

    def fake(host, command):
        seen_commands.append((host, command))
        if command == "cat /etc/ceph/ceph.pub":
            return cephadm_pubkey + "\n"
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    # 10.20.1.112 is first_mon (per _NODES) — only the other two hosts get
    # the pubkey pushed to them, each BEFORE their own `orch host add` call.
    for ip in ("10.20.1.95", "10.20.1.21"):
        authorize_index = next(
            i
            for i, (host, cmd) in enumerate(seen_commands)
            if host == ip and cephadm_pubkey in cmd and "authorized_keys" in cmd
        )
        host_add_index = next(
            i
            for i, (host, cmd) in enumerate(seen_commands)
            if host == "10.20.1.112" and "ceph orch host add" in cmd and ip in cmd
        )
        assert authorize_index < host_add_index


def test_no_command_sent_with_all_available_devices_flag(monkeypatch):
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    assert not any("--all-available-devices" in cmd for cmd in seen_commands)
    # Each OSD node uses its OWN disk (10.20.1.95 -> /dev/vdc, 10.20.1.21 ->
    # /dev/vdb) — proves osd_disk is read per node, not one cluster-wide value.
    assert any("orch daemon add osd" in cmd and "/dev/vdc" in cmd for cmd in seen_commands)
    assert any("orch daemon add osd" in cmd and "/dev/vdb" in cmd for cmd in seen_commands)


# --- ceph-deploy method (Story 8.2) ----------------------------------------


def _ceph_deploy_fake_execute(host, command):
    """Extends _default_fake_execute with the ceph-deploy-only commands:
    base64 file reads (mon/admin/bootstrap-osd keyrings, monmap, per-host mgr
    keyring — content doesn't matter, execute_command is fully mocked so
    nothing ever really decodes it) and `ceph quorum_status`, which must
    report every configured MON node as being in quorum for the happy path.
    """
    if command.startswith("base64 "):
        return base64.b64encode(b"fake-binary-content").decode()
    if "quorum_status" in command:
        mon_hostnames = [n["ip"].replace(".", "-") + ".lab" for n in _NODES if "mon" in n["roles"]]
        return json.dumps({"quorum_names": mon_hostnames})
    return _default_fake_execute(host, command)


def test_ceph_deploy_happy_path_installs_role_specific_packages_and_writes_env(monkeypatch):
    seen_install_commands: dict[str, list[str]] = {}

    def fake(host, command):
        if "install -y" in command and any(pkg in command for pkg in ("ceph-mon", "ceph-mgr", "ceph-osd")):
            seen_install_commands.setdefault(host, []).append(command)
        return _ceph_deploy_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_POLL_INTERVAL_SECONDS", 0)
    write_progress, calls = _make_recording_progress_writer()

    written_fields = {}
    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", written_fields.update)

    result = run(
        "action-1", "deploy_cluster_ceph_deploy", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    assert all(step["status"] == "done" for step in calls[-1][1])

    # 10.20.1.112 has roles mon+mgr only -> ceph-osd must never be installed
    # there; 10.20.1.95 has all three roles -> all three packages.
    assert "ceph-mon" in seen_install_commands["10.20.1.112"][0]
    assert "ceph-mgr" in seen_install_commands["10.20.1.112"][0]
    assert "ceph-osd" not in seen_install_commands["10.20.1.112"][0]
    assert all(pkg in seen_install_commands["10.20.1.95"][0] for pkg in ("ceph-mon", "ceph-mgr", "ceph-osd"))

    assert written_fields["CEPH_EXEC_MODE"] == "none"
    assert written_fields["CEPH_MON_NODES"] == "10.20.1.112,10.20.1.95,10.20.1.21"


def test_ceph_deploy_mon_init_clears_stale_scratch_files_before_generating(monkeypatch):
    """Regression, 2026-07-27 (live-verified): monmaptool --create refuses
    to overwrite an existing /tmp/ceph-aiops.monmap ("--clobber to
    overwrite") — these /tmp scratch files are only ever used transiently
    within this phase and were never cleaned up afterward, so a retry of a
    failed/aborted deploy attempt always hit this on the SAME node, even
    one "Xoá cụm" had already torn down (that feature only ever cleaned
    real Ceph state, /etc/ceph and /var/lib/ceph, never these transient
    generation-time files). Must clean its own scratch files first so this
    phase is idempotent across retries."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _ceph_deploy_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)

    cluster_deploy_module._phase_ceph_deploy_mon_init(
        copy.deepcopy(_NODES), _cephadm_params(), lambda status: None
    )

    keygen_command = next(cmd for cmd in seen_commands if "monmaptool --create" in cmd)
    rm_pos = keygen_command.index("rm -f")
    monmaptool_pos = keygen_command.index("monmaptool --create")
    assert rm_pos < monmaptool_pos
    assert "ceph-aiops-mon.keyring" in keygen_command[rm_pos:monmaptool_pos]
    assert "ceph-aiops.monmap" in keygen_command[rm_pos:monmaptool_pos]


def test_ceph_deploy_mon_security_enables_msgr2_and_disables_insecure_reclaim(monkeypatch):
    """2026-07-27, operator request after seeing these on a real deployed
    cluster: _phase_ceph_deploy_mon_init's monmaptool generates a v1-only
    monmap, so every cluster built via ceph-deploy/rpm-local previously
    started already reporting MON_MSGR2_NOT_ENABLED +
    AUTH_INSECURE_GLOBAL_ID_RECLAIM_ALLOWED health warnings — this app's own
    Watcher flagged both as investigate_manually incidents (verified live)
    against a real cluster built this way, requiring manual `ceph mon
    enable-msgr2` + `ceph config set ...` after every single deploy. Now
    run automatically, right after quorum, via the first MON node."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append((host, command))
        return _ceph_deploy_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)

    cluster_deploy_module._phase_ceph_deploy_mon_security(
        copy.deepcopy(_NODES), {}, lambda status: None
    )

    first_mon_ip = _NODES[0]["ip"]
    host, command = next(
        (h, c) for h, c in seen_commands if "enable-msgr2" in c
    )
    assert host == first_mon_ip
    assert "ceph mon enable-msgr2" in command
    assert "ceph config set mon auth_allow_insecure_global_id_reclaim false" in command


def test_ceph_deploy_mon_security_failure_raises_deploy_phase_error(monkeypatch):
    def fake(host, command):
        if "enable-msgr2" in command:
            raise ExecutorError(f"{host}: boom")
        return _ceph_deploy_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)

    with pytest.raises(DeployPhaseError, match="msgr2"):
        cluster_deploy_module._phase_ceph_deploy_mon_security(
            copy.deepcopy(_NODES), {}, lambda status: None
        )


def test_ceph_deploy_packages_pins_exact_version_for_nautilus(monkeypatch):
    """Regression, 2026-07-27 (verified live): rpm-nautilus/ (the codename
    alias _build_ceph_package_repo_command falls back to for Nautilus, see
    that function's docstring) physically hosts EVERY Nautilus point
    release side by side and advertises all of them in its repodata — a
    bare `dnf install ceph-mon` there silently resolves to whichever is
    numerically newest, regardless of which point release was actually
    requested. Must pin the exact version in the package name for RPM
    installs whenever that fallback kicked in. Calls the phase function
    directly — same reasoning as the dependencies-phase regression test
    above, no need to drive the whole run() pipeline through quorum/mgr/osd
    just to check one install command's package names."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)

    nodes = [{"ip": "10.20.1.112", "roles": ["mon", "mgr", "osd"]}]
    cluster_deploy_module._phase_ceph_deploy_packages(
        nodes, {"version": "14.2.22"}, lambda status: None
    )

    command = seen_commands[0]
    assert "dnf install -y ceph-mgr-14.2.22 ceph-mon-14.2.22 ceph-osd-14.2.22" in command
    assert "apt-get install -y ceph-mgr ceph-mon ceph-osd" in command  # apt never pins — no ambiguity there


def test_ceph_deploy_packages_does_not_pin_version_for_normal_releases(monkeypatch):
    """Every release except Nautilus already has a repo scoped to exactly
    one version (see _build_ceph_package_repo_command) — a bare package
    name there already resolves unambiguously, so pinning is unnecessary
    (and would be one more way to get the exact NEVRA string wrong for no
    benefit)."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)

    nodes = [{"ip": "10.20.1.112", "roles": ["mon"]}]
    cluster_deploy_module._phase_ceph_deploy_packages(
        nodes, {"version": "18.2.8"}, lambda status: None
    )

    command = seen_commands[0]
    assert "(dnf install -y ceph-mon || yum install -y ceph-mon)" in command
    assert "18.2.8" not in command


def test_ceph_deploy_quorum_timeout_fails(monkeypatch):
    def fake(host, command):
        if command.startswith("base64 "):
            return base64.b64encode(b"x").decode()
        if "quorum_status" in command:
            # Only one MON ever reports into quorum_names — never reaches
            # the 3 this cluster's _NODES configures.
            return json.dumps({"quorum_names": ["10-20-1-112.lab"]})
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    params = _cephadm_params(quorum_timeout_seconds=0)
    result = run(
        "action-1", "deploy_cluster_ceph_deploy", params, "incident-1", write_progress, _never_blocked
    )

    assert result is False
    final_progress = calls[-1][1]
    steps_by_key = {s["step"]: s for s in final_progress}
    assert steps_by_key["mon_init"]["status"] == "done"
    assert steps_by_key["wait_quorum"]["status"] == "failed"
    assert "quorum" in steps_by_key["wait_quorum"]["message"].lower()
    assert steps_by_key["mgr_create"]["status"] == "pending"
    assert steps_by_key["osd_create"]["status"] == "pending"


def test_ceph_deploy_stops_on_first_host_failure_in_packages_phase(monkeypatch):
    """The single most important safety property of AC #4: unlike the
    package-based Cluster Upgrade path (which keeps going to the next host
    on a per-host failure), a deploy must stop at the FIRST failing host and
    never touch the ones after it."""

    def fake(host, command):
        if host == "10.20.1.95" and "install -y" in command and "ceph-" in command:
            raise ExecutorError("dnf transaction failed")
        return _ceph_deploy_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_ceph_deploy", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["ssh_check"]["status"] == "done"
    assert steps_by_key["dependencies"]["status"] == "done"
    assert steps_by_key["repo"]["status"] == "done"

    packages_step = steps_by_key["packages"]
    assert packages_step["status"] == "failed"
    hosts_by_ip = {h["host"]: h["status"] for h in packages_step["hosts"]}
    assert hosts_by_ip["10.20.1.112"] == "done"
    assert hosts_by_ip["10.20.1.95"] == "failed"
    # 10.20.1.21 comes after the failing host in _NODES — must never even be
    # attempted, proving this phase stops rather than continuing past a
    # failed host the way the generic per-host loop elsewhere does.
    assert hosts_by_ip["10.20.1.21"] == "pending"

    assert steps_by_key["mon_init"]["status"] == "pending"
    assert steps_by_key["wait_quorum"]["status"] == "pending"
    assert steps_by_key["mgr_create"]["status"] == "pending"
    assert steps_by_key["osd_create"]["status"] == "pending"
    assert steps_by_key["verify"]["status"] == "pending"


# --- rpm-local method (Story 8.3) -------------------------------------------


def _rpm_local_params(**overrides):
    return _cephadm_params(rpm_path="/opt/ceph-rpms", **overrides)


def _rpm_local_fake_execute(host, command):
    """Extends _ceph_deploy_fake_execute with the rpm-local repo phase's own
    commands: the directory existence+non-empty check, and whatever
    createrepo/dpkg-scanpackages/repo-file-write command
    _package_manager_branch resolves to for this fake's RPM-family OS."""
    if command.startswith("[ -d "):
        return ""
    if "createrepo" in command or "dpkg-scanpackages" in command:
        return ""
    return _ceph_deploy_fake_execute(host, command)


def test_rpm_local_happy_path_builds_local_repo_installs_packages_and_writes_env(monkeypatch):
    seen_repo_commands: dict[str, list[str]] = {}

    def fake(host, command):
        if "createrepo" in command:
            seen_repo_commands.setdefault(host, []).append(command)
        return _rpm_local_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_POLL_INTERVAL_SECONDS", 0)
    write_progress, calls = _make_recording_progress_writer()

    written_fields = {}
    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", written_fields.update)

    result = run(
        "action-1", "deploy_cluster_rpm_local", _rpm_local_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    assert all(step["status"] == "done" for step in calls[-1][1])

    # Never adds the download.ceph.com repo — only builds a repo against the
    # locally-staged rpm_path (AC #2).
    for commands in seen_repo_commands.values():
        assert all("download.ceph.com" not in c for c in commands)
        assert all("/opt/ceph-rpms" in c for c in commands)

    assert written_fields["CEPH_EXEC_MODE"] == "none"


def test_rpm_local_fails_when_rpm_path_missing_on_one_node(monkeypatch):
    def fake(host, command):
        if command.startswith("[ -d ") and host == "10.20.1.95":
            raise ExecutorError("no such file or directory")
        return _rpm_local_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_rpm_local", _rpm_local_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["ssh_check"]["status"] == "done"
    assert steps_by_key["dependencies"]["status"] == "done"

    repo_step = steps_by_key["repo_local"]
    assert repo_step["status"] == "failed"
    assert "10.20.1.95" in repo_step["message"]
    assert "/opt/ceph-rpms" in repo_step["message"]

    # Stop-on-first-host-failure (AC #3/#4, same posture as Story 8.2): the
    # node after the failing one must never even be attempted.
    hosts_by_ip = {h["host"]: h["status"] for h in repo_step["hosts"]}
    assert hosts_by_ip["10.20.1.112"] == "done"
    assert hosts_by_ip["10.20.1.95"] == "failed"
    assert hosts_by_ip["10.20.1.21"] == "pending"

    assert steps_by_key["packages"]["status"] == "pending"
    assert steps_by_key["mon_init"]["status"] == "pending"
    assert steps_by_key["verify"]["status"] == "pending"


def test_rpm_local_fails_when_rpm_path_not_configured(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _rpm_local_fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_rpm_local", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["repo_local"]["status"] == "failed"
    assert "rpm_path" in steps_by_key["repo_local"]["message"]


# --- Misc ------------------------------------------------------------------


def test_unknown_action_id_returns_false_without_writing_progress():
    write_progress, calls = _make_recording_progress_writer()

    result = run("action-1", "not_a_real_action_id", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    assert result is False
    assert calls == []


def test_cluster_deploy_action_ids_match_policy_layer():
    from worker.policy.gate import VALID_CLUSTER_DEPLOY_ACTION_IDS

    assert CLUSTER_DEPLOY_ACTION_IDS == VALID_CLUSTER_DEPLOY_ACTION_IDS


def test_deploy_phase_error_is_a_plain_exception():
    with pytest.raises(DeployPhaseError):
        raise DeployPhaseError("something specific")
