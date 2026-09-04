import base64
import copy
import json
import shutil
import stat
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.executor.cluster_deploy as cluster_deploy_module
from shared.db import Base
from shared.models import BackupJob, Incident, NodeUpgradeGate, NodeUpgradeGateLock, NodeUpgradeGateState
from shared.node_upgrade_gate import LOCK_ID, claim_node_upgrade_gate_lock
from worker.executor.cluster_deploy import CLUSTER_DEPLOY_ACTION_IDS, DeployPhaseError, run
from worker.executor.ssh_executor import ExecutorError

_ROCKY_OS_RELEASE = 'ID="rocky"\nVERSION_ID="9.3"\nPRETTY_NAME="Rocky Linux 9.3"\n'
_UBUNTU_OS_RELEASE = 'ID="ubuntu"\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04.3 LTS"\n'
_UNSUPPORTED_OS_RELEASE = 'ID="alpine"\nVERSION_ID="3.19"\nPRETTY_NAME="Alpine Linux"\n'

_NODES = [
    {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
    # Different disk names per node (node1 /dev/vdc, node2 /dev/vdb) — the
    # whole point of per-node osd_disks instead of one cluster-wide value.
    {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disks": ["/dev/vdc"]},
    {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disks": ["/dev/vdb"]},
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


# --- Framework: ordering and stop-on-first-failure -------------------------


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


def test_completed_steps_get_frozen_started_and_finished_timestamps(monkeypatch):
    # 2026-07-28 regression test: dashboard/static/deploy_cluster.js used to
    # stamp EVERY step line with the browser's current clock on every poll
    # tick, so an already-finished step's displayed time kept drifting
    # forever — the actual fix is that cluster_deploy.py now writes each
    # step's OWN real started_at/finished_at once, which must never change
    # again after being set.
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _default_fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    final_progress = calls[-1][1]
    assert len(final_progress) > 1  # need at least 2 steps to prove "frozen", not just "set once"

    for step in final_progress:
        assert step["status"] == "done"
        assert step["started_at"] is not None
        assert step["finished_at"] is not None
        assert step["started_at"] <= step["finished_at"]

    # Once a step's write_progress call recorded it as "done", no LATER
    # write_progress call (for a subsequent step) may have changed its
    # timestamps — reconstruct each step's OWN first-seen "done" snapshot
    # and compare against the truly final one.
    first_seen_done = {}
    for _pk, snapshot in calls:
        for step in snapshot:
            key = step["step"]
            if step["status"] == "done" and key not in first_seen_done:
                first_seen_done[key] = (step["started_at"], step["finished_at"])

    for step in final_progress:
        assert first_seen_done[step["step"]] == (step["started_at"], step["finished_at"])


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
        i for i, cmd in enumerate(seen_commands) if "-o /usr/local/bin/cephadm" in cmd
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
    assert "apt-get install -y chrony lvm2" in seen_commands[chrony_index]
    assert "dnf install -y chrony epel-release lvm2" in seen_commands[chrony_index]


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
    install_pos = command.index("dnf install -y chrony epel-release lvm2")
    assert cleanup_pos < install_pos


def test_cephadm_deploy_uses_containerized_ceph_cli(monkeypatch):
    """A cephadm deployment must not require host-native ceph-common."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    assert any("cephadm bootstrap --mon-ip" in cmd for cmd in seen_commands)
    assert not any("ceph-common" in cmd for cmd in seen_commands)
    assert any(cmd.startswith("cephadm shell -- ceph orch host add") for cmd in seen_commands)
    assert any(cmd.startswith("cephadm shell -- ceph orch apply mgr") for cmd in seen_commands)
    assert any(cmd.startswith("cephadm shell -- ceph orch daemon add osd") for cmd in seen_commands)
    assert "cephadm shell -- ceph -s --format json" in seen_commands


def test_build_ceph_package_repo_command_nautilus_uses_codename_not_exact_version():
    """Regression, 2026-07-27: verified live against download.ceph.com —
    unlike every later release (which this command tries by exact version
    FIRST, falling back to the codename if that 404s — see the
    fallback-behavior tests below), Nautilus (14.x) was NEVER published
    under a per-exact-version directory at all (rpm-14.2.22/el8/ -> 404) —
    only the rpm-nautilus/debian-nautilus codename alias exists, safe to
    use forever since Nautilus is long EOL. repo_path_version() returns
    "nautilus" for BOTH repo_path and codename here, so the for-loop's two
    candidates collapse to the same (harmless) value."""
    command = cluster_deploy_module._build_ceph_package_repo_command("14.2.22")

    assert "debian-nautilus/" in command
    assert "for candidate in nautilus nautilus" in command
    assert "14.2.22" not in command


def test_build_ceph_package_repo_command_tries_exact_version_before_codename():
    command = cluster_deploy_module._build_ceph_package_repo_command("18.2.8")

    assert "for candidate in 18.2.8 reef" in command
    assert "rpm-$candidate/el$rhel_ver/noarch/repodata/repomd.xml" in command


def _run_repo_command_in_sandbox(tmp_path, command, *, exact_version_curl_ok, codename_curl_ok):
    """Actually EXECUTES the generated rpm branch in a real (but hermetic)
    bash subprocess with stubbed curl/rpm/dnf — every other test in this
    file only ever inspects the command STRING, but the exact-version ->
    codename fallback here is `&&`/`;`-chained shell logic subtle enough
    that a real regression (found while writing this fix: a bare `&&`
    before the "neither worked" error check let the FOR LOOP's own exit
    status — non-zero, from the last failed curl — silently skip that
    check and surface curl's raw exit code instead) would NOT have been
    caught by string inspection alone."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    curl_script = (
        "#!/bin/bash\n"
        "for arg in \"$@\"; do\n"
        f"  if [[ \"$arg\" == *rpm-18.2.8* ]]; then {'exit 0' if exact_version_curl_ok else 'exit 22'}; fi\n"
        f"  if [[ \"$arg\" == *rpm-reef* ]]; then {'exit 0' if codename_curl_ok else 'exit 22'}; fi\n"
        "done\n"
        "exit 1\n"
    )
    (bindir / "curl").write_text(curl_script)
    (bindir / "rpm").write_text(
        "#!/bin/bash\nif [[ \"$1\" == \"-E\" ]]; then echo 8; exit 0; fi\nexit 0\n"
    )
    dnf_log = tmp_path / "dnf_calls.log"
    (bindir / "dnf").write_text(f"#!/bin/bash\necho \"$*\" >> {dnf_log}\nexit 0\n")
    for name in ("curl", "rpm", "dnf"):
        path = bindir / name
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    for real_tool in ("rm", "uname"):
        shutil.copy(f"/bin/{real_tool}", bindir / real_tool)

    # Only the rpm branch matters here — since there's no apt-get on PATH
    # (env below sets PATH to ONLY our stub bindir), `if command -v
    # apt-get` fails and the elif (rpm) branch runs, matching a real
    # RPM-family node.
    result = subprocess.run(
        ["/bin/bash", "-c", command],
        env={"PATH": str(bindir)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = dnf_log.read_text().splitlines() if dnf_log.exists() else []
    return result.returncode, calls, result.stderr


def test_repo_command_falls_back_to_codename_when_exact_version_404s(tmp_path):
    command = cluster_deploy_module._build_ceph_package_repo_command("18.2.8")

    returncode, calls, _stderr = _run_repo_command_in_sandbox(
        tmp_path, command, exact_version_curl_ok=False, codename_curl_ok=True
    )

    assert returncode == 0
    assert any("rpm-reef/el8" in c for c in calls)
    assert not any("rpm-18.2.8" in c for c in calls)


def test_repo_command_uses_exact_version_when_it_works(tmp_path):
    command = cluster_deploy_module._build_ceph_package_repo_command("18.2.8")

    returncode, calls, _stderr = _run_repo_command_in_sandbox(
        tmp_path, command, exact_version_curl_ok=True, codename_curl_ok=True
    )

    assert returncode == 0
    assert any("rpm-18.2.8/el8" in c for c in calls)


def test_repo_command_fails_loudly_when_neither_candidate_works(tmp_path):
    # Regression for the exact bug found while building this fix: this
    # used to surface as a bare, unhelpful `curl` exit code (22) instead of
    # the clear message below, because the `&&` right before the
    # "neither worked" check was gated on the FOR LOOP's own exit status.
    command = cluster_deploy_module._build_ceph_package_repo_command("18.2.8")

    returncode, calls, stderr = _run_repo_command_in_sandbox(
        tmp_path, command, exact_version_curl_ok=False, codename_curl_ok=False
    )

    assert returncode == 1
    assert calls == []
    assert "No Ceph RPM repo found" in stderr


def test_cephadm_does_not_configure_package_repo_for_cli(monkeypatch):
    """Regression (live-verified 2026-07-26): `cephadm install ceph-common`
    left `ceph-common` unfindable via yum TWICE in a row. An intermediate
    fix (2026-07-28) tried forcing a same-version Ceph.com repo via
    `_build_ceph_package_repo_command` instead — which itself broke live
    against a real CentOS Stream 8 node targeting reef (Ceph never
    published el8 packages for reef at all, confirmed against the real
    download.ceph.com directory listing) — so this installs ceph-common
    PLAINLY (`dnf/yum install -y ceph-common`, no forced repo setup),
    relying on whatever repos are already configured, confirmed fine by
    the operator: this CLI only needs to talk to the orchestrator over
    Ceph's own cross-version-compatible protocol, never needs to exactly
    match the containerized daemons' real version. Still run AFTER
    bootstrap succeeds (bootstrap itself doesn't need `ceph` on PATH)."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    run("action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked)

    assert not any("install -y ceph-common" in cmd for cmd in seen_commands)
    assert not any("cephadm install ceph-common" in cmd for cmd in seen_commands)
    assert not any("cephadm add-repo" in cmd for cmd in seen_commands)
    # No forced same-version repo setup anywhere in this phase anymore.
    assert not any("rpm -E %rhel" in cmd for cmd in seen_commands)


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
    # /dev/vdb) — proves osd_disks is read per node, not one cluster-wide value.
    assert any("orch daemon add osd" in cmd and "/dev/vdc" in cmd for cmd in seen_commands)
    assert any("orch daemon add osd" in cmd and "/dev/vdb" in cmd for cmd in seen_commands)


def test_cephadm_orch_apply_osd_creates_one_osd_per_disk_on_same_node(monkeypatch):
    """The feature this all exists for: a single node can carry multiple
    OSD disks (e.g. /dev/vdc AND /dev/vdd), each becoming its OWN OSD via
    its own `ceph orch daemon add osd` call — never a combined/batch call."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, _calls = _make_recording_progress_writer()

    nodes = copy.deepcopy(_NODES)
    nodes[1]["osd_disks"] = ["/dev/vdc", "/dev/vdd"]  # 10.20.1.95 now has 2 disks

    result = run(
        "action-1",
        "deploy_cluster_cephadm",
        _cephadm_params(nodes=nodes),
        "incident-1",
        write_progress,
        _never_blocked,
    )

    assert result is True
    add_osd_commands = [cmd for cmd in seen_commands if "orch daemon add osd" in cmd]
    assert any("10-20-1-95.lab:/dev/vdc" in cmd for cmd in add_osd_commands)
    assert any("10-20-1-95.lab:/dev/vdd" in cmd for cmd in add_osd_commands)
    # 2 disks on 10.20.1.95 + 1 disk on 10.20.1.21 = 3 total OSD-create calls.
    assert len(add_osd_commands) == 3


# --- RGW (optional role) ----------------------------------------------------
#
# Unlike mon/mgr/osd, a node table with zero "rgw"-role nodes is a valid
# cluster (object storage is opt-in) — both the cephadm and ceph-deploy RGW
# phases must no-op cleanly in that case, and actually create/start RGW when
# at least one node has the role.

_NODES_WITH_RGW = _NODES + [{"ip": "10.20.1.201", "roles": ["rgw"]}]


def test_cephadm_orch_apply_rgw_skips_cleanly_when_no_rgw_nodes(monkeypatch):
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_cephadm", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["orch_apply_rgw"]["status"] == "done"
    assert steps_by_key["orch_apply_rgw"]["hosts"] == []
    assert not any("orch apply rgw" in cmd for cmd in seen_commands)


def test_cephadm_orch_apply_rgw_happy_path(monkeypatch):
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    params = _cephadm_params(nodes=copy.deepcopy(_NODES_WITH_RGW))
    result = run(
        "action-1", "deploy_cluster_cephadm", params, "incident-1", write_progress, _never_blocked
    )

    assert result is True
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["orch_apply_rgw"]["status"] == "done"

    rgw_hostname = "10-20-1-201.lab"
    apply_rgw_cmd = next(cmd for cmd in seen_commands if "orch apply rgw" in cmd)
    assert "default" in apply_rgw_cmd
    assert f"--placement={rgw_hostname}" in apply_rgw_cmd
    assert "--port=7480" in apply_rgw_cmd


def test_cephadm_happy_path_with_rgw_writes_rgw_nodes_to_env(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _default_fake_execute)
    write_progress, _calls = _make_recording_progress_writer()

    written_fields = {}
    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", written_fields.update)

    params = _cephadm_params(nodes=copy.deepcopy(_NODES_WITH_RGW))
    result = run(
        "action-1", "deploy_cluster_cephadm", params, "incident-1", write_progress, _never_blocked
    )

    assert result is True
    assert written_fields["CEPH_RGW_NODES"] == "10.20.1.201"


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
    # 2026-07-29 regression (supersedes a 2026-07-28 fix that got this
    # backwards): download.ceph.com's own RPM repos (checked directly —
    # rpm-nautilus/el8, rpm-quincy/el9, rpm-18.2.8/el9) never ship a
    # standalone ceph-volume RPM for ANY version; it's bundled inside
    # ceph-osd there. The 2026-07-28 fix added "ceph-volume" to the RPM
    # install command based on Fedora Project's OWN package listing — a
    # different build/repo than this app ever installs from — which made
    # every RPM install request a package NEVRA that has never existed
    # ("Unable to find a match: ceph-volume-14.2.22", confirmed live).
    # _package_manager_branch's generated command carries BOTH the apt AND
    # rpm branches inline in one string (only one runs at execution time,
    # decided live on the host) — the apt branch legitimately keeps
    # "ceph-volume", so a plain substring check against the whole command
    # would pass even with the old, wrong RPM behavior. Check the actual
    # dnf/yum install list text specifically instead.
    assert "dnf install -y ceph-mgr ceph-mon ceph-osd || yum install -y ceph-mgr ceph-mon ceph-osd" in (
        seen_install_commands["10.20.1.95"][0]
    )
    assert "ceph-volume" not in seen_install_commands["10.20.1.112"][0]  # no osd role there

    assert written_fields["CEPH_EXEC_MODE"] == "none"
    assert written_fields["CEPH_MON_NODES"] == "10.20.1.112,10.20.1.95,10.20.1.21"


def test_ceph_deploy_osd_create_runs_one_lvm_create_per_disk_on_same_node(monkeypatch):
    """Same feature, ceph-deploy method: `ceph-volume lvm create` creates
    exactly one OSD per invocation, so a node with 2 configured disks must
    get 2 separate calls — never a combined/batch call."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append((host, command))
        return _ceph_deploy_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_POLL_INTERVAL_SECONDS", 0)
    write_progress, _calls = _make_recording_progress_writer()

    nodes = copy.deepcopy(_NODES)
    nodes[1]["osd_disks"] = ["/dev/vdc", "/dev/vdd"]  # 10.20.1.95 now has 2 disks

    result = run(
        "action-1",
        "deploy_cluster_ceph_deploy",
        _cephadm_params(nodes=nodes),
        "incident-1",
        write_progress,
        _never_blocked,
    )

    assert result is True
    lvm_create_commands = [
        cmd for host, cmd in seen_commands if host == "10.20.1.95" and "ceph-volume lvm create" in cmd
    ]
    assert any("/dev/vdc" in cmd for cmd in lvm_create_commands)
    assert any("/dev/vdd" in cmd for cmd in lvm_create_commands)
    assert len(lvm_create_commands) == 2
    assert all("/usr/sbin" in cmd and "/sbin" in cmd for cmd in lvm_create_commands)


def test_ceph_deploy_packages_verifies_ceph_volume_with_system_sbin_path(monkeypatch):
    """Non-login Paramiko shells may omit /usr/sbin, where RPM installs
    ceph-volume. Package setup must expose that path and fail before OSD
    creation if the executable is genuinely absent."""
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    cluster_deploy_module._phase_ceph_deploy_packages(
        [{"ip": "10.3.53.136", "roles": ["osd"]}],
        {"version": "17.2.2"},
        lambda status: None,
    )

    assert len(seen_commands) == 1
    assert "export PATH=/usr/local/sbin:/usr/sbin:/sbin:$PATH" in seen_commands[0]
    assert "command -v ceph-volume" in seen_commands[0]
    assert "dnf install -y ceph-volume || yum install -y ceph-volume" in seen_commands[0]


def test_ceph_deploy_rgw_create_skips_cleanly_when_no_rgw_nodes(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _ceph_deploy_fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "deploy_cluster_ceph_deploy", _cephadm_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["rgw_create"]["status"] == "done"
    assert steps_by_key["rgw_create"]["hosts"] == []


def test_ceph_deploy_rgw_create_happy_path(monkeypatch):
    """Mirrors _phase_ceph_deploy_mgr_create's own shape: keyring generated
    on first_mon via `ceph auth get-or-create`, pushed to the RGW node under
    /var/lib/ceph/radosgw/ceph-rgw.<hostname>/keyring alongside the shared
    ceph.conf, then `ceph-radosgw@rgw.<hostname>` enabled+started — same
    "ceph-radosgw@rgw.*" unit-name convention commands.py's own
    `_UNIT_TYPE_MARKERS` substring classification already expects."""
    seen_commands: list[tuple[str, str]] = []

    def fake(host, command):
        seen_commands.append((host, command))
        return _ceph_deploy_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    write_progress, calls = _make_recording_progress_writer()

    params = _cephadm_params(nodes=copy.deepcopy(_NODES_WITH_RGW))
    result = run(
        "action-1", "deploy_cluster_ceph_deploy", params, "incident-1", write_progress, _never_blocked
    )

    assert result is True
    steps_by_key = {s["step"]: s for s in calls[-1][1]}
    assert steps_by_key["rgw_create"]["status"] == "done"

    rgw_ip = "10.20.1.201"
    rgw_hostname = "10-20-1-201.lab"
    first_mon_ip = "10.20.1.112"

    auth_cmd = next(
        cmd for host, cmd in seen_commands if host == first_mon_ip and "ceph auth get-or-create client.rgw." in cmd
    )
    assert f"client.rgw.{rgw_hostname}" in auth_cmd
    assert "osd 'allow rwx' mon 'allow rw'" in auth_cmd

    assert any(
        host == rgw_ip and f"{rgw_hostname}/keyring" in cmd and "base64 -d" in cmd for host, cmd in seen_commands
    )
    start_cmd = next(
        cmd for host, cmd in seen_commands if host == rgw_ip and "systemctl enable --now ceph-radosgw@rgw." in cmd
    )
    assert f"ceph-radosgw@rgw.{rgw_hostname}" in start_cmd

    # Packages phase (installed alongside mon/mgr/osd) must have requested
    # the RGW package for this node too — RPM build is ceph-radosgw.
    install_cmd = next(
        cmd for host, cmd in seen_commands if host == rgw_ip and "install -y" in cmd and "ceph-radosgw" in cmd
    )
    assert "ceph-radosgw" in install_cmd


def test_build_ceph_conf_adds_client_rgw_section_only_when_rgw_nodes_given():
    mon_nodes = [n for n in _NODES if "mon" in n["roles"]]
    hostnames = {n["ip"]: n["ip"].replace(".", "-") + ".lab" for n in _NODES_WITH_RGW}

    conf_without_rgw = cluster_deploy_module._build_ceph_conf(
        {"osd_pool_default_size": 3, "osd_pool_default_min_size": 2}, mon_nodes, hostnames, "fake-fsid"
    )
    assert "[client.rgw." not in conf_without_rgw

    rgw_nodes = [n for n in _NODES_WITH_RGW if "rgw" in n["roles"]]
    conf_with_rgw = cluster_deploy_module._build_ceph_conf(
        {"osd_pool_default_size": 3, "osd_pool_default_min_size": 2}, mon_nodes, hostnames, "fake-fsid", rgw_nodes
    )
    assert "[client.rgw.10-20-1-201.lab]" in conf_with_rgw
    assert "rgw frontends = \"beast port=7480\"" in conf_with_rgw


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


def test_mkfs_and_start_mon_command_wipes_data_dir_before_mkfs():
    """Regression, 2026-07-28 (live-verified on a real deploy): `ceph-mon
    --mkfs` against a data dir that already has a valid store from an
    earlier attempt does NOT reinitialize it — it silently keeps the OLD
    store (old fsid, old monmap) and ignores the --monmap/--keyring passed
    this time. A retried "Dựng cụm" (an earlier attempt got past mon_init
    but failed at a later phase, then the operator retried without an
    intervening "Xoá cụm") silently produced a mon whose real fsid didn't
    match the freshly-written /etc/ceph/ceph.conf — every `ceph` CLI call
    against it then failed with the deeply misleading "[errno 1] error
    connecting to the cluster", even though the mon itself was healthy and
    in quorum (confirmed via the admin socket, which bypasses the network
    client entirely). Must stop any running mon and wipe its data dir
    before mkfs, every time, so a retry can never reuse stale state."""
    command = cluster_deploy_module._mkfs_and_start_mon_command("khiempx-ceph1.novalocal")

    stop_pos = command.index("systemctl stop ceph-mon@")
    rm_pos = command.index("rm -rf")
    mkdir_pos = command.index("mkdir -p")
    mkfs_pos = command.index("ceph-mon --mkfs")
    assert stop_pos < rm_pos < mkdir_pos < mkfs_pos
    assert "/var/lib/ceph/mon/ceph-khiempx-ceph1.novalocal" in command[rm_pos:mkdir_pos]


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


def test_ceph_deploy_mon_security_skips_for_mimic(monkeypatch):
    # 2026-08-06, verified live against a real Mimic 13.2.10 mon: `ceph mon
    # enable-msgr2` doesn't exist pre-Nautilus ("no valid command found",
    # EINVAL) — msgr2 support was introduced in Nautilus (14.x), so this
    # phase must not even attempt it for Mimic.
    def fake(host, command):
        raise AssertionError(f"must not run any command for Mimic: {command}")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)

    cluster_deploy_module._phase_ceph_deploy_mon_security(
        copy.deepcopy(_NODES), {"version": "13.2.10"}, lambda status: None
    )


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
    just to check one install command's package names.

    2026-07-29: also the exact real-world failure this pinning interacted
    badly with — pinning "ceph-volume-14.2.22" specifically always 404s
    ("Unable to find a match"), since download.ceph.com's RPM repos never
    ship a standalone ceph-volume package for any version (see
    _ROLE_TO_PACKAGES_RPM's own comment). RPM must not request ceph-volume
    at all; apt still does (a genuine, confirmed packaging difference)."""
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
    assert "ceph-volume-14.2.22" not in command
    assert (
        "apt-get install -y ceph-mgr ceph-mon ceph-osd ceph-volume" in command
    )  # apt never pins — no ambiguity there; and DOES include ceph-volume


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
    # Package NAME must not be version-pinned (the actual behavior under
    # test) — the auto-cleanup snippet legitimately mentions the version
    # elsewhere (for its own installed-version comparison), so check the
    # pinned-name shape specifically rather than "18.2.8 not in command".
    assert "ceph-mon-18.2.8" not in command


# -- Auto-cleanup of a conflicting pre-existing Ceph install (2026-08-06) --
# Real-world case: a lab node reused from an earlier manual/ceph-deploy
# install (or an earlier attempt at a DIFFERENT version through this same
# tool) already has ceph-common installed — yum/dnf then gets stuck
# resolving the OLD version's already-installed librados2/etc against the
# NEW ceph-common's own librados2 requirement (an unreadable dependency
# wall, not a clean failure). _phase_ceph_deploy_packages now prepends an
# auto-cleanup snippet that force-removes the old install FIRST when it
# detects a version mismatch, so the install right after just works.


def test_remove_conflicting_ceph_install_snippet_compares_exact_version():
    snippet = cluster_deploy_module._remove_conflicting_ceph_install_snippet("13.2.10")

    assert "rpm -q --qf '%{VERSION}\\n' ceph-common" in snippet
    assert '!= 13.2.10' in snippet
    assert "yum remove -y" in snippet and "dnf remove -y" in snippet
    assert "ceph-*" in snippet and "librados2*" in snippet
    assert "rm -f /etc/yum.repos.d/ceph.repo" in snippet
    assert "dpkg-query -W" in snippet  # apt branch present too


def test_ceph_deploy_packages_prepends_conflict_cleanup_before_install(monkeypatch):
    seen_commands = []

    def fake(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)

    nodes = [{"ip": "10.20.1.112", "roles": ["mon"]}]
    cluster_deploy_module._phase_ceph_deploy_packages(
        nodes, {"version": "13.2.10"}, lambda status: None
    )

    command = seen_commands[0]
    # Cleanup runs FIRST (same SSH round trip), then the real install.
    cleanup_index = command.index("rpm -q --qf")
    install_index = command.index("(dnf install -y ceph-mon-13.2.10")
    assert cleanup_index < install_index
    assert "!= 13.2.10" in command


def test_ceph_deploy_packages_cleanup_never_blocks_install_on_removal_failure():
    # The removal branch must swallow its own exit status — a partial
    # removal failure (e.g. a package half-uninstalled already) must not
    # stop the fresh install from even being attempted.
    snippet = cluster_deploy_module._remove_conflicting_ceph_install_snippet("18.2.8")
    assert "|| true" in snippet


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


# --- Chuyển đổi systemd -> cephadm (2026-07-28) -----------------------------

_CONVERT_NODES = [
    {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
    {"ip": "10.20.1.95", "roles": ["mon", "osd"]},
]

_CONVERT_MON_IDS = {"10.20.1.112": "nodeA", "10.20.1.95": "nodeB"}
_CONVERT_MGR_IDS = {"10.20.1.112": "nodeA"}
_CONVERT_OSD_IDS = {"10.20.1.95": ["0", "1"]}


def _convert_params(**overrides):
    params = {"version": "18.2.8", "nodes": copy.deepcopy(_CONVERT_NODES)}
    params.update(overrides)
    return params


def _convert_fake_execute(host, command):
    if command == "true":
        return ""
    if "hostname -f" in command:
        return host.replace(".", "-") + ".lab"
    if "command -v cephadm" in command:
        return ""
    if "systemctl list-units" in command and "ceph-mon@" in command:
        mon_id = _CONVERT_MON_IDS.get(host)
        return f"ceph-mon@{mon_id}.service" if mon_id else ""
    if "systemctl list-units" in command and "ceph-mgr@" in command:
        mgr_id = _CONVERT_MGR_IDS.get(host)
        return f"ceph-mgr@{mgr_id}.service" if mgr_id else ""
    if "adopt --style legacy" in command:
        return ""
    if "ceph mgr module enable cephadm" in command:
        return ""
    if command == "ceph cephadm get-pub-key":
        return "ssh-ed25519 AAAAFAKEKEY cephadm\n"
    if "authorized_keys" in command:
        return ""
    if "ceph orch host add" in command:
        return ""
    if "ceph-volume lvm list --format json" in command:
        ids = _CONVERT_OSD_IDS.get(host, [])
        return json.dumps({i: [{"tags": {"ceph.osd_id": i}}] for i in ids})
    if "ceph -s --format json" in command:
        return json.dumps({"health": {"status": "HEALTH_OK"}})
    return ""


def test_convert_action_id_registered_in_phases_and_policy():
    from worker.policy.gate import VALID_CLUSTER_DEPLOY_ACTION_IDS

    assert "convert_cluster_to_cephadm" in cluster_deploy_module._PHASES_BY_ACTION_ID
    assert "convert_cluster_to_cephadm" in VALID_CLUSTER_DEPLOY_ACTION_IDS


def test_discover_systemd_daemon_id_extracts_id_from_unit_name(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "execute_command",
        lambda host, command: "ceph-mon@nodeA.service",
    )
    daemon_id = cluster_deploy_module._discover_systemd_daemon_id("10.20.1.112", "ceph-mon")
    assert daemon_id == "nodeA"


def test_discover_systemd_daemon_id_returns_none_when_no_unit_found(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda host, command: "")
    assert cluster_deploy_module._discover_systemd_daemon_id("10.20.1.112", "ceph-mon") is None


def test_discover_systemd_daemon_id_raises_on_ssh_failure(monkeypatch):
    def broken(host, command):
        raise ExecutorError("no route to host")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", broken)
    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._discover_systemd_daemon_id("10.20.1.112", "ceph-mon")


def test_cephadm_image_for_version_builds_quay_tag():
    assert cluster_deploy_module._cephadm_image_for_version("17.2.5") == "quay.io/ceph/ceph:v17.2.5"


def test_convert_adopt_mons_uses_discovered_daemon_id(monkeypatch):
    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    write_progress, _calls = _make_recording_progress_writer()

    cluster_deploy_module._phase_convert_adopt_mons(
        copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
    )

    adopt_commands = [c for _h, c in calls if "adopt --style legacy" in c]
    assert any("mon.nodeA" in c for c in adopt_commands)
    assert any("mon.nodeB" in c for c in adopt_commands)
    # 2026-07-28 regression: without an explicit --image pin, cephadm adopt
    # defaults to whatever the LATEST build for that codename currently is
    # on quay.io — verified live to silently diverge from the exact
    # version actually running on the not-yet-adopted daemons, leaving
    # `ceph versions` permanently mixed.
    assert all("--image quay.io/ceph/ceph:v18.2.8" in c for c in adopt_commands)


def test_cephadm_managed_daemon_ids_parses_cephadm_ls(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "execute_command",
        lambda ip, cmd: json.dumps(
            [
                {"name": "mon.nodeA", "style": "cephadm:v1"},
                {"name": "mgr.nodeA", "style": "cephadm:v1"},
                {"name": "osd.0", "style": "cephadm:v1"},
            ]
        ),
    )
    assert cluster_deploy_module._cephadm_managed_daemon_ids("10.20.1.112", "mon") == {"nodeA"}
    assert cluster_deploy_module._cephadm_managed_daemon_ids("10.20.1.112", "osd") == {"0"}
    assert cluster_deploy_module._cephadm_managed_daemon_ids("10.20.1.112", "mds") == set()


def test_cephadm_managed_daemon_ids_uses_no_detail_flag_not_format_json(monkeypatch):
    """Regression, 2026-07-28 (verified live): `cephadm ls --format json`
    fails outright ("error: unrecognized arguments: --format json") —
    `cephadm ls` accepts no such flag and always prints JSON regardless.
    That failure used to be swallowed silently by this function's own
    `except ExecutorError: return set()`, so it always reported "nothing
    adopted yet" even when something plainly was, defeating the whole
    point of checking in the first place."""
    seen_commands = []

    def fake(ip, cmd):
        seen_commands.append(cmd)
        return "[]"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    cluster_deploy_module._cephadm_managed_daemon_ids("10.20.1.112", "mon")

    assert "--format json" not in seen_commands[0]
    assert "--no-detail" in seen_commands[0]


def test_cephadm_managed_daemon_ids_excludes_legacy_style_entries(monkeypatch):
    """Regression, 2026-07-28 (verified live): `cephadm ls` lists EVERY
    Ceph daemon it can discover on the host, including ones it does NOT
    manage yet — a real, still-native OSD showed up with `"name": "osd.0"`
    and `"style": "legacy"` right alongside genuinely-adopted mon/mgr
    entries (`"style": "cephadm:v1"`). Matching on `name` alone (the
    original bug) treated that legacy OSD as "already converted" and
    skipped adopting it entirely — the OSD-adoption phase silently
    no-opped and reported success while `ceph versions` stayed mixed."""
    monkeypatch.setattr(
        cluster_deploy_module,
        "execute_command",
        lambda ip, cmd: json.dumps(
            [
                {"name": "mon.nodeA", "style": "cephadm:v1"},
                {"name": "mgr.nodeA", "style": "cephadm:v1"},
                {"name": "osd.0", "style": "legacy", "systemd_unit": "ceph-osd@0"},
            ]
        ),
    )
    assert cluster_deploy_module._cephadm_managed_daemon_ids("10.20.1.112", "mon") == {"nodeA"}
    assert cluster_deploy_module._cephadm_managed_daemon_ids("10.20.1.112", "osd") == set()


def test_cephadm_managed_daemon_ids_returns_empty_set_when_cephadm_not_installed(monkeypatch):
    def fake(ip, cmd):
        raise ExecutorError("cephadm: command not found")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake)
    assert cluster_deploy_module._cephadm_managed_daemon_ids("10.20.1.112", "mon") == set()


def test_convert_adopt_mons_skips_already_cephadm_managed_daemon(monkeypatch):
    """2026-07-28 regression: a real conversion adopted mon+mgr, then
    failed at a LATER phase (enable_orchestrator) — an operator who
    finishes the remaining steps by hand and re-runs this feature must not
    have mon adoption re-attempted (no native ceph-mon@* unit is left to
    discover for it anymore, already renamed by the first adoption)."""
    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        if "cephadm ls" in command:
            return json.dumps([{"name": "mon.nodeA", "style": "cephadm:v1"}])
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    host_updates = []

    cluster_deploy_module._phase_convert_adopt_mons(
        [{"ip": "10.20.1.112", "roles": ["mon"]}], _convert_params(), host_updates.append
    )

    assert not any("adopt --style legacy" in c for _h, c in calls)
    final_status = host_updates[-1][0]
    assert final_status["status"] == "done"
    assert "đã chuyển đổi từ trước" in final_status["message"]


def test_convert_adopt_mons_fails_when_no_mon_configured():
    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_convert_adopt_mons(
            [{"ip": "10.20.1.1", "roles": ["osd"]}], _convert_params(), lambda hosts: None
        )


def test_convert_adopt_mons_fails_when_no_systemd_unit_running(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda host, command: "")
    with pytest.raises(DeployPhaseError, match="ceph-mon@"):
        cluster_deploy_module._phase_convert_adopt_mons(
            copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
        )


def test_convert_adopt_mgrs_uses_discovered_daemon_id(monkeypatch):
    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)

    cluster_deploy_module._phase_convert_adopt_mgrs(
        copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
    )

    adopt_commands = [c for _h, c in calls if "adopt --style legacy" in c]
    assert any("mgr.nodeA" in c for c in adopt_commands)
    assert all("--image quay.io/ceph/ceph:v18.2.8" in c for c in adopt_commands)


def test_convert_install_cephadm_runs_on_every_node(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cluster_deploy_module,
        "execute_command",
        lambda host, command: calls.append(host) or "",
    )

    cluster_deploy_module._phase_convert_install_cephadm(
        copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
    )

    assert set(calls) == {"10.20.1.112", "10.20.1.95"}


def test_convert_install_cephadm_fails_for_unrecognized_version(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda host, command: "")
    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_convert_install_cephadm(
            copy.deepcopy(_CONVERT_NODES), _convert_params(version="99.9.9"), lambda hosts: None
        )


def test_convert_distribute_ssh_key_appends_key_to_every_node_including_first_mon(monkeypatch):
    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)

    cluster_deploy_module._phase_convert_distribute_ssh_key(
        copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
    )

    # 2026-07-28 regression: first_mon (10.20.1.112) used to be skipped
    # (wrongly assuming `cephadm adopt` self-authorizes its own host the
    # way `cephadm bootstrap` does) — failed live with "ceph orch host
    # add" unable to SSH back to first_mon itself, "Permission denied".
    # Every node, including first_mon, must get the key appended.
    key_appends = {h for h, c in calls if "authorized_keys" in c}
    assert key_appends == {"10.20.1.112", "10.20.1.95"}


def test_convert_distribute_ssh_key_fails_when_pub_key_empty(monkeypatch):
    def fake_execute(host, command):
        if command == "ceph cephadm get-pub-key":
            return ""
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_convert_distribute_ssh_key(
            copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
        )


def test_convert_register_hosts_adds_every_node(monkeypatch):
    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    params = _convert_params()
    params["_node_hostnames"] = {"10.20.1.112": "nodeA.lab", "10.20.1.95": "nodeB.lab"}

    cluster_deploy_module._phase_convert_register_hosts(
        copy.deepcopy(_CONVERT_NODES), params, lambda hosts: None
    )

    host_add_commands = [c for _h, c in calls if "ceph orch host add" in c]
    assert any("nodeA.lab" in c and "10.20.1.112" in c for c in host_add_commands)
    assert any("nodeB.lab" in c and "10.20.1.95" in c for c in host_add_commands)


def test_convert_adopt_osds_discovers_and_adopts_every_id(monkeypatch):
    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)

    cluster_deploy_module._phase_convert_adopt_osds(
        copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
    )

    adopt_commands = [c for h, c in calls if h == "10.20.1.95" and "adopt --style legacy" in c]
    assert any("osd.0" in c for c in adopt_commands)
    assert any("osd.1" in c for c in adopt_commands)
    assert all("--image quay.io/ceph/ceph:v18.2.8" in c for c in adopt_commands)


def test_convert_adopt_osds_skips_already_cephadm_managed_ids(monkeypatch):
    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        if "cephadm ls" in command:
            return json.dumps([{"name": "osd.0", "style": "cephadm:v1"}])
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    host_updates = []

    cluster_deploy_module._phase_convert_adopt_osds(
        copy.deepcopy(_CONVERT_NODES), _convert_params(), host_updates.append
    )

    adopt_commands = [c for h, c in calls if h == "10.20.1.95" and "adopt --style legacy" in c]
    # osd.0 already cephadm-managed -> must NOT be re-adopted; osd.1 is not
    # -> must still be adopted.
    assert not any("osd.0" in c for c in adopt_commands)
    assert any("osd.1" in c for c in adopt_commands)
    final_message = host_updates[-1][0]["message"]
    assert "đã chuyển đổi từ trước" in final_message


def test_convert_adopt_osds_skips_host_with_no_osds(monkeypatch):
    write_progress, calls = _make_recording_progress_writer()
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _convert_fake_execute)

    nodes = [{"ip": "10.20.1.200", "roles": ["osd"]}]  # not in _CONVERT_OSD_IDS
    host_updates = []
    cluster_deploy_module._phase_convert_adopt_osds(nodes, _convert_params(), host_updates.append)

    assert host_updates[-1][0]["status"] == "done"
    assert "Không có OSD" in host_updates[-1][0]["message"]


def test_convert_enable_orchestrator_runs_on_first_mon(monkeypatch):
    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)

    cluster_deploy_module._phase_convert_enable_orchestrator(
        copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
    )

    assert calls[0][0] == "10.20.1.112"
    assert "ceph orch set backend cephadm" in calls[0][1]
    # 2026-07-28 regression: without --force, this failed live right after
    # adopt_mgrs with "all mgr daemons do not support module 'cephadm'"
    # (the mon's capability cache for the just-restarted mgr hadn't caught
    # up yet) — Ceph's own error message names --force as the fix.
    assert "ceph mgr module enable cephadm --force" in calls[0][1]


def test_convert_cluster_happy_path_all_phases_succeed_and_writes_env(monkeypatch):
    # Stateful per-host `cephadm ls` fake (2026-07-28, added alongside
    # _phase_convert_verify): the final verify phase independently
    # re-queries `cephadm ls` on every host, so the fake must actually
    # reflect each `adopt --style legacy --name X` command this same run
    # already sent — a fake that always reports "nothing adopted" (like
    # the plain _convert_fake_execute default) would make the new verify
    # phase correctly fail even on this genuinely-successful run.
    adopted_by_host: dict[str, set[str]] = {}

    def fake_execute(host, command):
        if "adopt --style legacy" in command:
            name = command.split("--name", 1)[1].strip().split()[0]
            adopted_by_host.setdefault(host, set()).add(name)
            return _convert_fake_execute(host, command)
        if "cephadm ls" in command:
            entries = [{"name": n, "style": "cephadm:v1"} for n in adopted_by_host.get(host, set())]
            return json.dumps(entries)
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    written_fields = {}
    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", written_fields.update)

    result = run(
        "action-1",
        "convert_cluster_to_cephadm",
        _convert_params(),
        "incident-1",
        write_progress,
        _never_blocked,
    )

    assert result is True
    assert all(step["status"] == "done" for step in calls[-1][1])
    assert written_fields["CEPH_EXEC_MODE"] == "cephadm"
    # mon/mgr/osd node lists must stay the SAME as before — this is a
    # conversion, not a re-deploy, nothing about which nodes serve which
    # role should change.
    assert written_fields["CEPH_MON_NODES"] == "10.20.1.112,10.20.1.95"
    assert written_fields["CEPH_MGR_NODES"] == "10.20.1.112"
    assert written_fields["CEPH_OSD_NODES"] == "10.20.1.95"


def test_convert_verify_fails_when_osd_still_legacy_despite_healthy_cluster(monkeypatch):
    """Regression, 2026-07-28: the exact real-world shape of the bug this
    phase exists to catch — `ceph -s` reports HEALTH_OK (mon/mgr already
    adopted) but the OSD host's `cephadm ls` still lists its OSD with
    style="legacy" (never actually adopted). Must raise, not report done,
    even though the generic health check alone would have passed."""

    def fake_execute(host, command):
        if "ceph -s --format json" in command:
            return json.dumps({"health": {"status": "HEALTH_OK"}})
        if "cephadm ls" in command:
            if host == "10.20.1.112":
                return json.dumps(
                    [
                        {"name": "mon.nodeA", "style": "cephadm:v1"},
                        {"name": "mgr.nodeA", "style": "cephadm:v1"},
                    ]
                )
            if host == "10.20.1.95":
                return json.dumps(
                    [
                        {"name": "mon.nodeB", "style": "cephadm:v1"},
                        {"name": "osd.0", "style": "legacy"},
                        {"name": "osd.1", "style": "legacy"},
                    ]
                )
            return "[]"
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)

    with pytest.raises(DeployPhaseError, match="OSD.*chưa được cephadm quản lý"):
        cluster_deploy_module._phase_convert_verify(
            copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
        )


