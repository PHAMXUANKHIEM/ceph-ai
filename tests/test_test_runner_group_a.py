"""Epic 10 Story 10.3: tests for the 11 Group A (RUN) test cases. Fakes
ssh_executor at the group_a/framework module boundary (run_ceph_command /
execute_background) rather than paramiko itself, matching this project's
existing hand-rolled-fake convention.
"""

import json
from datetime import datetime, timedelta

import pytest

from worker.executor.ssh_executor import ExecutorError
from worker.executor.test_runner import framework as fw
from worker.executor.test_runner import group_a as ga
from tests.test_test_runner_framework import FakeHandle


def _ctx(**overrides):
    defaults = dict(
        mon_host="mon1",
        osd_hosts=["osd1", "osd2"],
        rgw_hosts=["rgw1"],
        client_host=None,
        rgw_endpoint_zone_a=None,
        rgw_endpoint_zone_b=None,
        rgw_endpoint_vip=None,
    )
    defaults.update(overrides)
    return fw.TestRunContext(**defaults)


def _fake_run_ceph_command(monkeypatch, responses):
    """responses: dict[command_substring_or_exact] -> output string, checked
    in insertion order against the exact command; falls back to substring
    match so tests don't have to spell out every exact command string."""

    def fake(host, command):
        for key, value in responses.items():
            if key == command or key in command:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"no fake response configured for command: {command!r}")

    monkeypatch.setattr(ga, "run_ceph_command", fake)


# ---------------------------------------------------------------------------
# TC-RUN-001
# ---------------------------------------------------------------------------


class TestTcRun001:
    def test_start_requires_client_host(self):
        test_case = ga.TcRun001ContinuousRbdIo()
        with pytest.raises(fw.TestCaseError):
            test_case.start(_ctx(client_host=None))

    def test_start_launches_fio_via_execute_background(self, monkeypatch):
        captured = {}

        def fake_execute_background(host, command):
            captured["host"] = host
            captured["command"] = command
            return FakeHandle()

        monkeypatch.setattr(ga, "execute_background", fake_execute_background)
        test_case = ga.TcRun001ContinuousRbdIo()
        state = test_case.start(_ctx(client_host="client1"))
        assert captured["host"] == "client1"
        assert "fio" in captured["command"]
        assert ga.TcRun001ContinuousRbdIo.CLIENT_DEVICE in captured["command"]
        assert state["error_seen"] is False

    def test_poll_clean_run_keeps_first_criterion_open(self):
        test_case = ga.TcRun001ContinuousRbdIo()
        handle = FakeHandle(stdout_chunks=["iops=5000"], done=False)
        new_state, result = fw.poll_test_case(test_case, _ctx(), {"handle": handle, "error_seen": False})
        assert result.status == fw.TestStatus.RUNNING
        assert result.criteria[0].passed is None
        assert new_state["error_seen"] is False

    def test_poll_io_error_fails_first_criterion(self):
        test_case = ga.TcRun001ContinuousRbdIo()
        handle = FakeHandle(stdout_chunks=["fio: Input/output error on file"], done=False)
        _new_state, result = fw.poll_test_case(test_case, _ctx(), {"handle": handle, "error_seen": False})
        assert result.status == fw.TestStatus.FAIL
        assert result.criteria[0].passed is False

    def test_manual_review_criteria_are_never_auto_passed(self):
        test_case = ga.TcRun001ContinuousRbdIo()
        handle = FakeHandle(stdout_chunks=[""], done=False)
        _new_state, result = fw.poll_test_case(test_case, _ctx(), {"handle": handle, "error_seen": False})
        assert result.criteria[1].passed is None
        assert result.criteria[2].passed is None


# ---------------------------------------------------------------------------
# TC-RUN-004
# ---------------------------------------------------------------------------


