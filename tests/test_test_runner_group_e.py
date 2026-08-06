"""Tests for worker/executor/test_runner/group_e.py (Group E, S3/RGW
upgrade regression, docs/s3-upgrade-test-cases.md). Same "fake at the
module boundary" convention tests/test_test_runner_group_c.py already
established -- monkeypatches group_e's own module-level
run_ceph_command/run_script/execute_background names.
"""

import pytest

from worker.executor.test_runner import framework as fw
from worker.executor.test_runner import group_e as ge


def _ctx(**overrides):
    defaults = dict(
        mon_host="mon1",
        osd_hosts=["osd1", "osd2"],
        rgw_hosts=["rgw1"],
        client_host="client1",
        rgw_endpoint_zone_a=None,
        rgw_endpoint_zone_b=None,
        rgw_endpoint_vip="http://vip:8080",
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

    monkeypatch.setattr(ge, "run_ceph_command", fake)


def _fake_run_script(monkeypatch, canned_output):
    monkeypatch.setattr(ge, "run_script", lambda host, script: canned_output)


class FakeHandle:
    def __init__(self, stdout_chunks=None, stderr_chunks=None, exit_code=None, done=False):
        self._stdout_chunks = list(stdout_chunks or [])
        self._stderr_chunks = list(stderr_chunks or [])
        self._exit_code = exit_code
        self._done = done

    def is_done(self):
        return self._done

    def read_new_output(self):
        out = self._stdout_chunks.pop(0) if self._stdout_chunks else ""
        err = self._stderr_chunks.pop(0) if self._stderr_chunks else ""
        return out, err

    def exit_code(self):
        return self._exit_code if self._done else None


# --- Registry-level invariants ----------------------------------------------


def test_group_e_tests_registry_has_52_default_constructible_classes():
    assert len(ge.GROUP_E_TESTS) == 52
    instances = [cls() for cls in ge.GROUP_E_TESTS]
    ids = [tc.id for tc in instances]
    assert len(ids) == len(set(ids)), "duplicate ids within Group E"
    for tc in instances:
        assert tc.id.startswith("TC-S3-"), tc.id
        assert tc.group == fw.TestGroup.E
        assert tc.priority in (fw.TestPriority.P1, fw.TestPriority.P2, fw.TestPriority.P3)


def test_group_e_registered_in_the_combined_registry():
    from worker.executor.test_runner.registry import ALL_TEST_CASES, TEST_CASES_BY_ID

    e_ids = {tc.id for tc in ge.GROUP_E_TESTS}
    all_ids = {tc.id for tc in ALL_TEST_CASES}
    assert e_ids <= all_ids
    for tc_id in e_ids:
        assert TEST_CASES_BY_ID[tc_id].__class__.__name__ in {cls.__name__ for cls in ge.GROUP_E_TESTS}


# --- Declined test cases (Vault/multisite scope cuts) -----------------------

_VAULT_DECLINED_CLASSES = [
    ge.TcS3Prep07VaultBeforeStart,
    ge.TcS3Data04DefaultEncryption,
    ge.TcS3Post40VaultHealth,
    ge.TcS3Post41VaultTransitKeys,
    ge.TcS3Post42PutWithSse,
    ge.TcS3Post43GetSseObject,
    ge.TcS3Post44VaultAuditLog,
]

_MULTISITE_DECLINED_CLASSES = [
    ge.TcS3Prep06MultisiteSyncBeforeStart,
    ge.TcS3Run003MultisiteSyncDuringUpgrade,
    ge.TcS3Post50CrossZoneReplication,
    ge.TcS3Post51BidirectionalSyncStatus,
    ge.TcS3Post52MetadataSync,
]


@pytest.mark.parametrize("cls", _VAULT_DECLINED_CLASSES)
def test_vault_scope_declined_test_cases(cls):
    with pytest.raises(fw.TestCaseDeclined, match="Vault"):
        cls().run(_ctx())
    result = fw.run_test_case(cls(), _ctx())
    assert result.status == fw.TestStatus.SKIP


@pytest.mark.parametrize("cls", _MULTISITE_DECLINED_CLASSES)
def test_multisite_scope_declined_test_cases(cls):
    with pytest.raises(fw.TestCaseDeclined, match="multisite"):
        cls().run(_ctx())
    result = fw.run_test_case(cls(), _ctx())
    assert result.status == fw.TestStatus.SKIP


def test_declined_classes_cover_every_class_that_raises_declined(monkeypatch):
    """Guards against a future edit silently un-declining (or adding a new
    silently-declined) class without updating the two lists above. Fakes
    the SSH boundary to raise immediately for every OTHER class -- this
    test only cares which classes raise TestCaseDeclined before ever
    reaching a real command, not what those other classes' real SSH
    results would be (that's covered by their own dedicated tests above).
    """
    from worker.executor.ssh_executor import ExecutorError

    def _boom(*args, **kwargs):
        raise ExecutorError("fake: no real SSH in this test")

    monkeypatch.setattr(ge, "run_ceph_command", _boom)
    monkeypatch.setattr(ge, "run_script", _boom)
    declined_ids = {cls().id for cls in _VAULT_DECLINED_CLASSES + _MULTISITE_DECLINED_CLASSES}
    actually_declined = set()
    for cls in ge.GROUP_E_TESTS:
        tc = cls()
        if not tc.background:
            try:
                tc.run(_ctx())
            except fw.TestCaseDeclined:
                actually_declined.add(tc.id)
            except (fw.TestCaseError, ExecutorError):
                pass
    assert actually_declined == declined_ids


# --- Precondition checks (a representative sample) --------------------------


def test_prep02_requires_client_host():
    with pytest.raises(fw.TestCaseError):
        ge.TcS3Prep02CreateTestBucket().run(_ctx(client_host=None))


def test_prep02_requires_rgw_endpoint():
    with pytest.raises(fw.TestCaseError):
        ge.TcS3Prep02CreateTestBucket().run(_ctx(rgw_endpoint_vip=None))


def test_prep01_requires_rgw_host():
    with pytest.raises(fw.TestCaseError):
        ge.TcS3Prep01CreateTestUser().run(_ctx(rgw_hosts=[]))


def test_post02_requires_rgw_hosts():
    with pytest.raises(fw.TestCaseError):
        ge.TcS3Post02InstancesActive().run(_ctx(rgw_hosts=[]))


# --- Exit-code-parsing correctness (highest-risk area given prior bugs) -----


class TestTcS3Prep01:
    def test_pass_when_user_created(self, monkeypatch):
        _fake_run_script(
            monkeypatch,
            '===STEP:full===\nuser created output\nEXIT:0\n',
        )
        result = fw.run_test_case(ge.TcS3Prep01CreateTestUser(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_pass_when_already_exists(self, monkeypatch):
        _fake_run_script(monkeypatch, '===STEP:full===\nALREADY_EXISTS\nEXIT:0\n')
        result = fw.run_test_case(ge.TcS3Prep01CreateTestUser(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_fail_when_create_fails(self, monkeypatch):
        _fake_run_script(monkeypatch, '===STEP:full===\nsome error\nEXIT:1\n')
        result = fw.run_test_case(ge.TcS3Prep01CreateTestUser(), _ctx())
        assert result.status == fw.TestStatus.FAIL


class TestTcS3Post10ManifestRoundTrip:
    def test_pass_when_no_mismatch(self, monkeypatch):
        _fake_run_script(monkeypatch, '===STEP:full===\nMISMATCH:0\nEXIT:0\n')
        result = fw.run_test_case(ge.TcS3Post10ManifestRoundTrip(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_fail_when_mismatch_nonzero(self, monkeypatch):
        _fake_run_script(monkeypatch, '===STEP:full===\nMISMATCH:3\nEXIT:0\n')
        result = fw.run_test_case(ge.TcS3Post10ManifestRoundTrip(), _ctx())
        assert result.status == fw.TestStatus.FAIL


class TestTcS3Post13MultipartRoundTrip:
    def test_pass_on_checksum_match(self, monkeypatch):
        _fake_run_script(monkeypatch, '===STEP:full===\nCHECKSUM_MATCH\nEXIT:0\n')
        result = fw.run_test_case(ge.TcS3Post13MultipartRoundTrip(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_fail_on_checksum_mismatch(self, monkeypatch):
        _fake_run_script(monkeypatch, '===STEP:full===\nCHECKSUM_MISMATCH up=a down=b\nEXIT:0\n')
        result = fw.run_test_case(ge.TcS3Post13MultipartRoundTrip(), _ctx())
        assert result.status == fw.TestStatus.FAIL


class TestTcS3Post24MultipartUpload:
    def test_fail_when_upload_itself_fails_even_if_trailing_exit_is_zero(self, monkeypatch):
        """Regression guard for the exit-code-masking bug class this
        project's code reviews already caught twice (Story 10.4/10.5): the
        trailing EXIT:$? only reflects the LAST command (the download +
        MATCH/NOMATCH check), so a failed upload (non-zero UPRC) must still
        fail this criterion even though the script's own trailing marker
        is 0.
        """
        _fake_run_script(
            monkeypatch,
            '===STEP:full===\nUPRC:1\nsome upload error\nMATCH\nEXIT:0\n',
        )
        result = fw.run_test_case(ge.TcS3Post24MultipartUpload(), _ctx())
        assert result.status == fw.TestStatus.FAIL

    def test_pass_when_upload_and_match(self, monkeypatch):
        _fake_run_script(monkeypatch, '===STEP:full===\nUPRC:0\nMATCH\nEXIT:0\n')
        result = fw.run_test_case(ge.TcS3Post24MultipartUpload(), _ctx())
        assert result.status == fw.TestStatus.PASS


class TestTcS3Post30ListBeforePut:
    def test_isolates_write_permission_failure_from_upgrade(self, monkeypatch):
        _fake_run_script(monkeypatch, '===STEP:full===\nLISTRC:0\nEXIT:1\n')
        result = fw.run_test_case(ge.TcS3Post30ListBeforePut(), _ctx())
        assert result.status == fw.TestStatus.FAIL
        assert "quyen ghi/quota" in result.criteria[0].detail

    def test_pass_when_both_succeed(self, monkeypatch):
        _fake_run_script(monkeypatch, '===STEP:full===\nLISTRC:0\nEXIT:0\n')
        result = fw.run_test_case(ge.TcS3Post30ListBeforePut(), _ctx())
        assert result.status == fw.TestStatus.PASS


class TestTcS3Post01VersionConsistent:
    def test_pass_when_single_rgw_version(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {"ceph versions": '{"rgw": {"ceph version 16.2.15 (abc) pacific (stable)": 3}}'},
        )
        result = fw.run_test_case(ge.TcS3Post01VersionConsistent(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_fail_when_mixed_rgw_versions(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph versions": (
                    '{"rgw": {"ceph version 14.2.22 (x) nautilus (stable)": 1, '
                    '"ceph version 16.2.15 (y) pacific (stable)": 2}}'
                )
            },
        )
        result = fw.run_test_case(ge.TcS3Post01VersionConsistent(), _ctx())
        assert result.status == fw.TestStatus.FAIL


class TestTcS3Post04NoNewCrash:
    def test_pass_when_no_crashes(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph crash ls-new": ""})
        result = fw.run_test_case(ge.TcS3Post04NoNewCrash(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_fail_when_crash_listed(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph crash ls-new": "2026-08-06_client.rgw.rgw1"})
        result = fw.run_test_case(ge.TcS3Post04NoNewCrash(), _ctx())
        assert result.status == fw.TestStatus.FAIL


# --- Background test cases ---------------------------------------------------


class TestTcS3Run001ContinuousLoadViaLb:
    def test_start_requires_client_host(self):
        with pytest.raises(fw.TestCaseError):
            ge.TcS3Run001ContinuousLoadViaLb().start(_ctx(client_host=None))

    def test_start_requires_rgw_endpoint(self):
        with pytest.raises(fw.TestCaseError):
            ge.TcS3Run001ContinuousLoadViaLb().start(_ctx(rgw_endpoint_vip=None))

    def test_start_launches_background_probe(self, monkeypatch):
        captured = {}

        def fake_execute_background(host, cmd):
            captured["host"] = host
            captured["cmd"] = cmd
            return FakeHandle()

        monkeypatch.setattr(ge, "execute_background", fake_execute_background)
        tc = ge.TcS3Run001ContinuousLoadViaLb()
        state = tc.start(_ctx())
        assert captured["host"] == "client1"
        assert "s3-upgrade-test-bucket" in captured["cmd"]
        assert isinstance(state["handle"], FakeHandle)

    def test_poll_detects_failure_streak(self, monkeypatch):
        handle = FakeHandle(done=False)
        probe_log = "\n".join(
            [f"2026-08-06T00:00:{i:02d} HTTP_FAIL code=503" for i in range(7)]
        )
        _fake_run_ceph_command(monkeypatch, {"cat /tmp/s3_run001_probe.log": probe_log})
        tc = ge.TcS3Run001ContinuousLoadViaLb()
        state = {"handle": handle, "client_host": "client1", "total_probes": 0, "fail_probes": 0}
        new_state, result = fw.poll_test_case(tc, _ctx(), state)
        streak_criterion = result.criteria[1]
        assert streak_criterion.passed is False

    def test_poll_no_failures_stays_open(self, monkeypatch):
        handle = FakeHandle(done=False)
        _fake_run_ceph_command(monkeypatch, {"cat /tmp/s3_run001_probe.log": ""})
        tc = ge.TcS3Run001ContinuousLoadViaLb()
        state = {"handle": handle, "client_host": "client1", "total_probes": 0, "fail_probes": 0}
        new_state, result = fw.poll_test_case(tc, _ctx(), state)
        assert result.status == fw.TestStatus.RUNNING
        assert all(c.passed is None for c in result.criteria)


class TestTcS3Run002RgwLogMonitor:
    def test_start_requires_rgw_host(self):
        with pytest.raises(fw.TestCaseError):
            ge.TcS3Run002RgwLogMonitor().start(_ctx(rgw_hosts=[]))

    def test_poll_flags_crash_keyword(self, monkeypatch):
        handle = FakeHandle(stdout_chunks=["some line\ncrash detected in thread\n"], done=False)
        tc = ge.TcS3Run002RgwLogMonitor()
        state = {"handle": handle, "error_seen": False, "error_lines": []}
        new_state, result = fw.poll_test_case(tc, _ctx(), state)
        assert result.criteria[0].passed is False
        assert new_state["error_seen"] is True

    def test_poll_clean_stays_open(self):
        handle = FakeHandle(stdout_chunks=["normal request log line\n"], done=False)
        tc = ge.TcS3Run002RgwLogMonitor()
        state = {"handle": handle, "error_seen": False, "error_lines": []}
        new_state, result = fw.poll_test_case(tc, _ctx(), state)
        assert result.status == fw.TestStatus.RUNNING
        assert result.criteria[0].passed is None


class TestTcS3Run004InstanceDowntime:
    def test_poll_flags_downtime_over_threshold(self, monkeypatch):
        handle = FakeHandle(done=False)
        down_log = "\n".join([f"2026-08-06T00:00:{i:02d} DOWN" for i in range(35)])
        _fake_run_ceph_command(monkeypatch, {"cat /tmp/s3_run004_probe.log": down_log})
        tc = ge.TcS3Run004InstanceDowntime()
        state = {"handle": handle, "client_host": "client1"}
        new_state, result = fw.poll_test_case(tc, _ctx(), state)
        assert result.criteria[0].passed is False

    def test_poll_short_downtime_stays_open(self, monkeypatch):
        handle = FakeHandle(done=False)
        down_log = "2026-08-06T00:00:00 DOWN\n2026-08-06T00:00:01 UP"
        _fake_run_ceph_command(monkeypatch, {"cat /tmp/s3_run004_probe.log": down_log})
        tc = ge.TcS3Run004InstanceDowntime()
        state = {"handle": handle, "client_host": "client1"}
        new_state, result = fw.poll_test_case(tc, _ctx(), state)
        assert result.criteria[0].passed is None


class TestTcS3Post60ThroughputLatency:
    def test_start_launches_warp(self, monkeypatch):
        captured = {}

        def fake_execute_background(host, cmd):
            captured["cmd"] = cmd
            return FakeHandle()

        monkeypatch.setattr(ge, "execute_background", fake_execute_background)
        tc = ge.TcS3Post60ThroughputLatency()
        state = tc.start(_ctx())
        assert "warp mixed" in captured["cmd"]
        assert isinstance(state["handle"], FakeHandle)

    def test_poll_fail_on_nonzero_exit(self, monkeypatch):
        handle = FakeHandle(exit_code=1, done=True)
        _fake_run_ceph_command(monkeypatch, {"tail -c 3000 /tmp/s3_post60_warp.log": "some error"})
        tc = ge.TcS3Post60ThroughputLatency()
        state = {"handle": handle, "client_host": "client1"}
        new_state, result = fw.poll_test_case(tc, _ctx(), state)
        assert result.status == fw.TestStatus.FAIL

    def test_poll_connection_lost_is_not_a_clean_pass(self, monkeypatch):
        handle = FakeHandle(exit_code=None, done=True)
        _fake_run_ceph_command(monkeypatch, {"tail -c 3000 /tmp/s3_post60_warp.log": ""})
        tc = ge.TcS3Post60ThroughputLatency()
        state = {"handle": handle, "client_host": "client1"}
        new_state, result = fw.poll_test_case(tc, _ctx(), state)
        assert result.status == fw.TestStatus.RUNNING
        assert all(c.passed is None for c in result.criteria)