def test_convert_verify_passes_when_every_daemon_is_cephadm_managed(monkeypatch):
    def fake_execute(host, command):
        if "ceph -s --format json" in command:
            return json.dumps({"health": {"status": "HEALTH_OK"}})
        if "cephadm ls" in command:
            if host == "10.20.1.112":
                return json.dumps(
                    [
                        {"name": "mon.nodeA", "style": "cephadm:v1"},
                        {"name": "mgr.nodeA", "style": "cephadm:v1"},
                    ]
                )
            if host == "10.20.1.95":
                return json.dumps(
                    [
                        {"name": "mon.nodeB", "style": "cephadm:v1"},
                        {"name": "osd.0", "style": "cephadm:v1"},
                        {"name": "osd.1", "style": "cephadm:v1"},
                    ]
                )
            return "[]"
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)

    # Must not raise.
    cluster_deploy_module._phase_convert_verify(
        copy.deepcopy(_CONVERT_NODES), _convert_params(), lambda hosts: None
    )


def test_convert_cluster_stops_at_health_precheck_when_already_health_err(monkeypatch):
    def fake_execute(host, command):
        if "ceph -s --format json" in command:
            return json.dumps({"health": {"status": "HEALTH_ERR"}})
        return _convert_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1",
        "convert_cluster_to_cephadm",
        _convert_params(),
        "incident-1",
        write_progress,
        _never_blocked,
    )

    assert result is False
    final = calls[-1][1]
    steps_by_key = {s["step"]: s for s in final}
    assert steps_by_key["ssh_check"]["status"] == "done"
    assert steps_by_key["health_precheck"]["status"] == "failed"
    # every LATER step must never have been touched
    assert steps_by_key["install_cephadm"]["status"] == "pending"


