"""Classifying why a node stopped answering from its kernel journal (09/10/2026)."""

from datetime import datetime, timezone

import pytest

from watcher import node_postmortem as pm

OUTAGE = datetime(2026, 10, 9, 2, 26)  # naive UTC, as stored
BEFORE = int(datetime(2026, 10, 5, 2, 22, tzinfo=timezone.utc).timestamp())
AFTER = int(datetime(2026, 10, 9, 2, 29, tzinfo=timezone.utc).timestamp())


def _out(boot, *lines, persistent=True):
    return "\n".join([f"BOOT {boot}", f"PERSISTENT {1 if persistent else 0}", *lines])


def test_ceph2_on_09_10_is_a_memory_reclaim_soft_lockup_then_a_reset():
    result = pm.classify(_out(
        AFTER,
        "PREV 2026-10-09T09:26:31+0700 ceph2 kernel: watchdog: BUG: soft lockup - CPU#2 stuck for 24s! [kswapd0:74]",
        "PREV 2026-10-09T09:27:07+0700 ceph2 kernel: watchdog: BUG: soft lockup - CPU#3 stuck for 23s! [dnf:233163]",
        "PREV 2026-10-09T09:27:35+0700 ceph2 kernel: watchdog: BUG: soft lockup - CPU#1 stuck for 24s! "
        "[bstore_kv_sync:21421]",
        "PREV 2026-10-09T09:27:35+0700 ceph2 kernel: watchdog: BUG: soft lockup - CPU#2 stuck for 50s! [kswapd0:74]",
    ), OUTAGE)

    assert result.kind == pm.SOFT_LOCKUP and result.rebooted is True
    assert result.processes == ("kswapd0", "dnf", "bstore_kv_sync")
    assert "thu hồi bộ nhớ (kswapd0)" in result.summary and "khởi động lại" in result.summary


def test_a_stall_without_reboot_reads_the_outage_window_not_the_old_boot():
    result = pm.classify(_out(
        BEFORE,
        "PREV 2026-10-01 kernel: watchdog: BUG: soft lockup - CPU#0 stuck for 30s! [old:1]",
        "CUR 2026-10-09T09:27:00+0700 kernel: INFO: task bstore_kv_sync:2142 blocked for more than 122 seconds.",
    ), OUTAGE)

    assert result.kind == pm.HUNG_TASK and result.rebooted is False
    assert "tự hồi lại" in result.summary


@pytest.mark.parametrize(("line", "kind"), [
    ("PREV kernel: Out of memory: Killed process 4242 (ceph-osd)", pm.OUT_OF_MEMORY),
    ("PREV kernel: blk_update_request: I/O error, dev vdb, sector 2048", pm.DISK_IO_ERROR),
])
def test_oom_and_disk_errors(line, kind):
    assert pm.classify(_out(AFTER, line), OUTAGE).kind == kind


def test_a_reset_with_a_persistent_journal_but_no_kernel_trace():
    result = pm.classify(_out(AFTER, "LAST 2026-10-09T09:27:54+0700 ceph2 sshd[1]: Accepted publickey"), OUTAGE)

    assert result.kind == pm.RESET_NO_TRACE and "hypervisor" in result.summary
    assert result.evidence == ("2026-10-09T09:27:54+0700 ceph2 sshd[1]: Accepted publickey",)


def test_a_reset_without_a_persistent_journal_says_so():
    assert pm.classify(_out(AFTER, persistent=False), OUTAGE).kind == pm.REBOOTED_NO_JOURNAL


def test_no_reboot_and_no_kernel_trace_points_at_network_or_a_paused_vm():
    result = pm.classify(_out(BEFORE), OUTAGE)

    assert result.kind == pm.NO_KERNEL_TRACE and result.rebooted is False


def test_unreadable_output_is_unavailable():
    assert pm.classify("ssh: connect timed out", OUTAGE).kind == pm.UNAVAILABLE


def test_the_command_is_read_only_and_starts_before_the_outage():
    command = pm.postmortem_command(OUTAGE)

    assert f"--since @{int(OUTAGE.replace(tzinfo=timezone.utc).timestamp()) - 300}" in command
    for verb in ("rm ", "systemctl", "reboot", "echo n", "> /"):
        assert verb not in command


def test_collect_never_raises(monkeypatch):
    from watcher import ceph_client

    def broken(*args, **kwargs):
        raise TimeoutError("ssh timeout")

    monkeypatch.setattr(ceph_client, "run_command_on_node", broken)

    assert pm.collect("10.3.53.69", OUTAGE).kind == pm.UNAVAILABLE
