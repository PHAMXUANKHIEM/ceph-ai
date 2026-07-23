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


def test_has_command_true_for_action_ids_with_a_real_command():
    assert commands_module.has_command("resync_ntp") is True
    assert commands_module.has_command("restart_osd_daemon") is True
    assert commands_module.has_command("create_pool") is True
    assert commands_module.has_command("enable_pool_application") is True


def test_has_command_false_for_action_ids_with_no_automated_remediation():
    assert commands_module.has_command("investigate_manually") is False
    assert commands_module.has_command("pg_repair_force") is False
    assert commands_module.has_command("this_action_id_does_not_exist") is False


def test_discover_ceph_units_classifies_osd_mon_mgr_and_ignores_sidecar_units(monkeypatch):
    monkeypatch.setattr(commands_module, "execute_command", lambda host, command: CEPHADM_SYSTEMCTL_OUTPUT)

    units = commands_module._discover_ceph_units("10.20.1.112")

    assert units["osd"] == ["ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@osd.0.service"]
    assert units["mon"] == ["ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@mon.khiempx-ceph1.service"]
    assert units["mgr"] == [
        "ceph-48a9efa2-8404-11f1-ac02-fa163ea23860@mgr.khiempx-ceph1.loylll.service"
    ]