_OSD_LVM_LIST_TWO_OSDS = """
====== osd.0 =======
      cluster fsid              11111111-1111-1111-1111-111111111111
      osd fsid                  22222222-2222-2222-2222-222222222222
      osd id                    0
      devices                   /dev/sdb
====== osd.1 =======
      cluster fsid              11111111-1111-1111-1111-111111111111
      osd fsid                  33333333-3333-3333-3333-333333333333
      osd id                    1
      devices                   /dev/sdc
"""

_OSD_LVM_LIST_MISSING_FSID = """
====== osd.0 =======
      cluster fsid              11111111-1111-1111-1111-111111111111
      osd id                    0
"""


@pytest.fixture
def gate_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cluster_deploy_module.db, "SessionLocal", session_factory)
    with session_factory() as session:
        session.add(Incident(id="incident-1", ceph_code="NODE_OS_GATE", detected_at=datetime.utcnow()))
        session.commit()
    yield session_factory
    Base.metadata.drop_all(engine)


def _make_gate(gate_db, **overrides) -> str:
    with gate_db() as session:
        session.add(NodeUpgradeGateLock(id=LOCK_ID, active_gate_id=overrides.get("id", "gate-1")))
        fields = {
            "id": "gate-1",
            "host": "10.20.1.83",
            "target_version": "19.2.0",
            "state": NodeUpgradeGateState.PREPARING.value,
        }
        fields.update(overrides)
        gate = NodeUpgradeGate(**fields)
        session.add(gate)
        session.commit()
        return gate.id


