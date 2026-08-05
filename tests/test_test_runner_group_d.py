"""Epic 10 Story 10.5: tests for the 7 Group D (performance) test cases.
Fakes worker.executor.test_runner.group_d's own module-level
`run_ceph_command`/`run_script`/`execute_background` names, same convention
as tests/test_test_runner_group_c.py.
"""

import json
from datetime import datetime, timedelta

import pytest

from worker.executor.test_runner import framework as fw
from worker.executor.test_runner import group_d as gd


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
    def fake(host, command):
        for key, value in responses.items():
            if key == command or key in command:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"no fake response configured for command: {command!r}")

    monkeypatch.setattr(gd, "run_ceph_command", fake)


def _fake_run_script(monkeypatch, canned_output):
    monkeypatch.setattr(gd, "run_script", lambda host, script: canned_output)


class FakeHandle:
    def __init__(self, exit_code=None, done=False):
        self._exit_code = exit_code
        self._done = done

    def is_done(self):
        return self._done

    def read_new_output(self):
        return "", ""

    def exit_code(self):
        return self._exit_code if self._done else None


def test_group_d_tests_registry_has_7_default_constructible_classes():
    assert len(gd.GROUP_D_TESTS) == 7
    ids = [cls().id for cls in gd.GROUP_D_TESTS]
    assert len(ids) == len(set(ids))
    assert ids[0] == "TC-PERF-001-003"
    assert ids[1:] == [f"TC-PERF-{i:03d}" for i in range(4, 10)]


class TestExtractFioSummary:
    def test_extracts_metrics_ignoring_trailing_exit_marker(self):
        payload = {
            "jobs": [
                {
                    "read": {"iops": 5000, "bw": 20000, "clat_ns": {"percentile": {"99.000000": 1234567}}},
                    "write": {},
                }
            ]
        }
        body = json.dumps(payload) + "\nEXIT:0\n"
        summary = gd._extract_fio_summary(body)
        assert summary["read_iops"] == 5000
        assert summary["read_p99_latency_ns"] == 1234567

    def test_unparseable_returns_empty_dict(self):
        assert gd._extract_fio_summary("not json at all\nEXIT:1\n") == {}

    def test_zero_iops_is_not_dropped(self):
        """Regression test: a truthy check (`if side.get("iops")`) used to
        silently omit a genuine 0 -- exactly the worst-case catastrophic
        result (a completely stalled I/O path) this measurement exists to
        surface."""
        payload = {"jobs": [{"read": {"iops": 0, "bw": 0}, "write": {}}]}
        body = json.dumps(payload) + "\nEXIT:0\n"
        summary = gd._extract_fio_summary(body)
        assert summary["read_iops"] == 0
        assert summary["read_bw_kbps"] == 0


