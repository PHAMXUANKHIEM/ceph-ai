import re

import pytest

import worker.executor.commands as commands_module
from worker.executor.commands import get_command
from worker.executor.ssh_executor import ExecutorError

# Real `systemctl | grep ceph` output shape (verified against a live
# cephadm/reef cluster) — one cephadm-managed OSD/MON/MGR plus assorted
# monitoring sidecar units that must NOT be mistaken for osd/mon/mgr.
CEPHADM_SYSTEMCTL_OUTPUT = (
    "  ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@ceph-exporter.khiempx-ceph1.service"
    "                                    loaded active running   Ceph ceph-exporter.khiempx-ceph1\n"
    "  ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@crash.khiempx-ceph1.service"
    "                                            loaded active running   Ceph crash.khiempx-ceph1\n"
    "  ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@grafana.khiempx-ceph1.service"
    "                                          loaded active running   Ceph grafana.khiempx-ceph1\n"
    "  ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@mgr.khiempx-ceph1.loylll.service"
    "                                       loaded active running   Ceph mgr.khiempx-ceph1.loylll\n"
    "  ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@mon.khiempx-ceph1.service"
    "                                              loaded active running   Ceph mon.khiempx-ceph1\n"
    "  ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@node-exporter.khiempx-ceph1.service"
    "                                    loaded active running   Ceph node-exporter.khiempx-ceph1\n"
    "  ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@osd.0.service"
    "                                                          loaded active running   Ceph osd.0\n"
    "  ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@prometheus.khiempx-ceph1.service"
    "                                       loaded active running   Ceph prometheus.khiempx-ceph1\n"
    "  system-ceph\\x2d48a9efa2\\x2d8404\\x2d11f1\\x2dac02\\x2dfa163ea23860.slice"
    "                                            loaded active active    Slice\n"
    "  ceph-48a9efa2-8404-11f1-ac02-fa163ea23860.target"
    "                                                                 loaded active active    Ceph cluster\n"
    "  ceph.target"
    "                                                                                        loaded active active    All Ceph clusters\n"
)


def test_get_command_returns_resync_ntp_command():
    command = get_command("resync_ntp")
    assert "chronyc" in command
    assert "ntpdate" in command
    assert "systemd-timesyncd" in command


def test_get_command_returns_crash_archive_all_command():
    assert get_command("crash_archive_all") == "ceph crash archive-all"


def test_get_command_raises_for_unknown_action_id():
    with pytest.raises(ExecutorError, match="no Command defined"):
        get_command("some_action_id_with_no_command")


def test_get_command_restart_osd_daemon_requires_host():
    with pytest.raises(ExecutorError, match="needs a specific host"):
        get_command("restart_osd_daemon")


def test_get_command_restart_osd_daemon_discovers_via_systemctl_and_restarts(monkeypatch):
    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        assert command == "systemctl | grep ceph || true"
        return CEPHADM_SYSTEMCTL_OUTPUT

    monkeypatch.setattr(commands_module, "execute_command", fake_execute)

    command = get_command("restart_osd_daemon", "10.20.1.112")

    assert calls == [("10.20.1.112", "systemctl | grep ceph || true")]
    assert command == (
        "systemctl restart ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@osd.0.service"
    )


# --- upgrade_ceph_cluster ----------------------------------------------------


def test_upgrade_ceph_cluster_requires_cephadm_exec_mode(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "docker")
    with pytest.raises(ExecutorError, match="cephadm"):
        get_command("upgrade_ceph_cluster", "10.20.1.150", {"target_version": "19.2.0"})


def test_upgrade_ceph_cluster_builds_expected_command(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "cephadm")

    command = get_command("upgrade_ceph_cluster", "10.20.1.150", {"target_version": "19.2.0"})

    assert command == "cephadm shell -- ceph orch upgrade start --ceph-version 19.2.0"


def test_upgrade_ceph_cluster_rejects_missing_target_version(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "cephadm")
    with pytest.raises(ExecutorError, match="target_version"):
        get_command("upgrade_ceph_cluster", "10.20.1.150", {})


def test_upgrade_ceph_cluster_rejects_malformed_target_version(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "cephadm")
    with pytest.raises(ExecutorError, match="target_version"):
        get_command(
            "upgrade_ceph_cluster", "10.20.1.150", {"target_version": "19.2.0; rm -rf /"}
        )