def _gate_action_params(gate_id: str, roles: list, **overrides) -> dict:
    params = {
        "host": "10.20.1.83",
        "target_version": "19.2.0",
        "roles": roles,
        "nodes": ["10.20.1.83"],
        "node_upgrade_gate_id": gate_id,
        "action_pk": "action-pk-1",
        "incident_id": "incident-1",
    }
    params.update(overrides)
    return params


def _fetch_gate(gate_db, gate_id: str) -> NodeUpgradeGate:
    with gate_db() as session:
        return session.get(NodeUpgradeGate, gate_id)


def _record_recording_host_updates():
    calls = []
    return (lambda host_status: calls.append(copy.deepcopy(host_status))), calls


# --- _parse_osd_backup -------------------------------------------------


def test_parse_osd_backup_extracts_id_and_fsid_not_cluster_fsid():
    result = cluster_deploy_module._parse_osd_backup(_OSD_LVM_LIST_TWO_OSDS)

    assert result == [
        {"osd_id": "0", "osd_fsid": "22222222-2222-2222-2222-222222222222"},
        {"osd_id": "1", "osd_fsid": "33333333-3333-3333-3333-333333333333"},
    ]
    # Confirm neither entry accidentally captured the (also-present) cluster fsid.
    assert "11111111-1111-1111-1111-111111111111" not in [r["osd_fsid"] for r in result]