class TestTcPerf001To003:
    def test_requires_client_host(self):
        with pytest.raises(fw.TestCaseError):
            gd.TcPerf001To003RbdPerformance().run(_ctx(client_host=None))

    def test_always_manual_review_with_measured_numbers_in_detail(self, monkeypatch):
        iops_json = json.dumps({"jobs": [{"read": {"iops": 5000}, "write": {}}]})
        bw_json = json.dumps({"jobs": [{"read": {}, "write": {"bw": 40000}}]})
        canned = f'===STEP:iops===\n{iops_json}\nEXIT:0\n===STEP:bw===\n{bw_json}\nEXIT:0\n'
        _fake_run_script(monkeypatch, canned)
        result = fw.run_test_case(gd.TcPerf001To003RbdPerformance(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is None
        assert result.criteria[1].passed is None
        assert "5000" in result.criteria[0].detail
        assert result.status == fw.TestStatus.RUNNING

    def test_fio_command_failure_fails_instead_of_silently_reporting_no_data(self, monkeypatch):
        """Regression test: previously neither fio step's exit code was
        checked at all, so a failed fio invocation looked identical to
        "ran fine, just no baseline configured yet"."""
        canned = '===STEP:iops===\nfio: failed to open device\nEXIT:1\n===STEP:bw===\n{}\nEXIT:0\n'
        _fake_run_script(monkeypatch, canned)
        result = fw.run_test_case(gd.TcPerf001To003RbdPerformance(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is False
        assert result.criteria[1].passed is False
        assert result.status == fw.TestStatus.FAIL


class TestTcPerf004:
    def test_always_manual_review_with_bandwidths_in_detail(self, monkeypatch):
        body = (
            "===STEP:bench===\n"
            "Bandwidth (MB/sec): 123.45\nBENCHRC:0\n"
            "Bandwidth (MB/sec): 100.00\nBENCHRC:0\n"
            "Bandwidth (MB/sec): 90.00\nBENCHRC:0\n"
            "EXIT:0\n"
        )
        _fake_run_script(monkeypatch, body)
        result = fw.run_test_case(gd.TcPerf004ObjectThroughput(), _ctx())
        assert result.criteria[0].passed is None
        assert "123.45" in result.criteria[0].detail

    def test_bench_failure_fails_even_if_cleanup_succeeds(self, monkeypatch):
        """Regression test: the script's final EXIT:$? only reflects `rados
        cleanup`, which can succeed even if an earlier bench phase failed --
        each bench command's own BENCHRC:$? is now checked instead."""
        body = "===STEP:bench===\nerror: pool not found\nBENCHRC:2\nBENCHRC:0\nBENCHRC:0\nEXIT:0\n"
        _fake_run_script(monkeypatch, body)
        result = fw.run_test_case(gd.TcPerf004ObjectThroughput(), _ctx())
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_missing_benchrc_markers_fails(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:bench===\nsome unexpected output\nEXIT:0\n")
        result = fw.run_test_case(gd.TcPerf004ObjectThroughput(), _ctx())
        assert result.criteria[0].passed is False


class TestTcPerf005:
    def test_start_requires_client_host(self):
        with pytest.raises(fw.TestCaseError):
            gd.TcPerf005RgwPerformance().start(_ctx(client_host=None, rgw_endpoint_vip="http://vip:8080"))

    def test_start_requires_rgw_endpoint(self):
        with pytest.raises(fw.TestCaseError):
            gd.TcPerf005RgwPerformance().start(_ctx(client_host="client1", rgw_endpoint_vip=None))

    def test_start_launches_warp_background(self, monkeypatch):
        captured = {}

        def fake_eb(host, command):
            captured["host"] = host
            captured["command"] = command
            return FakeHandle()

        monkeypatch.setattr(gd, "execute_background", fake_eb)
        state = gd.TcPerf005RgwPerformance().start(_ctx(client_host="client1", rgw_endpoint_vip="http://vip:8080"))
        assert captured["host"] == "client1"
        assert "warp" in captured["command"]
        assert "vip:8080" in captured["command"]
        assert state["client_host"] == "client1"

    def test_poll_still_running_is_manual_review(self):
        state = {"handle": FakeHandle(done=False), "client_host": "client1"}
        _new_state, result = fw.poll_test_case(gd.TcPerf005RgwPerformance(), _ctx(), state)
        assert result.criteria[0].passed is None
        assert result.status == fw.TestStatus.RUNNING

    def test_poll_failed_exit_fails(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"tail -c": "some log"})
        state = {"handle": FakeHandle(exit_code=1, done=True), "client_host": "client1"}
        _new_state, result = fw.poll_test_case(gd.TcPerf005RgwPerformance(), _ctx(), state)
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_poll_clean_exit_is_manual_review(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"tail -c": "throughput: 500 MB/s"})
        state = {"handle": FakeHandle(exit_code=0, done=True), "client_host": "client1"}
        _new_state, result = fw.poll_test_case(gd.TcPerf005RgwPerformance(), _ctx(), state)
        assert result.criteria[0].passed is None
        assert "500 MB/s" in result.criteria[0].detail


class TestTcPerf006:
    def test_success_is_manual_review(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:mdtest===\nSUMMARY: ...\nEXIT:0\n")
        result = fw.run_test_case(gd.TcPerf006CephfsMetadata(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is None

    def test_command_failure_fails(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:mdtest===\nmdtest: command not found\nEXIT:127\n")
        result = fw.run_test_case(gd.TcPerf006CephfsMetadata(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL


class TestTcPerf007:
    def test_start_requires_osd_hosts(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"systemctl stop": ""})
        with pytest.raises(fw.TestCaseError):
            gd.TcPerf007RecoveryTime().start(_ctx(osd_hosts=[]))

    def test_start_stops_osd_target_on_first_osd_host(self, monkeypatch):
        calls = []

        def fake(host, command):
            calls.append((host, command))
            return ""

        monkeypatch.setattr(gd, "run_ceph_command", fake)
        state = gd.TcPerf007RecoveryTime().start(_ctx(osd_hosts=["osd1", "osd2"]))
        assert calls == [("osd1", "systemctl stop ceph-osd.target")]
        assert state["target_host"] == "osd1"
        assert state["restarted"] is False

    def test_poll_healthy_restarts_osd_and_reports_recovery_seconds(self, monkeypatch):
        status = {"health": {"status": "HEALTH_OK"}}
        calls = []

        def fake(host, command):
            calls.append((host, command))
            return json.dumps(status)

        monkeypatch.setattr(gd, "run_ceph_command", fake)
        state = {
            "start_time": datetime.utcnow() - timedelta(seconds=42),
            "target_host": "osd1",
            "restarted": False,
            "mon": "mon1",
        }
        new_state, result = fw.poll_test_case(gd.TcPerf007RecoveryTime(), _ctx(), state)
        assert new_state["restarted"] is True
        assert ("osd1", "systemctl start ceph-osd.target") in calls
        assert "recovery_seconds" in result.criteria[0].detail
        assert result.criteria[0].passed is None

    def test_poll_still_unhealthy_stays_open_and_does_not_restart_yet(self, monkeypatch):
        status = {"health": {"status": "HEALTH_WARN"}}
        monkeypatch.setattr(gd, "run_ceph_command", lambda host, command: json.dumps(status))
        state = {
            "start_time": datetime.utcnow() - timedelta(seconds=5),
            "target_host": "osd1",
            "restarted": False,
            "mon": "mon1",
        }
        new_state, result = fw.poll_test_case(gd.TcPerf007RecoveryTime(), _ctx(), state)
        assert new_state["restarted"] is False
        assert result.criteria[0].passed is None

    def test_poll_timeout_restarts_osd_and_fails(self, monkeypatch):
        status = {"health": {"status": "HEALTH_WARN"}}
        monkeypatch.setattr(gd, "run_ceph_command", lambda host, command: json.dumps(status))
        state = {
            "start_time": datetime.utcnow() - timedelta(seconds=gd.TcPerf007RecoveryTime.MAX_WAIT_SECONDS + 10),
            "target_host": "osd1",
            "restarted": False,
            "mon": "mon1",
        }
        new_state, result = fw.poll_test_case(gd.TcPerf007RecoveryTime(), _ctx(), state)
        assert new_state["restarted"] is True
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL


class TestTcPerf008:
    def test_within_target_passes(self, monkeypatch):
        mempool = {"mempool": {"total_bytes": 1000}}
        _fake_run_ceph_command(
            monkeypatch,
            {"dump_mempools": json.dumps(mempool), "osd_memory_target": "1000\n"},
        )
        result = fw.run_test_case(gd.TcPerf008OsdMemory(), _ctx())
        assert result.criteria[0].passed is True
        assert result.status == fw.TestStatus.PASS

    def test_over_20_percent_fails(self, monkeypatch):
        mempool = {"mempool": {"total_bytes": 1300}}
        _fake_run_ceph_command(
            monkeypatch,
            {"dump_mempools": json.dumps(mempool), "osd_memory_target": "1000\n"},
        )
        result = fw.run_test_case(gd.TcPerf008OsdMemory(), _ctx())
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_unparseable_target_is_manual_review(self, monkeypatch):
        mempool = {"mempool": {"total_bytes": 1000}}
        _fake_run_ceph_command(
            monkeypatch,
            {"dump_mempools": json.dumps(mempool), "osd_memory_target": "not-a-number\n"},
        )
        result = fw.run_test_case(gd.TcPerf008OsdMemory(), _ctx())
        assert result.criteria[0].passed is None


class TestTcPerf009:
    def test_start_requires_client_host(self):
        with pytest.raises(fw.TestCaseError):
            gd.TcPerf009SoakTest().start(_ctx(client_host=None, rgw_endpoint_vip="http://vip:8080"))

    def test_start_requires_rgw_endpoint(self):
        with pytest.raises(fw.TestCaseError):
            gd.TcPerf009SoakTest().start(_ctx(client_host="client1", rgw_endpoint_vip=None))

    def test_start_snapshots_crash_baseline(self, monkeypatch):
        monkeypatch.setattr(gd, "execute_background", lambda host, command: FakeHandle())
        _fake_run_ceph_command(monkeypatch, {"ceph crash ls-new": "existing-crash-1\n"})
        state = gd.TcPerf009SoakTest().start(_ctx(client_host="client1", rgw_endpoint_vip="http://vip:8080"))
        assert state["crash_baseline_lines"] == 1
        assert state["crash_seen"] is False
        assert state["ram_samples"] == []
        assert state["handle"] is not None
        assert state["load_dead_seen"] is False

    def _base_state(self, **overrides):
        state = dict(
            start_time=datetime.utcnow() - timedelta(hours=1),
            mon="mon1",
            handle=FakeHandle(done=False),
            load_dead_seen=False,
            crash_baseline_lines=0,
            crash_seen=False,
            pg_issue_seen=False,
            ram_issue_seen=False,
            ram_samples=[],
        )
        state.update(overrides)
        return state

    def test_poll_load_died_fails_sticky(self, monkeypatch):
        """Regression test: start() used to discard the execute_background()
        handle entirely, so poll() had no way to detect the load dying."""
        status = {"pgmap": {"num_pgs": 10, "pgs_by_state": [{"state_name": "active+clean", "count": 10}]}}
        mempool = {"mempool": {"total_bytes": 100}}

        def fake(host, command):
            if "crash ls-new" in command:
                return ""
            if "dump_mempools" in command:
                return json.dumps(mempool)
            return json.dumps(status)

        monkeypatch.setattr(gd, "run_ceph_command", fake)
        state = self._base_state(handle=FakeHandle(done=True, exit_code=0))
        new_state, result = fw.poll_test_case(gd.TcPerf009SoakTest(), _ctx(), state)
        assert new_state["load_dead_seen"] is True
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL

        # Sticky: even if a later poll somehow observes a fresh (not-done)
        # handle, the earlier death must still be remembered.
        state2 = dict(new_state, handle=FakeHandle(done=False))
        new_state2, result2 = fw.poll_test_case(gd.TcPerf009SoakTest(), _ctx(), state2)
        assert new_state2["load_dead_seen"] is True
        assert result2.criteria[0].passed is False

    def test_poll_new_crash_fails_sticky(self, monkeypatch):
        status = {"pgmap": {"num_pgs": 10, "pgs_by_state": [{"state_name": "active+clean", "count": 10}]}}
        mempool = {"mempool": {"total_bytes": 100}}

        def fake(host, command):
            if "crash ls-new" in command:
                return "new-crash-1\n"
            if "dump_mempools" in command:
                return json.dumps(mempool)
            return json.dumps(status)

        monkeypatch.setattr(gd, "run_ceph_command", fake)
        new_state, result = fw.poll_test_case(gd.TcPerf009SoakTest(), _ctx(), self._base_state())
        assert new_state["crash_seen"] is True
        assert result.criteria[1].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_poll_pg_issue_fails_sticky(self, monkeypatch):
        status = {
            "pgmap": {
                "num_pgs": 10,
                "pgs_by_state": [{"state_name": "active+clean", "count": 8}, {"state_name": "down", "count": 2}],
            }
        }
        mempool = {"mempool": {"total_bytes": 100}}

        def fake(host, command):
            if "crash ls-new" in command:
                return ""
            if "dump_mempools" in command:
                return json.dumps(mempool)
            return json.dumps(status)

        monkeypatch.setattr(gd, "run_ceph_command", fake)
        new_state, result = fw.poll_test_case(gd.TcPerf009SoakTest(), _ctx(), self._base_state())
        assert new_state["pg_issue_seen"] is True
        assert result.criteria[2].passed is False

    def test_poll_monotonic_ram_increase_fails(self, monkeypatch):
        status = {"pgmap": {"num_pgs": 10, "pgs_by_state": [{"state_name": "active+clean", "count": 10}]}}
        readings = iter([100, 200, 300, 400, 500])

        def fake(host, command):
            if "crash ls-new" in command:
                return ""
            if "dump_mempools" in command:
                return json.dumps({"mempool": {"total_bytes": next(readings)}})
            return json.dumps(status)

        monkeypatch.setattr(gd, "run_ceph_command", fake)
        test_case = gd.TcPerf009SoakTest()
        state = self._base_state()
        for _ in range(5):
            state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert state["ram_issue_seen"] is True
        assert result.criteria[3].passed is False

    def test_ram_increase_signal_stays_sticky_after_a_later_dip(self, monkeypatch):
        """Regression test: monotonic_increase used to be recomputed fresh
        from only the last MAX_RAM_SAMPLES every poll with no sticky
        accumulation -- a leak signature observed on one poll could
        un-trip on a later poll where RAM happened to dip."""
        status = {"pgmap": {"num_pgs": 10, "pgs_by_state": [{"state_name": "active+clean", "count": 10}]}}
        # 5 increasing readings trip the leak signal, then a 6th reading
        # that drops back down -- without stickiness this would clear it.
        readings = iter([100, 200, 300, 400, 500, 50])

        def fake(host, command):
            if "crash ls-new" in command:
                return ""
            if "dump_mempools" in command:
                return json.dumps({"mempool": {"total_bytes": next(readings)}})
            return json.dumps(status)

        monkeypatch.setattr(gd, "run_ceph_command", fake)
        test_case = gd.TcPerf009SoakTest()
        state = self._base_state()
        result = None
        for _ in range(6):
            state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert state["ram_issue_seen"] is True
        assert result.criteria[3].passed is False

    def test_poll_before_72h_stays_open_even_when_healthy(self, monkeypatch):
        status = {"pgmap": {"num_pgs": 10, "pgs_by_state": [{"state_name": "active+clean", "count": 10}]}}

        def fake(host, command):
            if "crash ls-new" in command:
                return ""
            if "dump_mempools" in command:
                return json.dumps({"mempool": {"total_bytes": 100}})
            return json.dumps(status)

        monkeypatch.setattr(gd, "run_ceph_command", fake)
        _new_state, result = fw.poll_test_case(gd.TcPerf009SoakTest(), _ctx(), self._base_state())
        assert result.status == fw.TestStatus.RUNNING
        assert result.criteria[0].passed is None

    def test_poll_after_72h_healthy_passes(self, monkeypatch):
        status = {"pgmap": {"num_pgs": 10, "pgs_by_state": [{"state_name": "active+clean", "count": 10}]}}

        def fake(host, command):
            if "crash ls-new" in command:
                return ""
            if "dump_mempools" in command:
                return json.dumps({"mempool": {"total_bytes": 100}})
            return json.dumps(status)

        monkeypatch.setattr(gd, "run_ceph_command", fake)
        state = self._base_state(start_time=datetime.utcnow() - timedelta(hours=73))
        _new_state, result = fw.poll_test_case(gd.TcPerf009SoakTest(), _ctx(), state)
        assert result.status == fw.TestStatus.PASS