def test_has_command_true_for_upgrade_ceph_cluster():
    from worker.executor.commands import has_command

    assert has_command("upgrade_ceph_cluster") is True


# --- upgrade_ceph_cluster_package_download / _package_local -----------------
#
# ceph-deploy/traditional (ceph_exec_mode=none) package-based upgrade —
# these do a real SSH round trip (unit discovery, execute_command) as part
# of building the command, so every test here stubs execute_command the
# same way test_get_command_restart_osd_daemon_discovers_via_systemctl_and_restarts
# above does.

_MIXED_UNITS_OUTPUT = (
    "  ceph-mon@a.service"
    "                                                          loaded active running   Ceph mon.a\n"
    "  ceph-osd@0.service"
    "                                                          loaded active running   Ceph osd.0\n"
)


def test_package_download_requires_non_cephadm_exec_mode(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "cephadm")
    with pytest.raises(ExecutorError, match="ceph_exec_mode=none"):
        get_command(
            "upgrade_ceph_cluster_package_download", "10.20.1.150", {"target_version": "19.2.0"}
        )


def test_package_download_requires_host(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    with pytest.raises(ExecutorError, match="needs a specific host"):
        get_command("upgrade_ceph_cluster_package_download", None, {"target_version": "19.2.0"})


def test_package_download_rejects_malformed_target_version(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    with pytest.raises(ExecutorError, match="target_version"):
        get_command(
            "upgrade_ceph_cluster_package_download", "10.20.1.150", {"target_version": "not-a-version"}
        )


def test_package_download_rejects_unknown_release_codename(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    with pytest.raises(ExecutorError, match="codename"):
        get_command(
            "upgrade_ceph_cluster_package_download", "10.20.1.150", {"target_version": "99.0.0"}
        )


def test_package_download_builds_expected_command_and_restarts_discovered_units(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(commands_module, "execute_command", lambda host, cmd: _MIXED_UNITS_OUTPUT)

    command = get_command(
        "upgrade_ceph_cluster_package_download", "10.20.1.150", {"target_version": "19.2.0"}
    )

    # 2026-07-24 fix: verified live against download.ceph.com — repo path
    # uses the exact target_version, NOT the codename ("squid" here would
    # be a rolling alias that can silently drop OS support an older point
    # release still had — see the command builder's own comment). Also
    # needs the architecture segment (rpm-<version>/el<N>/ itself has no
    # repodata, only rpm-<version>/el<N>/<arch>/ does — verified live too).
    assert "debian-19.2.0" in command
    assert "rpm-19.2.0/el$(rpm -E %rhel)/$(uname -m)/" in command
    assert "apt-get install -y ceph" in command
    assert "dnf install -y ceph || yum install -y ceph" in command
    assert "(systemctl reset-failed ceph-mon@a.service 2>/dev/null; systemctl restart ceph-mon@a.service)" in command
    assert "(systemctl reset-failed ceph-osd@0.service 2>/dev/null; systemctl restart ceph-osd@0.service)" in command
    # 2026-07-24 regression test: `dnf/yum config-manager --add-repo` never
    # removes an old repo file — a retried/earlier-version attempt's stale
    # broken repo permanently blocks every future dnf/yum install on that
    # host otherwise (verified live: this exact scenario left 2+ stale
    # download.ceph.com_rpm-*.repo files on 3 real nodes).
    assert "rm -f /etc/yum.repos.d/download.ceph.com_rpm-*.repo" in command
    # 2026-07-24 regression test: ceph-mgr-modules-core (and other noarch
    # deps) live under a SEPARATE .../noarch/ repo, not the arch-specific
    # one — verified live: `dnf install ceph` failed with "nothing provides
    # ceph-mgr-modules-core" until this repo was also added.
    assert "rpm-19.2.0/el$(rpm -E %rhel)/noarch/" in command


def test_package_download_nautilus_uses_codename_repo_path_but_pins_exact_version(monkeypatch):
    """Regression, 2026-07-27: verified live against download.ceph.com —
    unlike every later release, Nautilus (14.x) was NEVER published under a
    per-exact-version REPO PATH (rpm-14.2.22/el8/ -> 404) — only the
    rpm-nautilus/debian-nautilus codename alias exists, safe to use forever
    since Nautilus is long EOL. But that alias repo physically hosts EVERY
    Nautilus point release's RPMs side by side (14.2.10 through 14.2.22) and
    advertises all of them in its metadata — a bare `dnf install ceph`
    against it silently resolves to whichever is numerically newest
    (14.2.22), regardless of target_version. So the exact version MUST
    still be pinned in the PACKAGE NAME (`ceph-14.2.22`) even though the
    repo PATH uses the codename. Every other release keeps using the exact
    version for both (see
    test_package_download_builds_expected_command_and_restarts_discovered_units
    above) since its repo is already scoped to one version."""
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(commands_module, "execute_command", lambda host, cmd: _MIXED_UNITS_OUTPUT)

    command = get_command(
        "upgrade_ceph_cluster_package_download", "10.20.1.150", {"target_version": "14.2.22"}
    )

    assert "debian-nautilus/" in command
    assert "rpm-nautilus/el$(rpm -E %rhel)/$(uname -m)/" in command
    assert "rpm-nautilus/el$(rpm -E %rhel)/noarch/" in command
    assert "dnf install -y ceph-14.2.22 || yum install -y ceph-14.2.22" in command
    assert "rpm-14.2.22" not in command


def test_package_download_with_no_discovered_units_skips_restart(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(commands_module, "execute_command", lambda host, cmd: "")

    command = get_command(
        "upgrade_ceph_cluster_package_download", "10.20.1.150", {"target_version": "19.2.0"}
    )

    assert "systemctl restart" not in command
    assert "apt-get install -y ceph" in command


def test_package_local_requires_non_cephadm_exec_mode(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "cephadm")
    with pytest.raises(ExecutorError, match="ceph_exec_mode=none"):
        get_command(
            "upgrade_ceph_cluster_package_local", "10.20.1.150", {"package_dir": "/opt/pkgs"}
        )


def test_package_local_rejects_missing_package_dir(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    with pytest.raises(ExecutorError, match="package_dir"):
        get_command("upgrade_ceph_cluster_package_local", "10.20.1.150", {})


def test_package_local_rejects_relative_path(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    with pytest.raises(ExecutorError, match="package_dir"):
        get_command(
            "upgrade_ceph_cluster_package_local",
            "10.20.1.150",
            {"package_dir": "relative/path"},
        )


def test_package_local_rejects_shell_injection_attempt(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    with pytest.raises(ExecutorError, match="package_dir"):
        get_command(
            "upgrade_ceph_cluster_package_local",
            "10.20.1.150",
            {"package_dir": "/opt/pkgs; rm -rf /"},
        )


def test_package_local_builds_expected_command_and_restarts_discovered_units(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(commands_module, "execute_command", lambda host, cmd: _MIXED_UNITS_OUTPUT)

    command = get_command(
        "upgrade_ceph_cluster_package_local", "10.20.1.150", {"package_dir": "/opt/ceph-pkgs"}
    )

    assert "[ -d /opt/ceph-pkgs ]" in command
    assert "apt-get install -y /opt/ceph-pkgs/*.deb" in command
    assert "dnf install -y /opt/ceph-pkgs/*.rpm || yum localinstall -y /opt/ceph-pkgs/*.rpm" in command
    assert "(systemctl reset-failed ceph-mon@a.service 2>/dev/null; systemctl restart ceph-mon@a.service)" in command
    assert "(systemctl reset-failed ceph-osd@0.service 2>/dev/null; systemctl restart ceph-osd@0.service)" in command


def test_has_command_true_for_both_package_based_upgrade_action_ids():
    assert commands_module.has_command("upgrade_ceph_cluster_package_download") is True
    assert commands_module.has_command("upgrade_ceph_cluster_package_local") is True


def test_discover_ceph_units_also_classifies_mds_and_rgw(monkeypatch):
    output = (
        "  ceph-mds@a.service"
        "                                                          loaded active running   Ceph mds.a\n"
        "  ceph-radosgw@rgw.a.service"
        "                                                  loaded active running   Ceph rgw.a\n"
    )
    monkeypatch.setattr(commands_module, "execute_command", lambda host, command: output)

    units = commands_module._discover_ceph_units("10.20.1.200")

    assert units["mds"] == ["ceph-mds@a.service"]
    assert units["rgw"] == ["ceph-radosgw@rgw.a.service"]


def test_get_command_restart_osd_daemon_works_for_traditional_non_cephadm_unit_names(monkeypatch):
    # "bất kể cụm nào" — a plain package-install deployment names its units
    # differently (type BEFORE the "@", no fsid wrapper) but must still work,
    # since discovery is a substring match on the whole unit name, not a
    # cephadm-specific positional parse.
    monkeypatch.setattr(
        commands_module,
        "execute_command",
        lambda host, command: "  ceph-osd@0.service   loaded active running   Ceph object storage daemon osd.0\n",
    )

    command = get_command("restart_osd_daemon", "10.20.1.150")

    assert command == "systemctl restart ceph-osd@0.service"


def test_get_command_restart_osd_daemon_chains_multiple_osd_units(monkeypatch):
    monkeypatch.setattr(
        commands_module,
        "execute_command",
        lambda host, command: (
            "  ceph-osd@0.service   loaded active running   x\n"
            "  ceph-osd@5.service   loaded active running   x\n"
        ),
    )

    command = get_command("restart_osd_daemon", "10.20.1.112")

    assert command == "systemctl restart ceph-osd@0.service && systemctl restart ceph-osd@5.service"


def test_get_command_restart_osd_daemon_raises_when_no_osd_unit_found(monkeypatch):
    monkeypatch.setattr(
        commands_module,
        "execute_command",
        lambda host, command: "  ceph-mon@khiempx-mon1.service   loaded active running   x\n",
    )

    with pytest.raises(ExecutorError, match="no ceph osd systemd unit found"):
        get_command("restart_osd_daemon", "10.20.1.112")


def test_get_command_restart_osd_daemon_raises_when_host_has_no_ceph_units_at_all(monkeypatch):
    # `systemctl | grep ceph` legitimately exits 1 (no matches) on a host
    # with none — execute_command must not raise for that (see the `|| true`
    # in the command itself), and get_command must still report a clear
    # "nothing found" error rather than crash on empty output.
    monkeypatch.setattr(commands_module, "execute_command", lambda host, command: "")

    with pytest.raises(ExecutorError, match="no ceph osd systemd unit found"):
        get_command("restart_osd_daemon", "10.20.1.112")


def test_get_command_create_pool_builds_expected_command():
    command = get_command("create_pool", params={"pool_name": "rbd_data", "pg_num": 128})
    assert command == "ceph osd pool create rbd_data 128"


def test_get_command_create_pool_raises_without_pg_num():
    with pytest.raises(ExecutorError, match="pg_num"):
        get_command("create_pool", params={"pool_name": "rbd_data"})


def test_get_command_create_pool_raises_on_out_of_range_pg_num():
    with pytest.raises(ExecutorError, match="out of allowed range"):
        get_command("create_pool", params={"pool_name": "rbd_data", "pg_num": 999999999})


def test_get_command_delete_pool_requires_pool_name_twice_and_confirmation_flag():
    command = get_command("delete_pool", params={"pool_name": "old_pool"})
    assert (
        "ceph osd pool delete old_pool old_pool --yes-i-really-really-mean-it" in command
    )


def test_get_command_delete_pool_enables_and_restores_mon_allow_pool_delete():
    # 2026-07-23: Ceph itself refuses to delete a pool unless
    # mon_allow_pool_delete=true — verified live against the real cluster.
    # Operator's explicit request: enable it right before the delete,
    # restore it to false right after, regardless of whether the delete
    # itself succeeded.
    command = get_command("delete_pool", params={"pool_name": "old_pool"})
    assert command == (
        "ceph config set mon mon_allow_pool_delete true && "
        "(ceph osd pool delete old_pool old_pool --yes-i-really-really-mean-it; rc=$?; "
        "ceph config set mon mon_allow_pool_delete false || "
        "echo 'WARNING: failed to reset mon_allow_pool_delete to false' >&2; "
        "exit $rc)"
    )
    # Enabling comes first, gated with && (never attempts delete if this
    # itself fails), and the restore-to-false runs unconditionally after
    # the delete regardless of its outcome (the `;` before `rc=$?`, not `&&`).
    enable_idx = command.index("mon_allow_pool_delete true")
    delete_idx = command.index("ceph osd pool delete")
    restore_idx = command.rindex("mon_allow_pool_delete false")
    assert enable_idx < delete_idx < restore_idx
    # The subshell's own exit status is the delete's exit code ($rc), not
    # the restore command's — so a failed delete still reports FAILED even
    # though the restore-to-false step ran successfully afterward.
    assert command.rstrip().endswith("exit $rc)")


def test_get_command_delete_pool_rejects_pool_name_that_looks_like_a_flag():
    with pytest.raises(ExecutorError, match="invalid or missing pool_name"):
        get_command("delete_pool", params={"pool_name": "--yes-i-really-really-mean-it"})


def test_get_command_delete_pool_rejects_pool_name_with_shell_metacharacters():
    with pytest.raises(ExecutorError, match="invalid or missing pool_name"):
        get_command("delete_pool", params={"pool_name": "pool; rm -rf /"})


def test_get_command_set_pool_size_builds_expected_command():
    command = get_command("set_pool_size", params={"pool_name": "rbd_data", "size": 3})
    assert command == "ceph osd pool set rbd_data size 3"


def test_get_command_set_pool_size_rejects_non_integer():
    with pytest.raises(ExecutorError, match="must be an integer"):
        get_command("set_pool_size", params={"pool_name": "rbd_data", "size": "3"})


def test_get_command_set_pool_pg_num_builds_expected_command():
    command = get_command("set_pool_pg_num", params={"pool_name": "rbd_data", "pg_num": 64})
    assert command == "ceph osd pool set rbd_data pg_num 64"


def test_get_command_mark_osd_out_in_down_build_expected_commands():
    assert get_command("mark_osd_out", params={"osd_id": 7}) == "ceph osd out 7"
    assert get_command("mark_osd_in", params={"osd_id": 7}) == "ceph osd in 7"
    assert get_command("mark_osd_down", params={"osd_id": 7}) == "ceph osd down 7"


def test_get_command_mark_osd_out_rejects_negative_osd_id():
    with pytest.raises(ExecutorError, match="out of allowed range"):
        get_command("mark_osd_out", params={"osd_id": -1})


def test_get_command_mark_osd_out_rejects_bool_as_int():
    # bool is a subclass of int in Python — must not silently pass as a
    # valid osd_id (isinstance(True, int) is True).
    with pytest.raises(ExecutorError, match="must be an integer"):
        get_command("mark_osd_out", params={"osd_id": True})


def test_get_command_management_action_with_no_params_raises_clearly():
    with pytest.raises(ExecutorError, match="invalid or missing pool_name"):
        get_command("create_pool")


def test_get_command_enable_pool_application_builds_expected_command():
    command = get_command(
        "enable_pool_application", params={"pool_name": "rbd_data", "app_name": "rbd"}
    )
    assert command == "ceph osd pool application enable rbd_data rbd --yes-i-really-mean-it"


def test_get_command_enable_pool_application_rejects_invalid_app_name():
    with pytest.raises(ExecutorError, match="invalid or missing app_name"):
        get_command(
            "enable_pool_application", params={"pool_name": "rbd_data", "app_name": "--force"}
        )


def test_get_command_enable_pool_application_requires_pool_name():
    with pytest.raises(ExecutorError, match="invalid or missing pool_name"):
        get_command("enable_pool_application", params={"app_name": "rbd"})


def test_get_command_rbd_trash_remove_builds_expected_command():
    command = get_command(
        "rbd_trash_remove", params={"pool_name": "vms", "trash_id": "1234567890ab"}
    )
    assert command == "rbd trash rm vms/1234567890ab"


def test_get_command_rbd_trash_remove_rejects_trash_id_that_looks_like_a_flag():
    with pytest.raises(ExecutorError, match="invalid or missing trash_id"):
        get_command("rbd_trash_remove", params={"pool_name": "vms", "trash_id": "--force"})


def test_get_command_rbd_trash_remove_rejects_trash_id_with_shell_metacharacters():
    with pytest.raises(ExecutorError, match="invalid or missing trash_id"):
        get_command(
            "rbd_trash_remove", params={"pool_name": "vms", "trash_id": "abc; rm -rf /"}
        )


def test_get_command_rbd_trash_remove_requires_pool_name():
    with pytest.raises(ExecutorError, match="invalid or missing pool_name"):
        get_command("rbd_trash_remove", params={"trash_id": "1234567890ab"})


def test_has_command_true_for_action_ids_with_a_real_command():
    assert commands_module.has_command("resync_ntp") is True
    assert commands_module.has_command("restart_osd_daemon") is True
    assert commands_module.has_command("create_pool") is True
    assert commands_module.has_command("enable_pool_application") is True
    assert commands_module.has_command("rbd_trash_remove") is True


def test_has_command_false_for_action_ids_with_no_automated_remediation():
    assert commands_module.has_command("investigate_manually") is False
    assert commands_module.has_command("pg_repair_force") is False
    assert commands_module.has_command("this_action_id_does_not_exist") is False


def test_has_command_true_for_restore_cluster_from_backup():
    # Story 9.7: without an entry, approve_action's has_command() gate
    # would silently mark this Action EXECUTED without ever reaching
    # cluster_deploy.run() — same bug class the 2026-07-29 fix (see
    # _VOLUME_PERF_COMMAND_BUILDERS's own comment) fixed for volume_perf_sweep.
    assert commands_module.has_command("restore_cluster_from_backup") is True


def test_get_command_restore_cluster_from_backup_builds_preview_text():
    preview = commands_module.get_command(
        "restore_cluster_from_backup",
        None,
        {"version": "18.2.8", "nodes": [{"ip": "10.20.1.112", "roles": ["mon"]}]},
    )
    assert "18.2.8" in preview
    assert "10.20.1.112" in preview


def test_has_command_true_for_restore_rbd_image_to_production():
    assert commands_module.has_command("restore_rbd_image_to_production") is True


def test_get_command_restore_rbd_image_to_production_builds_preview_text():
    preview = commands_module.get_command(
        "restore_rbd_image_to_production", None, {"pool": "vms", "image": "web01"}
    )
    assert "vms/web01" in preview


def test_has_command_false_for_backup_delete_manual_not_yet_implemented():
    # backup_delete_manual is registered in the policy enum (Story 9.1) but
    # has no engine.py execution and no preview builder yet — has_command()
    # correctly still reports False for it (same not-yet-wired posture as
    # any other registered-but-unimplemented action_id).
    assert commands_module.has_command("backup_delete_manual") is False


def test_discover_ceph_units_classifies_osd_mon_mgr_and_ignores_sidecar_units(monkeypatch):
    monkeypatch.setattr(commands_module, "execute_command", lambda host, command: CEPHADM_SYSTEMCTL_OUTPUT)

    units = commands_module._discover_ceph_units("10.20.1.112")

    assert units["osd"] == ["ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@osd.0.service"]
    assert units["mon"] == ["ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@mon.khiempx-ceph1.service"]
    assert units["mgr"] == [
        "ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@mgr.khiempx-ceph1.loylll.service"
    ]


# --- patch_build_and_stage / patch_install (2026-07-24) ---------------------
#
# Ceph patch build & deploy pipeline (dashboard/routes/patch.py). Same "stub
# execute_command" posture as the package-based upgrade tests above —
# patch_install does a real unit-discovery SSH round trip just like those do.


def _set_patch_build_settings(monkeypatch, *, mon_nodes="10.20.1.150"):
    monkeypatch.setattr(commands_module.settings, "ceph_patch_source_dir", "/root/ceph")
    monkeypatch.setattr(
        commands_module.settings, "ceph_patch_build_command", "./make-srpm.sh && rpmbuild --rebuild x.src.rpm"
    )
    monkeypatch.setattr(commands_module.settings, "ceph_patch_output_dir", "/root/rpmbuild/RPMS/x86_64")
    monkeypatch.setattr(commands_module.settings, "ceph_patch_node_staging_dir", "/opt/ceph-aiops-patch-staging")
    monkeypatch.setattr(commands_module.settings, "ceph_mon_nodes", mon_nodes)
    monkeypatch.setattr(commands_module.settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(commands_module.settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(commands_module.settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(commands_module.settings, "ssh_user", "root")
    monkeypatch.setattr(commands_module.settings, "ssh_key_path", "/root/.ssh/ceph_lab_watcher")


def test_patch_build_and_stage_requires_host(monkeypatch):
    _set_patch_build_settings(monkeypatch)
    with pytest.raises(ExecutorError, match="build server host"):
        get_command("patch_build_and_stage", None, {"patch_content": "diff\n"})


def test_patch_build_and_stage_requires_patch_content(monkeypatch):
    _set_patch_build_settings(monkeypatch)
    with pytest.raises(ExecutorError, match="patch_content"):
        get_command("patch_build_and_stage", "10.9.9.9", {})


def test_patch_build_and_stage_requires_patch_content_non_blank(monkeypatch):
    _set_patch_build_settings(monkeypatch)
    with pytest.raises(ExecutorError, match="patch_content"):
        get_command("patch_build_and_stage", "10.9.9.9", {"patch_content": "   "})


@pytest.mark.parametrize(
    "field",
    ["ceph_patch_source_dir", "ceph_patch_build_command", "ceph_patch_output_dir", "ceph_patch_node_staging_dir"],
)
def test_patch_build_and_stage_requires_all_settings_configured(monkeypatch, field):
    _set_patch_build_settings(monkeypatch)
    monkeypatch.setattr(commands_module.settings, field, "")
    with pytest.raises(ExecutorError, match="ceph_patch_"):
        get_command("patch_build_and_stage", "10.9.9.9", {"patch_content": "diff\n"})


def test_patch_build_and_stage_requires_configured_ceph_nodes(monkeypatch):
    _set_patch_build_settings(monkeypatch, mon_nodes="")
    with pytest.raises(ExecutorError, match="no Ceph nodes configured"):
        get_command("patch_build_and_stage", "10.9.9.9", {"patch_content": "diff\n"})


def test_patch_build_and_stage_builds_expected_command(monkeypatch):
    _set_patch_build_settings(monkeypatch, mon_nodes="10.20.1.150,10.20.1.151")

    command = get_command("patch_build_and_stage", "10.9.9.9", {"patch_content": "diff --git a b\n+x\n"})

    # patch content round-trips through the embedded base64 payload
    import base64

    match = re.search(r"base64 -d > '?/root/ceph/ceph-aiops-current\.patch'? <<< '?(\S+)'?", command)
    assert match is not None
    assert base64.b64decode(match.group(1)).decode() == "diff --git a b\n+x\n"

    assert "cd /root/ceph && git apply --check ceph-aiops-current.patch" in command
    assert "git apply ceph-aiops-current.patch" in command
    assert "./make-srpm.sh && rpmbuild --rebuild x.src.rpm" in command
    assert (
        "scp -o StrictHostKeyChecking=accept-new -i /root/.ssh/ceph_lab_watcher "
        "/root/rpmbuild/RPMS/x86_64/*.rpm root@10.20.1.150:/opt/ceph-aiops-patch-staging/" in command
    )
    assert (
        "scp -o StrictHostKeyChecking=accept-new -i /root/.ssh/ceph_lab_watcher "
        "/root/rpmbuild/RPMS/x86_64/*.rpm root@10.20.1.151:/opt/ceph-aiops-patch-staging/" in command
    )


def test_patch_install_requires_non_cephadm_exec_mode(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "cephadm")
    with pytest.raises(ExecutorError, match="ceph_exec_mode=none"):
        get_command("patch_install", "10.20.1.150", {})


def test_patch_install_requires_host(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    with pytest.raises(ExecutorError, match="specific host"):
        get_command("patch_install", None, {})


def test_patch_install_requires_staging_dir_configured(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(commands_module.settings, "ceph_patch_node_staging_dir", "")
    with pytest.raises(ExecutorError, match="ceph_patch_node_staging_dir"):
        get_command("patch_install", "10.20.1.150", {})


def test_patch_install_builds_expected_command_and_restarts_discovered_units(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(commands_module.settings, "ceph_patch_node_staging_dir", "/opt/ceph-aiops-patch-staging")
    monkeypatch.setattr(commands_module, "execute_command", lambda host, cmd: _MIXED_UNITS_OUTPUT)

    command = get_command("patch_install", "10.20.1.150", {})

    assert "[ -d /opt/ceph-aiops-patch-staging ]" in command
    assert "apt-get install -y /opt/ceph-aiops-patch-staging/*.deb" in command
    assert (
        "dnf install -y /opt/ceph-aiops-patch-staging/*.rpm || "
        "yum localinstall -y /opt/ceph-aiops-patch-staging/*.rpm" in command
    )
    assert "(systemctl reset-failed ceph-mon@a.service 2>/dev/null; systemctl restart ceph-mon@a.service)" in command
    assert "(systemctl reset-failed ceph-osd@0.service 2>/dev/null; systemctl restart ceph-osd@0.service)" in command


def test_has_command_true_for_both_patch_action_ids():
    assert commands_module.has_command("patch_build_and_stage") is True
    assert commands_module.has_command("patch_install") is True


# --- Dựng cụm Ceph tự động (preview builders only — real execution is
# worker/executor/cluster_deploy.py, not this module) -----------------------

_DEPLOY_PARAMS = {
    "version": "18.2.8",
    "rpm_path": "/opt/ceph-rpms",
    "nodes": [
        {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
        {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"]},
        {"ip": "10.20.1.21", "roles": ["mon", "osd"]},
    ],
}


def test_deploy_cluster_cephadm_preview_mentions_version_and_first_mon():
    command = get_command("deploy_cluster_cephadm", None, _DEPLOY_PARAMS)
    assert "18.2.8" in command
    assert "10.20.1.112" in command
    assert "cephadm bootstrap" in command


def test_deploy_cluster_ceph_deploy_preview_mentions_version_and_first_mon():
    command = get_command("deploy_cluster_ceph_deploy", None, _DEPLOY_PARAMS)
    assert "18.2.8" in command
    assert "10.20.1.112" in command


def test_deploy_cluster_rpm_local_preview_mentions_rpm_path():
    command = get_command("deploy_cluster_rpm_local", None, _DEPLOY_PARAMS)
    assert "/opt/ceph-rpms" in command
    assert "10.20.1.112" in command


def test_deploy_cluster_preview_falls_back_to_host_when_no_mon_in_params():
    command = get_command("deploy_cluster_cephadm", "10.20.1.200", {"version": "18.2.8", "nodes": []})
    assert "10.20.1.200" in command


def test_has_command_true_for_all_deploy_cluster_action_ids():
    assert commands_module.has_command("deploy_cluster_cephadm") is True
    assert commands_module.has_command("deploy_cluster_ceph_deploy") is True
    assert commands_module.has_command("deploy_cluster_rpm_local") is True


@pytest.mark.parametrize("action_id", ["delete_cluster_cephadm", "delete_cluster_manual"])
def test_delete_cluster_preview_mentions_wipe_choice(action_id):
    # Both action_ids share the same preview builder — verified live,
    # 2026-07-27: the teardown mechanism is identical regardless of
    # exec_mode (cephadm's own rm-cluster turned out unreliable).
    command = get_command(action_id, None, {"wipe_osd_disks": True})
    assert "ceph-volume lvm zap" in command
    command = get_command(action_id, None, {"wipe_osd_disks": False})
    assert "không xoá dữ liệu đĩa OSD" in command


def test_has_command_true_for_all_delete_cluster_action_ids():
    assert commands_module.has_command("delete_cluster_cephadm") is True
    assert commands_module.has_command("delete_cluster_manual") is True


def test_convert_cluster_to_cephadm_preview_mentions_version_and_first_mon():
    command = get_command("convert_cluster_to_cephadm", None, _DEPLOY_PARAMS)
    assert "18.2.8" in command
    assert "10.20.1.112" in command
    assert "cephadm adopt" in command


def test_has_command_true_for_convert_cluster_to_cephadm():
    assert commands_module.has_command("convert_cluster_to_cephadm") is True


# --- Volumes "Đo hiệu năng tối đa" (preview builder only — real execution
# is worker/executor/volume_perf.py, not this module) -----------------------


def test_volume_perf_sweep_preview_mentions_pool_and_mon_ip():
    command = get_command("volume_perf_sweep", None, {"pool": "vms", "mon_ip": "10.20.1.112"})
    assert "vms" in command
    assert "10.20.1.112" in command
    assert "fio" in command


def test_volume_perf_sweep_preview_falls_back_to_host_when_no_mon_ip_in_params():
    command = get_command("volume_perf_sweep", "10.20.1.200", {"pool": "vms"})
    assert "10.20.1.200" in command


def test_has_command_true_for_volume_perf_sweep():
    # Regression, 2026-07-29 (verified live): dashboard/routes/actions.py::
    # approve_action checks has_command() BEFORE ever setting
    # Action.status=APPROVED — without an entry here, approving a
    # volume_perf_sweep proposal silently marked it EXECUTED with nothing
    # ever run (worker/llm/router_client.py's poll only ever looks at
    # status=APPROVED), and just redirected to "/" with no visible error.
    assert commands_module.has_command("volume_perf_sweep") is True