def test_parse_osd_backup_raises_on_missing_field():
    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._parse_osd_backup(_OSD_LVM_LIST_MISSING_FSID)


def test_parse_osd_backup_raises_when_no_osd_blocks_found():
    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._parse_osd_backup("")


# --- _phase_gate_backup_osd_and_metadata --------------------------------


def test_backup_osd_and_metadata_skips_osd_backup_for_non_osd_node(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db)
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata,
        "latest_successful_metadata_job",
        lambda: type("_J", (), {"created_at": datetime.utcnow()})(),
    )

    def fake_execute(host, command):
        assert command != "ceph-volume lvm list", "must not run for a non-OSD node"
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_backup_osd_and_metadata(
        ["10.20.1.83"], _gate_action_params(gate_id, roles=["MON"]), on_update
    )

    assert _fetch_gate(gate_db, gate_id).osd_backup is None


def test_backup_osd_and_metadata_writes_osd_backup_for_osd_node(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db)
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata,
        "latest_successful_metadata_job",
        lambda: type("_J", (), {"created_at": datetime.utcnow()})(),
    )

    def fake_execute(host, command):
        if command == "ceph-volume lvm list":
            return _OSD_LVM_LIST_TWO_OSDS
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_backup_osd_and_metadata(
        ["10.20.1.83"], _gate_action_params(gate_id, roles=["OSD"]), on_update
    )

    stored = json.loads(_fetch_gate(gate_db, gate_id).osd_backup)
    assert stored == [
        {"osd_id": "0", "osd_fsid": "22222222-2222-2222-2222-222222222222"},
        {"osd_id": "1", "osd_fsid": "33333333-3333-3333-3333-333333333333"},
    ]


def test_backup_osd_and_metadata_skips_metadata_run_when_recent_backup_exists(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db)
    recent_job = type("_J", (), {"created_at": datetime.utcnow() - timedelta(hours=1)})()
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata, "latest_successful_metadata_job", lambda: recent_job
    )
    called = []
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata, "run", lambda *a, **k: called.append(1) or True
    )
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda h, c: "")
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_backup_osd_and_metadata(
        ["10.20.1.83"], _gate_action_params(gate_id, roles=["MON"]), on_update
    )

    assert called == []  # metadata.run() was never triggered


def test_backup_osd_and_metadata_triggers_metadata_run_when_stale_or_absent(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db)
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata, "latest_successful_metadata_job", lambda: None
    )
    called = []
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata,
        "run",
        lambda *a, **k: called.append(a) or True,
    )
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda h, c: "")
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_backup_osd_and_metadata(
        ["10.20.1.83"], _gate_action_params(gate_id, roles=["MON"]), on_update
    )

    assert len(called) == 1


def test_backup_osd_and_metadata_raises_when_metadata_run_fails(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db)
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata, "latest_successful_metadata_job", lambda: None
    )
    monkeypatch.setattr(cluster_deploy_module.backup_metadata, "run", lambda *a, **k: False)
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda h, c: "")
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_gate_backup_osd_and_metadata(
            ["10.20.1.83"], _gate_action_params(gate_id, roles=["MON"]), on_update
        )


# --- _phase_gate_set_maintenance_flags ----------------------------------


def test_set_maintenance_flags_skips_for_non_osd_node(monkeypatch):
    def fake_execute(host, command):
        raise AssertionError("must not run any ceph command for a non-OSD node")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    monkeypatch.setattr(cluster_deploy_module, "configured_nodes", lambda: [])
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_set_maintenance_flags(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["MON"]), on_update
    )