class TestTcRun004:
    def test_healthy_cluster_passes(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph health detail": "HEALTH_OK"})
        result = fw.run_test_case(ga.TcRun004PgStatus(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_inactive_pg_fails(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch, {"ceph health detail": "PG_AVAILABILITY: 3 pgs inactive, 1 pg down"}
        )
        result = fw.run_test_case(ga.TcRun004PgStatus(), _ctx())
        assert result.status == fw.TestStatus.FAIL

    def test_degraded_alone_does_not_fail(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph health detail": "PG_DEGRADED: 2 pgs degraded"})
        result = fw.run_test_case(ga.TcRun004PgStatus(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_missing_mon_host_is_error(self):
        result = fw.run_test_case(ga.TcRun004PgStatus(), _ctx(mon_host=None))
        assert result.status == fw.TestStatus.ERROR


# ---------------------------------------------------------------------------
# TC-RUN-005
# ---------------------------------------------------------------------------


class TestTcRun005:
    def test_full_quorum_passes(self, monkeypatch):
        payload = json.dumps({"quorum": [0, 1, 2]})
        _fake_run_ceph_command(monkeypatch, {"ceph quorum_status": payload})
        result = fw.run_test_case(ga.TcRun005MonQuorum(), _ctx())
        assert result.criteria[0].passed is True
        assert result.status == fw.TestStatus.RUNNING  # 2nd criterion always manual

    def test_quorum_below_two_fails(self, monkeypatch):
        payload = json.dumps({"quorum": [0]})
        _fake_run_ceph_command(monkeypatch, {"ceph quorum_status": payload})
        result = fw.run_test_case(ga.TcRun005MonQuorum(), _ctx())
        assert result.status == fw.TestStatus.FAIL

    def test_invalid_json_is_error(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph quorum_status": "not json"})
        result = fw.run_test_case(ga.TcRun005MonQuorum(), _ctx())
        assert result.status == fw.TestStatus.ERROR


# ---------------------------------------------------------------------------
# TC-RUN-006
# ---------------------------------------------------------------------------


class TestTcRun006:
    def test_no_slow_ops(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph health detail": "HEALTH_OK"})
        result = fw.run_test_case(ga.TcRun006SlowOps(), _ctx())
        assert result.criteria[0].passed is True

    def test_slow_ops_present(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch, {"ceph health detail": "REQUEST_SLOW: 12 slow ops, oldest one blocked"}
        )
        result = fw.run_test_case(ga.TcRun006SlowOps(), _ctx())
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-RUN-007
# ---------------------------------------------------------------------------


class TestTcRun007:
    def _responses(self, num_osds, num_up, num_mons, quorum_size, mgr_available):
        return {
            "ceph osd stat": json.dumps({"num_osds": num_osds, "num_up_osds": num_up}),
            "ceph quorum_status": json.dumps({"monmap": {"mons": [{}] * num_mons}, "quorum": list(range(quorum_size))}),
            "ceph -s": json.dumps({"mgrmap": {"available": mgr_available}}),
        }

    def test_start_requires_mon_host(self):
        with pytest.raises(fw.TestCaseError):
            ga.TcRun007DaemonDowntime().start(_ctx(mon_host=None))

    def test_all_healthy_leaves_criteria_open(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, self._responses(3, 3, 3, 3, True))
        test_case = ga.TcRun007DaemonDowntime()
        state = test_case.start(_ctx())
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert all(c.passed is None for c in result.criteria)
        assert result.status == fw.TestStatus.RUNNING

    def test_osd_down_then_up_records_downtime_within_threshold(self, monkeypatch):
        test_case = ga.TcRun007DaemonDowntime()
        state = test_case.start(_ctx())

        _fake_run_ceph_command(monkeypatch, self._responses(3, 2, 3, 3, True))
        state, result_down = fw.poll_test_case(test_case, _ctx(), state)
        assert state["down_since"]["osd"] is not None
        osd_criterion = next(c for c in result_down.criteria if "OSD" in c.description)
        assert osd_criterion.passed is None

        _fake_run_ceph_command(monkeypatch, self._responses(3, 3, 3, 3, True))
        state, result_up = fw.poll_test_case(test_case, _ctx(), state)
        assert state["down_since"]["osd"] is None
        assert len(state["downtimes"]["osd"]) == 1
        osd_criterion = next(c for c in result_up.criteria if "OSD" in c.description)
        assert osd_criterion.passed is True

    def test_mon_lost_quorum_fails_when_never_recovers_but_stays_open(self, monkeypatch):
        test_case = ga.TcRun007DaemonDowntime()
        state = test_case.start(_ctx())
        _fake_run_ceph_command(monkeypatch, self._responses(3, 3, 3, 1, True))
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        mon_criterion = next(c for c in result.criteria if "MON" in c.description)
        assert mon_criterion.passed is None  # still down, not yet resolved either way


# ---------------------------------------------------------------------------
# TC-RUN-008
# ---------------------------------------------------------------------------


class TestTcRun008:
    def test_start_requires_at_least_one_node(self):
        with pytest.raises(fw.TestCaseError):
            ga.TcRun008SystemErrorLogs().start(_ctx(mon_host=None, osd_hosts=[], rgw_hosts=[]))

    def test_no_critical_lines_passes(self, monkeypatch):
        test_case = ga.TcRun008SystemErrorLogs()
        state = test_case.start(_ctx())
        _fake_run_ceph_command(monkeypatch, {"journalctl": "ceph-osd started ok\n"})
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert result.criteria[0].passed is True

    def test_assert_failure_fails(self, monkeypatch):
        test_case = ga.TcRun008SystemErrorLogs()
        state = test_case.start(_ctx())
        _fake_run_ceph_command(monkeypatch, {"journalctl": "FAILED ceph_assert(foo)\n"})
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_second_criterion_always_manual(self, monkeypatch):
        test_case = ga.TcRun008SystemErrorLogs()
        state = test_case.start(_ctx())
        _fake_run_ceph_command(monkeypatch, {"journalctl": ""})
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert result.criteria[1].passed is None


# ---------------------------------------------------------------------------
# TC-RUN-009
# ---------------------------------------------------------------------------


class TestTcRun009:
    def test_no_new_crash_passes(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph crash ls-new": ""})
        result = fw.run_test_case(ga.TcRun009CrashModule(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_new_crash_fails(self, monkeypatch):
        crash_output = "ID                                                        ENTITY  NEW\n2026-08-04_10:00:00.000000Z_abcd1234  osd.3   *\n"
        _fake_run_ceph_command(monkeypatch, {"ceph crash ls-new": crash_output})
        result = fw.run_test_case(ga.TcRun009CrashModule(), _ctx())
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-RUN-010
# ---------------------------------------------------------------------------


class TestTcRun010:
    def test_start_requires_client_host_and_rgw_vip(self):
        test_case = ga.TcRun010OldClientDuringUpgrade()
        with pytest.raises(fw.TestCaseError):
            test_case.start(_ctx(client_host=None, rgw_endpoint_vip="http://vip:8080"))
        with pytest.raises(fw.TestCaseError):
            test_case.start(_ctx(client_host="client1", rgw_endpoint_vip=None))

    def test_start_launches_combined_script(self, monkeypatch):
        captured = {}

        def fake_execute_background(host, command):
            captured["host"] = host
            captured["command"] = command
            return FakeHandle()

        monkeypatch.setattr(ga, "execute_background", fake_execute_background)
        test_case = ga.TcRun010OldClientDuringUpgrade()
        test_case.start(_ctx(client_host="client1", rgw_endpoint_vip="http://vip:8080"))
        assert captured["host"] == "client1"
        assert "fio" in captured["command"]
        assert "aws --endpoint-url http://vip:8080" in captured["command"]
        assert ga.TcRun010OldClientDuringUpgrade.CEPHFS_MOUNT in captured["command"]

    def test_poll_error_keyword_fails(self):
        test_case = ga.TcRun010OldClientDuringUpgrade()
        handle = FakeHandle(stdout_chunks=["NoSuchBucket error from s3"], done=False)
        _new_state, result = fw.poll_test_case(test_case, _ctx(), {"handle": handle, "error_seen": False})
        assert result.status == fw.TestStatus.FAIL

    def test_poll_clean_stays_open(self):
        test_case = ga.TcRun010OldClientDuringUpgrade()
        handle = FakeHandle(stdout_chunks=["ok"], done=False)
        _new_state, result = fw.poll_test_case(test_case, _ctx(), {"handle": handle, "error_seen": False})
        assert result.status == fw.TestStatus.RUNNING
        assert result.criteria[0].passed is None


# ---------------------------------------------------------------------------
# TC-RUN-011
# ---------------------------------------------------------------------------


class TestTcRun011:
    def test_zero_degraded_passes_snapshot_criterion(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph -s": json.dumps({"pgmap": {"degraded_ratio": 0}})})
        result = fw.run_test_case(ga.TcRun011PgDegraded(), _ctx())
        assert result.criteria[0].passed is True
        assert result.status == fw.TestStatus.RUNNING  # 2nd criterion always manual

    def test_nonzero_degraded_fails_snapshot_criterion(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph -s": json.dumps({"pgmap": {"degraded_ratio": 0.02}})})
        result = fw.run_test_case(ga.TcRun011PgDegraded(), _ctx())
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-RUN-012
# ---------------------------------------------------------------------------


class TestTcRun012:
    def test_noout_not_set_is_not_applicable(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph -s": json.dumps({"pgmap": {"misplaced_ratio": 0.5}}),
                "ceph osd dump": "flags nearfull",
            },
        )
        result = fw.run_test_case(ga.TcRun012NoUnplannedRebalance(), _ctx())
        assert result.criteria[0].passed is None
        assert result.status == fw.TestStatus.RUNNING

    def test_noout_set_within_threshold_passes(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph -s": json.dumps({"pgmap": {"misplaced_ratio": 0.01}}),
                "ceph osd dump": "flags noout,norebalance",
            },
        )
        result = fw.run_test_case(ga.TcRun012NoUnplannedRebalance(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_noout_set_over_threshold_fails(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph -s": json.dumps({"pgmap": {"misplaced_ratio": 0.2}}),
                "ceph osd dump": "flags noout",
            },
        )
        result = fw.run_test_case(ga.TcRun012NoUnplannedRebalance(), _ctx())
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-RUN-013
# ---------------------------------------------------------------------------


class TestTcRun013:
    def test_start_requires_targets(self):
        with pytest.raises(fw.TestCaseError):
            ga.TcRun013OmapConvert().start(_ctx(), targets=[])

    def test_start_launches_first_target(self, monkeypatch):
        captured = {}

        def fake_execute_background(host, command):
            captured["host"] = host
            captured["command"] = command
            return FakeHandle()

        monkeypatch.setattr(ga, "execute_background", fake_execute_background)
        targets = [ga.OsdConvertTarget(osd_id=3, host="osd1", estimated_seconds=60)]
        test_case = ga.TcRun013OmapConvert()
        state = test_case.start(_ctx(), targets=targets)
        _new_state, _result = fw.poll_test_case(test_case, _ctx(), state)
        assert captured["host"] == "osd1"
        assert "ceph-bluestore-tool repair" in captured["command"]
        assert "ceph-3" in captured["command"]

    def test_successful_single_osd_completes_and_passes(self, monkeypatch):
        monkeypatch.setattr(ga, "execute_background", lambda host, command: FakeHandle(done=False))
        targets = [ga.OsdConvertTarget(osd_id=3, host="osd1", estimated_seconds=1000)]
        test_case = ga.TcRun013OmapConvert()
        state = test_case.start(_ctx(), targets=targets)
        state, _result = fw.poll_test_case(test_case, _ctx(), state)  # launches, still running

        state["handle"] = FakeHandle(stdout_chunks=["repair success"], exit_code=0, done=True)
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert result.status == fw.TestStatus.PASS
        assert _new_state["index"] == 1
        assert _new_state["paused"] is False

    def test_over_estimate_pauses_sequence(self, monkeypatch):
        monkeypatch.setattr(ga, "execute_background", lambda host, command: FakeHandle(done=False))
        targets = [
            ga.OsdConvertTarget(osd_id=3, host="osd1", estimated_seconds=1),
            ga.OsdConvertTarget(osd_id=4, host="osd1", estimated_seconds=1000),
        ]
        test_case = ga.TcRun013OmapConvert()
        state = test_case.start(_ctx(), targets=targets)
        state, _result = fw.poll_test_case(test_case, _ctx(), state)
        # Force elapsed duration past the 2x threshold deterministically
        # instead of relying on real wall-clock time between two calls.
        state["current_started_at"] = datetime.utcnow() - timedelta(seconds=10)

        state["handle"] = FakeHandle(stdout_chunks=["repair success"], exit_code=0, done=True)
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert _new_state["paused"] is True
        assert _new_state["index"] == 0  # did not advance to osd.4
        over_estimate_criterion = result.criteria[0]
        assert over_estimate_criterion.passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_failed_exit_pauses_sequence(self, monkeypatch):
        monkeypatch.setattr(ga, "execute_background", lambda host, command: FakeHandle(done=False))
        targets = [ga.OsdConvertTarget(osd_id=3, host="osd1", estimated_seconds=1000)]
        test_case = ga.TcRun013OmapConvert()
        state = test_case.start(_ctx(), targets=targets)
        state, _result = fw.poll_test_case(test_case, _ctx(), state)

        state["handle"] = FakeHandle(stdout_chunks=["repair failed"], exit_code=1, done=True)
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert _new_state["paused"] is True
        failed_criterion = result.criteria[2]
        assert failed_criterion.passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_manual_review_criterion_always_none(self, monkeypatch):
        monkeypatch.setattr(ga, "execute_background", lambda host, command: FakeHandle(done=False))
        targets = [ga.OsdConvertTarget(osd_id=3, host="osd1")]
        test_case = ga.TcRun013OmapConvert()
        state = test_case.start(_ctx(), targets=targets)
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert result.criteria[1].passed is None