def test_set_maintenance_flags_only_sets_flags_not_already_present(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [{"host": "10.20.1.150", "roles": ["MON"]}],
    )
    commands_run = []

    def fake_execute(host, command):
        if command == "ceph osd dump --format json":
            return json.dumps({"flags": "sortbitwise,noout"})
        commands_run.append(command)
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_set_maintenance_flags(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
    )

    assert commands_run == [
        "ceph osd set noscrub",
        "ceph osd set nodeep-scrub",
        "ceph osd set nosnaptrim",
    ]


def test_set_maintenance_flags_noop_when_all_already_set(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [{"host": "10.20.1.150", "roles": ["MON"]}],
    )

    def fake_execute(host, command):
        if command == "ceph osd dump --format json":
            return json.dumps({"flags": "noout,noscrub,nodeep-scrub,nosnaptrim"})
        raise AssertionError("no `ceph osd set` should run when everything is already set")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_set_maintenance_flags(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
    )


# --- _phase_gate_remove_mon ----------------------------------------------


def test_remove_mon_skips_for_non_mon_node(monkeypatch):
    def fake_execute(host, command):
        raise AssertionError("must not run any ceph command for a non-MON node")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_remove_mon(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
    )


def test_remove_mon_happy_path_confirms_quorum_count(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [
            {"host": "10.20.1.83", "roles": ["MON"]},
            {"host": "10.20.1.150", "roles": ["MON"]},
        ],
    )
    quorum_calls = {"n": 0}

    def fake_execute(host, command):
        if command.startswith("hostname"):
            return "node83.lab"
        if command == "ceph quorum_status --format json":
            quorum_calls["n"] += 1
            names = ["node83", "node150", "node200"] if quorum_calls["n"] == 1 else ["node150", "node200"]
            return json.dumps({"quorum_names": names})
        if command.startswith("ceph mon rm"):
            assert "mon remove" not in command  # never the deprecated alias
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_remove_mon(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["MON"]), on_update
    )

    assert calls[-1][0]["status"] == "done"


def test_remove_mon_fails_when_quorum_count_mismatches_expected(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [
            {"host": "10.20.1.83", "roles": ["MON"]},
            {"host": "10.20.1.150", "roles": ["MON"]},
        ],
    )
    quorum_calls = {"n": 0}

    def fake_execute(host, command):
        if command.startswith("hostname"):
            return "node83.lab"
        if command == "ceph quorum_status --format json":
            quorum_calls["n"] += 1
            # Still 3 mons after removal (expected 2) — a real quorum problem.
            return json.dumps({"quorum_names": ["node83", "node150", "node200"]})
        if command.startswith("ceph mon rm"):
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_gate_remove_mon(
            ["10.20.1.83"], _gate_action_params("gate-1", roles=["MON"]), on_update
        )


def test_remove_mon_fails_when_only_one_mon_configured(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module, "configured_nodes", lambda: [{"host": "10.20.1.83", "roles": ["MON"]}]
    )
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_gate_remove_mon(
            ["10.20.1.83"], _gate_action_params("gate-1", roles=["MON"]), on_update
        )


# --- _phase_gate_mark_prepared -------------------------------------------


def test_mark_prepared_sets_state(gate_db):
    gate_id = _make_gate(gate_db)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_mark_prepared(
        ["10.20.1.83"], _gate_action_params(gate_id, roles=["OSD"]), on_update
    )

    assert _fetch_gate(gate_db, gate_id).state == NodeUpgradeGateState.PREPARED.value


# --- _rejoin_mon_after_reinstall -------------------------------------------


def test_rejoin_mon_happy_path_reaches_quorum(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module, "configured_nodes", lambda: [{"host": "10.20.1.83", "roles": ["MON"]}]
    )
    quorum_calls = {"n": 0}

    def fake_execute(host, command):
        if "ceph auth get mon." in command or "ceph mon getmap" in command:
            return ""
        if command.startswith("base64 "):
            return base64.b64encode(b"fake-bytes").decode()
        if "base64 -d" in command:
            return ""
        if command == "ceph quorum_status --format json":
            quorum_calls["n"] += 1
            names = ["node150"] if quorum_calls["n"] == 1 else ["node150", "node83"]
            return json.dumps({"quorum_names": names})
        if command.startswith("sed -i"):
            return ""
        if "mkfs" in command or "systemctl" in command:
            return ""
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_POLL_INTERVAL_SECONDS", 0)

    cluster_deploy_module._rejoin_mon_after_reinstall("10.20.1.83", "node83", "10.20.1.150")

    assert quorum_calls["n"] == 2


def test_rejoin_mon_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "configured_nodes", lambda: [])

    def fake_execute(host, command):
        if "ceph auth get mon." in command or "ceph mon getmap" in command:
            return ""
        if command.startswith("base64 "):
            return base64.b64encode(b"fake-bytes").decode()
        if "base64 -d" in command:
            return ""
        if command == "ceph quorum_status --format json":
            return json.dumps({"quorum_names": ["node150"]})  # never includes node83
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_DEFAULT_TIMEOUT_SECONDS", 0)

    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._rejoin_mon_after_reinstall("10.20.1.83", "node83", "10.20.1.150")


# --- _phase_gate_abort_maybe_clear_flags ----------------------------------


def test_abort_maybe_clear_flags_skips_when_another_gate_pending(gate_db, monkeypatch):
    with gate_db() as session:
        session.add(
            NodeUpgradeGate(
                id="other-gate", host="10.20.1.150", target_version="19.2.0",
                state=NodeUpgradeGateState.PREPARING.value,
            )
        )
        session.commit()

    def fake_execute(host, command):
        raise AssertionError("must not touch flags while another gate is pending")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_abort_maybe_clear_flags(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"], action_pk="abort-action-1"), on_update
    )


def test_abort_maybe_clear_flags_unsets_when_last_one(gate_db, monkeypatch):
    _make_gate(
        gate_db,
        state=NodeUpgradeGateState.ABORTING.value,
        abort_action_id="abort-action-1",
        maintenance_flags_added=json.dumps(["noscrub"]),
    )
    monkeypatch.setattr(
        cluster_deploy_module, "configured_nodes", lambda: [{"host": "10.20.1.150", "roles": ["MON"]}]
    )
    commands_run = []

    def fake_execute(host, command):
        if command == "ceph osd dump --format json":
            return json.dumps({"flags": "noout,noscrub,nodeep-scrub,nosnaptrim"})
        commands_run.append(command)
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_abort_maybe_clear_flags(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"], action_pk="abort-action-1"), on_update
    )

    assert commands_run == ["ceph osd unset noscrub"]
    assert _fetch_gate(gate_db, "gate-1").maintenance_flags_added == "[]"


# --- _phase_gate_abort_mark_done -------------------------------------------


def test_abort_mark_done_sets_state_and_releases_lock(gate_db):
    gate_id = _make_gate(gate_db, state=NodeUpgradeGateState.ABORTING.value)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_abort_mark_done(
        ["10.20.1.83"], _gate_action_params(gate_id, roles=["OSD"]), on_update
    )

    with gate_db() as session:
        assert session.get(NodeUpgradeGate, gate_id).state == NodeUpgradeGateState.DONE.value
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id is None


# --- run()'s failure-path gate cleanup (Task 5) ----------------------------


def test_run_marks_gate_failed_and_releases_lock_on_mid_phase_failure(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db)
    action_params = _gate_action_params(gate_id, roles=["MON"], nodes=["10.20.1.83"])
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata,
        "latest_successful_metadata_job",
        lambda: type("_J", (), {"created_at": datetime.utcnow()})(),
    )
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [{"host": "10.20.1.83", "roles": ["MON"]}],  # only 1 mon -> _phase_gate_remove_mon fails
    )
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda h, c: "")

    result = run(
        "action-pk-1", "node_os_gate_prepare", action_params, "incident-1",
        lambda pk, progress: None, lambda incident_id: False,
    )

    assert result is False
    with gate_db() as session:
        assert session.get(NodeUpgradeGate, gate_id).state == NodeUpgradeGateState.FAILED.value
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id is None


def test_failed_prepare_rollback_only_unsets_flags_it_added_and_rejoins_mon(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [
            {"host": "10.20.1.83", "roles": ["MON", "OSD"]},
            {"host": "10.20.1.150", "roles": ["MON"]},
        ],
    )
    rejoined = []
    unset_commands = []
    flag_read_hosts = []

    def fake_rejoin(host, mon_name, other_mon_host):
        rejoined.append((host, mon_name, other_mon_host))

    def fake_execute(host, command):
        if command.startswith("hostname"):
            return "node83.lab"
        if command == "ceph osd dump --format json":
            flag_read_hosts.append(host)
            return json.dumps({"flags": "noout,noscrub"})
        if command.startswith("ceph osd unset"):
            unset_commands.append(command)
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cluster_deploy_module, "_rejoin_mon_after_reinstall", fake_rejoin)
    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    action_params = {
        "host": "10.20.1.83",
        "_maintenance_flags_added": ["noscrub"],
        "_mon_removed": True,
    }

    assert cluster_deploy_module._rollback_failed_prepare(action_params) is True
    assert rejoined == [("10.20.1.83", "node83.lab", "10.20.1.150")]
    assert flag_read_hosts == ["10.20.1.150"]
    assert unset_commands == ["ceph osd unset noscrub"]
    assert action_params["_maintenance_flags_added"] == []
    assert action_params["_mon_removed"] is False


def test_prepare_rollback_markers_are_persisted_on_gate(gate_db):
    gate_id = _make_gate(gate_db)
    action_params = {"node_upgrade_gate_id": gate_id}

    cluster_deploy_module._persist_gate_marker(
        action_params, "_maintenance_flags_added", ["noout", "noscrub"]
    )
    cluster_deploy_module._persist_gate_marker(action_params, "_mon_removed", True)

    with gate_db() as session:
        gate = session.get(NodeUpgradeGate, gate_id)
        assert json.loads(gate.maintenance_flags_added) == ["noout", "noscrub"]
        assert gate.mon_removed is True

    recovered_flags, recovered_mon_removed = cluster_deploy_module._prepare_rollback_state(
        {"node_upgrade_gate_id": gate_id}
    )
    assert recovered_flags == ["noout", "noscrub"]
    assert recovered_mon_removed is True


def test_run_refuses_gate_without_incident_and_releases_lock(gate_db):
    gate_id = _make_gate(gate_db)
    action_params = _gate_action_params(gate_id, roles=["MON"], incident_id="missing-incident")

    result = run(
        "action-pk-1", "node_os_gate_prepare", action_params, "missing-incident",
        lambda pk, progress: None, lambda incident_id: False,
    )

    assert result is False
    with gate_db() as session:
        assert session.get(NodeUpgradeGate, gate_id).state == NodeUpgradeGateState.FAILED.value
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id is None


def test_gate_worker_rejects_gate_from_another_cluster(gate_db):
    gate_id = _make_gate(gate_db)
    action_params = _gate_action_params(
        gate_id, roles=["MON"], _gate_cluster=SimpleNamespace(id="cluster-a")
    )

    with gate_db() as session:
        with pytest.raises(DeployPhaseError, match="không thuộc cluster"):
            cluster_deploy_module._get_node_upgrade_gate_or_raise(session, gate_id, action_params)


def test_gate_worker_rejects_host_outside_selected_cluster(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db)
    monkeypatch.setattr(
        cluster_deploy_module, "configured_nodes",
        lambda: [{"host": "10.20.1.150", "roles": ["MON"]}],
    )
    action_params = _gate_action_params(gate_id, roles=["MON"], host="10.20.1.83")

    with gate_db() as session:
        with pytest.raises(DeployPhaseError, match="không nằm trong cấu hình cluster"):
            cluster_deploy_module._get_node_upgrade_gate_or_raise(session, gate_id, action_params)


def test_run_failure_cleanup_unblocks_a_later_prepare_attempt(gate_db):
    gate_id = _make_gate(gate_db)
    action_params = _gate_action_params(gate_id, roles=["OSD"], nodes=["10.20.1.83"])

    run(
        "action-pk-1", "node_os_gate_prepare", action_params, "incident-1",
        lambda pk, progress: None, lambda incident_id: True,
    )

    with gate_db() as session:
        assert claim_node_upgrade_gate_lock(session, "new-gate-id") is True


def test_run_does_not_touch_env_config_for_gate_action_ids(gate_db, monkeypatch):
    # AC #7: epilogue must be skipped entirely for these action_ids —
    # _write_cluster_config would crash anyway on nodes=[host] (a plain
    # string list, not the dict-list _node_ips_with_role expects), so this
    # also proves the epilogue-skip branch is actually reached.
    gate_id = _make_gate(gate_db)
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata,
        "latest_successful_metadata_job",
        lambda: type("_J", (), {"created_at": datetime.utcnow()})(),
    )
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda h, c: "")

    def fail_write_cluster_config(*a, **k):
        raise AssertionError("_write_cluster_config must not be called for node_os_gate_prepare")

    monkeypatch.setattr(cluster_deploy_module, "_write_cluster_config", fail_write_cluster_config)

    action_params = _gate_action_params(gate_id, roles=[], nodes=["10.20.1.83"])
    result = run(
        "action-pk-1", "node_os_gate_prepare", action_params, "incident-1",
        lambda pk, progress: None, lambda incident_id: False,
    )

    assert result is True


# ============================================================================
# Story 11.4 — node_os_gate_recover (Confirm & Node Recovery)
# ============================================================================

# --- _phase_gate_check_disk (FR-9) ------------------------------------------


def test_check_disk_skips_for_non_osd_role(monkeypatch):
    def fake_execute(host, command):
        raise AssertionError("must not run any command for a non-OSD node")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_check_disk(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["MON"]), on_update
    )
    assert calls[-1][0]["status"] == "done"


def test_check_disk_happy_path_pv_visible(monkeypatch):
    def fake_execute(host, command):
        assert "pvscan" in command
        return "some lsblk output\nCEPH_AIOPS_PV_OK\nCEPH_AIOPS_LV_ALL_ACTIVE\n"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_check_disk(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
    )

    assert calls[-1][0]["status"] == "done"


def test_check_disk_raises_when_lv_stays_inactive_after_vgchange(monkeypatch):
    # Code review fix: Task 1 requires CONFIRMING the LV reached ACTIVE
    # after the vgchange -ay repair attempt, not just running it.
    def fake_execute(host, command):
        return "CEPH_AIOPS_PV_OK\nCEPH_AIOPS_LV_INACTIVE\n"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError, match="inactive"):
        cluster_deploy_module._phase_gate_check_disk(
            ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
        )


def test_check_disk_raises_when_pv_still_missing_after_repair(monkeypatch):
    def fake_execute(host, command):
        assert "lvmdevices --adddev" in command  # repair attempt IS part of the command
        return "CEPH_AIOPS_PV_MISSING\n"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_gate_check_disk(
            ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
        )


def test_check_disk_raises_on_ssh_failure(monkeypatch):
    def fake_execute(host, command):
        raise ExecutorError("connection lost")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_gate_check_disk(
            ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
        )


# --- _phase_gate_configure_base (FR-10) -------------------------------------


def test_configure_base_happy_path(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [
            {"host": "10.20.1.83", "roles": ["MON", "OSD"]},
            {"host": "10.20.1.150", "roles": ["MON", "OSD"]},
        ],
    )
    commands_run = []

    def fake_execute(host, command):
        commands_run.append((host, command))
        if command.startswith("hostname"):
            return f"host-{host.split('.')[-1]}.lab"
        if "getenforce" in command:
            return ""
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_configure_base(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["MON", "OSD"]), on_update
    )

    assert calls[-1][0]["status"] == "done"
    assert any("/etc/hosts" in c for _h, c in commands_run)
    assert any("chrony" in c for _h, c in commands_run)
    assert any("getenforce" in c for _h, c in commands_run)


def test_configure_base_raises_when_verification_fails(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module, "configured_nodes", lambda: [{"host": "10.20.1.83", "roles": ["OSD"]}]
    )

    def fake_execute(host, command):
        if command.startswith("hostname"):
            return "host83.lab"
        if "getenforce" in command:
            return "CEPH_AIOPS_SELINUX_STILL_ENFORCING\n"
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError, match="SELinux chưa Disabled"):
        cluster_deploy_module._phase_gate_configure_base(
            ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
        )


def test_configure_base_verification_names_multiple_failed_checks(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module, "configured_nodes", lambda: [{"host": "10.20.1.83", "roles": ["OSD"]}]
    )

    def fake_execute(host, command):
        if command.startswith("hostname"):
            return "host83.lab"
        if "getenforce" in command:
            return "CEPH_AIOPS_FIREWALLD_STILL_ACTIVE\nCEPH_AIOPS_CHRONYD_NOT_ACTIVE\n"
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError) as excinfo:
        cluster_deploy_module._phase_gate_configure_base(
            ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
        )
    assert "firewalld vẫn active" in str(excinfo.value)
    assert "chronyd chưa active" in str(excinfo.value)
    assert "SELinux" not in str(excinfo.value)  # only the checks that actually failed are named


# --- _phase_gate_install_packages (FR-11) -----------------------------------


def test_install_packages_combined_role_no_pinning(monkeypatch):
    commands_run = []

    def fake_execute(host, command):
        commands_run.append(command)
        return "el8-repo-ok"

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_install_packages(
        ["10.20.1.83"],
        _gate_action_params("gate-1", roles=["MON", "OSD"], target_version="19.2.0"),
        on_update,
    )

    assert calls[-1][0]["status"] == "done"
    install_cmd = next(c for c in commands_run if "install" in c and "ceph-osd" in c)
    assert "ceph-osd-19.2.0" not in install_cmd  # non-Nautilus: unpinned
    assert "ceph-mon" in install_cmd and "ceph-osd" in install_cmd
    assert "fmt" in install_cmd and "python3-libs" in install_cmd
    devel_cmd = next(c for c in commands_run if "config-manager --set-enabled" in c)
    assert "powertools" in devel_cmd.lower() or "crb" in devel_cmd.lower()


def test_install_packages_nautilus_pins_exact_version(monkeypatch):
    commands_run = []

    def fake_execute(host, command):
        commands_run.append(command)
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_install_packages(
        ["10.20.1.83"],
        _gate_action_params("gate-1", roles=["OSD"], target_version="14.2.15"),
        on_update,
    )

    install_cmd = next(c for c in commands_run if "install" in c and "ceph-osd" in c)
    assert "ceph-osd-14.2.15" in install_cmd


def test_install_packages_raises_for_unrecognized_version(monkeypatch):
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_gate_install_packages(
            ["10.20.1.83"],
            _gate_action_params("gate-1", roles=["OSD"], target_version="0.0.0"),
            on_update,
        )


# --- _phase_gate_restore_config_and_keyring (FR-12) -------------------------


def test_restore_config_and_keyring_happy_path(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [
            {"host": "10.20.1.83", "roles": ["OSD"]},
            {"host": "10.20.1.150", "roles": ["MON"]},
        ],
    )
    commands_run = []

    def fake_execute(host, command):
        commands_run.append((host, command))
        if command.startswith("base64 "):
            return base64.b64encode(b"fake-conf-or-keyring").decode()
        if "base64 -d" in command:
            return ""
        if command == "ceph -s":
            return "cluster ok"
        if "ceph auth get client.bootstrap-osd" in command:
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_restore_config_and_keyring(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
    )

    assert calls[-1][0]["status"] == "done"
    # ceph -s must run BEFORE the bootstrap-osd fetch.
    ceph_s_index = next(i for i, (_h, c) in enumerate(commands_run) if c == "ceph -s")
    bootstrap_index = next(
        i for i, (_h, c) in enumerate(commands_run) if "bootstrap-osd" in c
    )
    assert ceph_s_index < bootstrap_index


def test_restore_config_and_keyring_stops_before_bootstrap_when_ceph_s_fails(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [{"host": "10.20.1.83", "roles": ["OSD"]}, {"host": "10.20.1.150", "roles": ["MON"]}],
    )

    def fake_execute(host, command):
        if command.startswith("base64 "):
            return base64.b64encode(b"fake").decode()
        if "base64 -d" in command:
            return ""
        if command == "ceph -s":
            raise ExecutorError("auth failed")
        raise AssertionError(f"must not reach bootstrap-osd fetch: {command}")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError) as excinfo:
        cluster_deploy_module._phase_gate_restore_config_and_keyring(
            ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
        )
    assert "fake" not in str(excinfo.value)  # never leaks keyring bytes into the error


# --- _phase_gate_activate_osd (FR-13) ---------------------------------------


def test_activate_osd_skips_for_non_osd_role():
    def fake_execute(host, command):
        raise AssertionError("must not run any command for a non-OSD node")

    on_update, calls = _record_recording_host_updates()
    cluster_deploy_module._phase_gate_activate_osd(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["MON"]), on_update
    )
    assert calls[-1][0]["status"] == "done"


def test_activate_osd_happy_path_matches_backed_up_ids(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db, osd_backup=json.dumps([{"osd_id": "0", "osd_fsid": "x"}]))
    commands_run = []

    def fake_execute(host, command):
        commands_run.append(command)
        if command == "ceph osd tree --format json":
            return json.dumps({"nodes": [{"id": "0", "type": "osd", "status": "up"}]})
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_activate_osd(
        ["10.20.1.83"], _gate_action_params(gate_id, roles=["OSD"]), on_update
    )

    assert calls[-1][0]["status"] == "done"
    assert any("ceph-volume lvm activate --all" == c for c in commands_run)
    assert any("ceph-osd@0" in c for c in commands_run)
    assert not any("osd in" in c for c in commands_run)  # never runs `ceph osd in`


def test_activate_osd_raises_without_backup_data(gate_db):
    gate_id = _make_gate(gate_db, osd_backup=None)
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_gate_activate_osd(
            ["10.20.1.83"], _gate_action_params(gate_id, roles=["OSD"]), on_update
        )


def test_activate_osd_raises_when_fewer_than_expected_are_up(gate_db, monkeypatch):
    gate_id = _make_gate(
        gate_db, osd_backup=json.dumps([{"osd_id": "0", "osd_fsid": "x"}, {"osd_id": "1", "osd_fsid": "y"}])
    )

    def fake_execute(host, command):
        if command == "ceph osd tree --format json":
            # id 1 never came up.
            return json.dumps({"nodes": [{"id": "0", "type": "osd", "status": "up"}]})
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    with pytest.raises(DeployPhaseError):
        cluster_deploy_module._phase_gate_activate_osd(
            ["10.20.1.83"], _gate_action_params(gate_id, roles=["OSD"]), on_update
        )


# --- _phase_gate_rejoin_mon (FR-14) -----------------------------------------


def test_rejoin_mon_phase_skips_for_non_mon_role():
    def fake_execute(host, command):
        raise AssertionError("must not run any command for a non-MON node")

    on_update, calls = _record_recording_host_updates()
    cluster_deploy_module._phase_gate_rejoin_mon(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"]), on_update
    )
    assert calls[-1][0]["status"] == "done"


def test_rejoin_mon_phase_calls_shared_helper_with_same_args(monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [{"host": "10.20.1.83", "roles": ["MON"]}, {"host": "10.20.1.150", "roles": ["MON"]}],
    )

    def fake_execute(host, command):
        if command.startswith("hostname"):
            return "node83.lab"
        raise AssertionError(f"unexpected command outside the shared helper: {command}")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)

    captured = {}

    def fake_rejoin(host, mon_name, other_mon_host):
        captured.update(host=host, mon_name=mon_name, other_mon_host=other_mon_host)

    monkeypatch.setattr(cluster_deploy_module, "_rejoin_mon_after_reinstall", fake_rejoin)
    on_update, calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_rejoin_mon(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["MON"]), on_update
    )

    assert calls[-1][0]["status"] == "done"
    assert captured == {"host": "10.20.1.83", "mon_name": "node83.lab", "other_mon_host": "10.20.1.150"}


# --- _phase_gate_maybe_clear_flags (FR-16) ----------------------------------


def test_recover_maybe_clear_flags_skips_when_another_gate_pending(gate_db, monkeypatch):
    with gate_db() as session:
        session.add(
            NodeUpgradeGate(
                id="other-gate", host="10.20.1.150", target_version="19.2.0",
                state=NodeUpgradeGateState.PREPARING.value,
            )
        )
        session.commit()

    def fake_execute(host, command):
        raise AssertionError("must not touch flags while another gate is pending")

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_maybe_clear_flags(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"], action_pk="recover-action-1"), on_update
    )


def test_recover_maybe_clear_flags_unsets_when_last_one(gate_db, monkeypatch):
    monkeypatch.setattr(
        cluster_deploy_module, "configured_nodes", lambda: [{"host": "10.20.1.150", "roles": ["MON"]}]
    )
    commands_run = []

    def fake_execute(host, command):
        if command == "ceph osd dump --format json":
            return json.dumps({"flags": "noout,noscrub,nodeep-scrub,nosnaptrim"})
        commands_run.append(command)
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_maybe_clear_flags(
        ["10.20.1.83"], _gate_action_params("gate-1", roles=["OSD"], action_pk="recover-action-1"), on_update
    )

    assert len(commands_run) == 1
    assert all(f in commands_run[0] for f in ("noout", "noscrub", "nodeep-scrub", "nosnaptrim"))


# --- _phase_gate_mark_recovered ----------------------------------------------


def test_mark_recovered_sets_done_and_releases_lock(gate_db):
    gate_id = _make_gate(gate_db, state=NodeUpgradeGateState.RECOVERING.value)
    on_update, _calls = _record_recording_host_updates()

    cluster_deploy_module._phase_gate_mark_recovered(
        ["10.20.1.83"], _gate_action_params(gate_id, roles=["OSD"]), on_update
    )

    with gate_db() as session:
        assert session.get(NodeUpgradeGate, gate_id).state == NodeUpgradeGateState.DONE.value
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id is None


# --- run() end-to-end for node_os_gate_recover ------------------------------


def _recover_dispatch_execute(host, command):
    """One big fake `execute_command` covering every SSH call the full
    node_os_gate_recover phase list issues for a combined MON+OSD node,
    happy-path only."""
    if "pvscan" in command:
        return "CEPH_AIOPS_PV_OK\nCEPH_AIOPS_LV_ALL_ACTIVE"
    if command.startswith("hostname"):
        return "node83.lab"
    if "getenforce" in command:
        return ""
    if "/etc/hosts" in command or "chrony" in command:
        return ""
    if "download.ceph.com" in command or "rhel_ver" in command or "install" in command:
        return ""
    if command.startswith("base64 "):
        return base64.b64encode(b"fake-bytes").decode()
    if "base64 -d" in command:
        return ""
    if command == "ceph -s":
        return "ok"
    if "bootstrap-osd" in command:
        return ""
    if command == "ceph-volume lvm activate --all" or "ceph-osd@" in command:
        return ""
    if command == "ceph osd tree --format json":
        return json.dumps({"nodes": [{"id": "0", "type": "osd", "status": "up"}]})
    if "ceph auth get mon." in command or "ceph mon getmap" in command:
        return ""
    if command == "ceph quorum_status --format json":
        return json.dumps({"quorum_names": ["node83.lab", "node150.lab"]})
    if command.startswith("sed -i"):
        return ""
    if "mkfs" in command or "systemctl enable --now ceph-mon" in command:
        return ""
    if command == "ceph osd dump --format json":
        return json.dumps({"flags": "noout,noscrub,nodeep-scrub,nosnaptrim"})
    if command.startswith("ceph osd unset"):
        return ""
    return ""


def test_run_node_os_gate_recover_happy_path_reaches_done(gate_db, monkeypatch):
    gate_id = _make_gate(
        gate_db,
        state=NodeUpgradeGateState.RECOVERING.value,
        osd_backup=json.dumps([{"osd_id": "0", "osd_fsid": "x"}]),
    )
    monkeypatch.setattr(
        cluster_deploy_module,
        "configured_nodes",
        lambda: [
            {"host": "10.20.1.83", "roles": ["MON", "OSD"]},
            {"host": "10.20.1.150", "roles": ["MON", "OSD"]},
        ],
    )
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _recover_dispatch_execute)
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_DEFAULT_TIMEOUT_SECONDS", 1)

    action_params = _gate_action_params(gate_id, roles=["MON", "OSD"], nodes=["10.20.1.83"])
    result = run(
        "action-pk-1", "node_os_gate_recover", action_params, "incident-1",
        lambda pk, progress: None, lambda incident_id: False,
    )

    assert result is True
    with gate_db() as session:
        assert session.get(NodeUpgradeGate, gate_id).state == NodeUpgradeGateState.DONE.value
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id is None


def test_run_marks_gate_failed_and_releases_lock_on_recover_mid_phase_failure(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db, state=NodeUpgradeGateState.RECOVERING.value)
    action_params = _gate_action_params(gate_id, roles=["OSD"], nodes=["10.20.1.83"])

    def fake_execute(host, command):
        if "pvscan" in command:
            return "CEPH_AIOPS_PV_MISSING"  # fails the very first phase
        return ""

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)

    result = run(
        "action-pk-1", "node_os_gate_recover", action_params, "incident-1",
        lambda pk, progress: None, lambda incident_id: False,
    )

    assert result is False
    with gate_db() as session:
        assert session.get(NodeUpgradeGate, gate_id).state == NodeUpgradeGateState.FAILED.value
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id is None


def test_run_recover_failure_cleanup_unblocks_a_later_prepare_attempt(gate_db, monkeypatch):
    gate_id = _make_gate(gate_db, state=NodeUpgradeGateState.RECOVERING.value)
    action_params = _gate_action_params(gate_id, roles=["OSD"], nodes=["10.20.1.83"])
    monkeypatch.setattr(cluster_deploy_module, "execute_command", lambda h, c: "CEPH_AIOPS_PV_MISSING")

    run(
        "action-pk-1", "node_os_gate_recover", action_params, "incident-1",
        lambda pk, progress: None, lambda incident_id: False,
    )

    with gate_db() as session:
        assert claim_node_upgrade_gate_lock(session, "new-gate-id") is True
