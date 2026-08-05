"""Epic 10 Story 10.4: tests for the 41 Group B (POST) test cases. Fakes
worker.executor.test_runner.group_b's own module-level `run_ceph_command` /
`execute_with_retry` / `read_baseline_text` names (same "fake at the module
boundary" convention tests/test_test_runner_group_a.py already established)
rather than paramiko itself.
"""

import json

import pytest

from worker.executor.test_runner import framework as fw
from worker.executor.test_runner import group_b as gb


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
    """responses: dict[command_substring] -> output string (or Exception),
    checked in insertion order, first substring match wins."""

    def fake(host, command):
        for key, value in responses.items():
            if key == command or key in command:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"no fake response configured for command: {command!r}")

    monkeypatch.setattr(gb, "run_ceph_command", fake)


def _fake_execute_with_retry(monkeypatch, canned_output):
    """Every _run_script()/_verify_checksum_manifest() call (all routed
    through gb.execute_with_retry) returns the same canned_output --
    correct here because callers within a single test case's run() that
    issue more than one script call (e.g. TC-POST-010's per-image mount +
    cleanup calls) either discard the cleanup call's return value entirely
    or re-derive the same parsed shape from it, so reusing one fixed string
    is harmless and keeps the fakes readable.
    """
    monkeypatch.setattr(gb, "execute_with_retry", lambda host, command: canned_output)


def _fake_read_baseline_text(monkeypatch, mapping):
    def fake(key):
        return mapping.get(key)

    monkeypatch.setattr(gb, "read_baseline_text", fake)


CHECKSUM_STEP_OK = (
    '===STEP:checksum===\n'
    "file1.bin: OK\n"
    "file2.bin: OK\n"
    "EXIT:0\n"
)
CHECKSUM_STEP_FAILED = (
    '===STEP:checksum===\n'
    "file1.bin: OK\n"
    "file2.bin: FAILED\n"
    "EXIT:1\n"
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_group_b_tests_registry_has_41_default_constructible_classes():
    assert len(gb.GROUP_B_TESTS) == 41
    ids = [cls().id for cls in gb.GROUP_B_TESTS]
    assert len(ids) == len(set(ids)), "duplicate test ids in GROUP_B_TESTS"
    assert ids[0] == "TC-POST-001"
    assert "TC-POST-026" not in ids and "TC-POST-029" not in ids
    assert "TC-POST-025" in ids and "TC-POST-030" in ids


# ---------------------------------------------------------------------------
# TC-POST-001
# ---------------------------------------------------------------------------


class TestTcPost001:
    def test_uniform_version_passes(self, monkeypatch):
        versions = {
            "mon": {"ceph version 16.2.15 (abc) pacific (stable)": 3},
            "osd": {"ceph version 16.2.15 (abc) pacific (stable)": 10},
            "overall": {"ceph version 16.2.15 (abc) pacific (stable)": 13},
        }
        _fake_run_ceph_command(monkeypatch, {"ceph versions": json.dumps(versions)})
        result = fw.run_test_case(gb.TcPost001VersionUniform(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_mismatched_osd_version_fails(self, monkeypatch):
        versions = {
            "mon": {"ceph version 16.2.15 (abc) pacific (stable)": 3},
            "osd": {
                "ceph version 16.2.15 (abc) pacific (stable)": 8,
                "ceph version 14.2.22 (xyz) nautilus (stable)": 2,
            },
        }
        _fake_run_ceph_command(monkeypatch, {"ceph versions": json.dumps(versions)})
        result = fw.run_test_case(gb.TcPost001VersionUniform(), _ctx())
        assert result.status == fw.TestStatus.FAIL
        assert result.criteria[1].passed is False


# ---------------------------------------------------------------------------
# TC-POST-002
# ---------------------------------------------------------------------------


class TestTcPost002:
    def test_health_ok_passes(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph -s --format json": json.dumps({"health": {"status": "HEALTH_OK"}}),
                "ceph health detail": "HEALTH_OK",
            },
        )
        result = fw.run_test_case(gb.TcPost002ClusterHealth(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_health_warn_is_manual_review(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph -s --format json": json.dumps({"health": {"status": "HEALTH_WARN"}}),
                "ceph health detail": "HEALTH_WARN: some warning",
            },
        )
        result = fw.run_test_case(gb.TcPost002ClusterHealth(), _ctx())
        assert result.criteria[0].passed is None
        assert result.status == fw.TestStatus.RUNNING

    def test_health_err_fails(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph -s --format json": json.dumps({"health": {"status": "HEALTH_ERR"}}),
                "ceph health detail": "HEALTH_ERR",
            },
        )
        result = fw.run_test_case(gb.TcPost002ClusterHealth(), _ctx())
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-POST-003
# ---------------------------------------------------------------------------


class TestTcPost003:
    def test_all_active_clean_and_no_stuck_passes(self, monkeypatch):
        status = {"pgmap": {"num_pgs": 10, "pgs_by_state": [{"state_name": "active+clean", "count": 10}]}}
        _fake_run_ceph_command(
            monkeypatch, {"ceph -s --format json": json.dumps(status), "ceph pg dump_stuck": "ok"}
        )
        result = fw.run_test_case(gb.TcPost003PgIntegrity(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_partial_active_clean_fails(self, monkeypatch):
        status = {
            "pgmap": {
                "num_pgs": 10,
                "pgs_by_state": [{"state_name": "active+clean", "count": 8}, {"state_name": "down", "count": 2}],
            }
        }
        _fake_run_ceph_command(
            monkeypatch, {"ceph -s --format json": json.dumps(status), "ceph pg dump_stuck": "ok"}
        )
        result = fw.run_test_case(gb.TcPost003PgIntegrity(), _ctx())
        assert result.status == fw.TestStatus.FAIL

    def test_stuck_pgs_fail_second_criterion(self, monkeypatch):
        status = {"pgmap": {"num_pgs": 5, "pgs_by_state": [{"state_name": "active+clean", "count": 5}]}}
        _fake_run_ceph_command(
            monkeypatch, {"ceph -s --format json": json.dumps(status), "ceph pg dump_stuck": "1.2 stuck inactive"}
        )
        result = fw.run_test_case(gb.TcPost003PgIntegrity(), _ctx())
        assert result.status == fw.TestStatus.FAIL
        assert result.criteria[1].passed is False


# ---------------------------------------------------------------------------
# TC-POST-004
# ---------------------------------------------------------------------------


class TestTcPost004:
    def test_missing_baseline_is_error(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {})
        result = fw.run_test_case(gb.TcPost004OsdMapStructure(), _ctx())
        assert result.status == fw.TestStatus.ERROR

    def test_matching_count_and_empty_diff_passes(self, monkeypatch):
        crush_text = '{"devices": [{"id": 0}, {"id": 1}, {"id": 2}]}'
        _fake_read_baseline_text(monkeypatch, {"osd_crush_dump_before.json": crush_text})
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph osd stat --format json": json.dumps({"num_up_osds": 3, "num_in_osds": 3}),
                "ceph osd crush dump": crush_text,
            },
        )
        result = fw.run_test_case(gb.TcPost004OsdMapStructure(), _ctx())
        assert result.criteria[0].passed is True
        assert result.criteria[1].passed is True
        assert result.status == fw.TestStatus.PASS

    def test_osd_count_mismatch_fails(self, monkeypatch):
        crush_text = '{"devices": [{"id": 0}, {"id": 1}, {"id": 2}]}'
        _fake_read_baseline_text(monkeypatch, {"osd_crush_dump_before.json": crush_text})
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph osd stat --format json": json.dumps({"num_up_osds": 2, "num_in_osds": 2}),
                "ceph osd crush dump": crush_text,
            },
        )
        result = fw.run_test_case(gb.TcPost004OsdMapStructure(), _ctx())
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-POST-005 / 006 / 008 (baseline-diff severity distinctions, AC #2/#3)
# ---------------------------------------------------------------------------


class TestTcPost005:
    def test_missing_baseline_is_error(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {})
        result = fw.run_test_case(gb.TcPost005CapacityAndObjects(), _ctx())
        assert result.status == fw.TestStatus.ERROR

    def test_empty_diff_passes(self, monkeypatch):
        text = "POOL_NAME  ID  OBJECTS\nrbd_rep 1 500\n"
        _fake_read_baseline_text(monkeypatch, {"df_before.txt": text})
        _fake_run_ceph_command(monkeypatch, {"ceph df detail": text})
        result = fw.run_test_case(gb.TcPost005CapacityAndObjects(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_nonempty_diff_is_manual_review_not_fail(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"df_before.txt": "rbd_rep 1 500\n"})
        _fake_run_ceph_command(monkeypatch, {"ceph df detail": "rbd_rep 1 700\n"})
        result = fw.run_test_case(gb.TcPost005CapacityAndObjects(), _ctx())
        assert result.criteria[0].passed is None
        assert result.status == fw.TestStatus.RUNNING


class TestTcPost006:
    def test_diff_present_is_manual_review_not_fail(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"osd_crush_dump_before.json": '{"a": 1}'})
        _fake_run_ceph_command(monkeypatch, {"ceph osd crush dump": '{"a": 2}'})
        result = fw.run_test_case(gb.TcPost006CrushMapUnchanged(), _ctx())
        assert result.criteria[0].passed is None
        assert result.status == fw.TestStatus.RUNNING


class TestTcPost008:
    def test_diff_present_is_hard_fail_unlike_tc_post_006(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"auth_list_before.txt": "client.admin key=aaa\n"})
        _fake_run_ceph_command(monkeypatch, {"ceph auth list": "client.admin key=bbb\n"})
        result = fw.run_test_case(gb.TcPost008AuthKeyringIntact(), _ctx())
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_empty_diff_passes(self, monkeypatch):
        text = "client.admin key=aaa\n"
        _fake_read_baseline_text(monkeypatch, {"auth_list_before.txt": text})
        _fake_run_ceph_command(monkeypatch, {"ceph auth list": text})
        result = fw.run_test_case(gb.TcPost008AuthKeyringIntact(), _ctx())
        assert result.status == fw.TestStatus.PASS


# ---------------------------------------------------------------------------
# TC-POST-007
# ---------------------------------------------------------------------------


class TestTcPost007:
    def test_removed_option_surfaced_but_left_manual(self, monkeypatch):
        _fake_read_baseline_text(
            monkeypatch, {"config_dump_before.txt": "global basic bluestore_min_alloc_size_hdd 4096\n"}
        )
        _fake_run_ceph_command(monkeypatch, {"ceph config dump": "global basic mon_allow_pool_delete true\n"})
        result = fw.run_test_case(gb.TcPost007ConfigPreserved(), _ctx())
        assert result.criteria[0].passed is None
        assert "bluestore_min_alloc_size_hdd" in result.criteria[0].detail
        assert result.status == fw.TestStatus.RUNNING


# ---------------------------------------------------------------------------
# TC-POST-009
# ---------------------------------------------------------------------------


class TestTcPost009:
    def test_no_crash_passes(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph crash ls-new": ""})
        result = fw.run_test_case(gb.TcPost009NoNewCrash(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_new_crash_fails(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph crash ls-new": "2026-08-04_10:00:00 osd.3\n"})
        result = fw.run_test_case(gb.TcPost009NoNewCrash(), _ctx())
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-POST-010 / 011 (checksum verification)
# ---------------------------------------------------------------------------


class TestTcPost010:
    def test_requires_client_host(self):
        with pytest.raises(fw.TestCaseError):
            gb.TcPost010RbdReplicatedChecksum().run(_ctx(client_host=None))

    def test_requires_baseline(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {})
        result = fw.run_test_case(gb.TcPost010RbdReplicatedChecksum(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.ERROR

    def test_all_ok_passes(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"rbd_rep.sha256": "abc  file1.bin\ndef  file2.bin\n"})
        _fake_execute_with_retry(monkeypatch, CHECKSUM_STEP_OK)
        result = fw.run_test_case(gb.TcPost010RbdReplicatedChecksum(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.PASS

    def test_failed_checksum_fails(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"rbd_rep.sha256": "abc  file1.bin\ndef  file2.bin\n"})
        _fake_execute_with_retry(monkeypatch, CHECKSUM_STEP_FAILED)
        result = fw.run_test_case(gb.TcPost010RbdReplicatedChecksum(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.FAIL

    def test_one_image_ssh_failure_does_not_abort_remaining_images(self, monkeypatch):
        """Regression test for the code-review finding that a transient
        ExecutorError on one image used to propagate out of run() entirely,
        discarding every other image's already-completed results."""
        from worker.executor.ssh_executor import ExecutorError

        _fake_read_baseline_text(monkeypatch, {"rbd_rep.sha256": "abc  file1.bin\n"})
        calls = {"n": 0}

        def fake(host, command):
            calls["n"] += 1
            # Calls alternate checksum/cleanup per image (2 calls/image); fail
            # only the 2nd image's checksum call (the 3rd call overall).
            if calls["n"] == 3:
                raise ExecutorError("transient SSH failure")
            return CHECKSUM_STEP_OK

        monkeypatch.setattr(gb, "execute_with_retry", fake)
        result = fw.run_test_case(gb.TcPost010RbdReplicatedChecksum(), _ctx(client_host="client1"))
        assert calls["n"] == 10  # all 5 images attempted (2 SSH round trips each), loop wasn't aborted
        assert result.status == fw.TestStatus.FAIL  # the one SSH failure still counts against "100% OK"
        assert "testimage2" in result.criteria[0].detail


class TestTcPost011:
    def test_all_ok_passes(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"rbd_rep.sha256": "abc  file1.bin\ndef  file2.bin\n"})
        _fake_execute_with_retry(monkeypatch, CHECKSUM_STEP_OK)
        result = fw.run_test_case(gb.TcPost011RbdErasureCodedChecksum(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.PASS

    def test_failed_checksum_fails(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"rbd_rep.sha256": "abc  file1.bin\ndef  file2.bin\n"})
        _fake_execute_with_retry(monkeypatch, CHECKSUM_STEP_FAILED)
        result = fw.run_test_case(gb.TcPost011RbdErasureCodedChecksum(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-POST-012
# ---------------------------------------------------------------------------


class TestTcPost012:
    def test_clean_rollback_and_empty_diff_pass(self, monkeypatch):
        output = (
            "===STEP:snapls===\nsnap1\nEXIT:0\n"
            "===STEP:rollback===\nRolling back...\nEXIT:0\n"
            "===STEP:mapmount===\nEXIT:0\n"
            "===STEP:diff===\nEXIT:0\n"
            "===STEP:children===\nEXIT:0\n"
        )
        _fake_execute_with_retry(monkeypatch, output)
        result = fw.run_test_case(gb.TcPost012RbdSnapshotClone(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is None
        assert result.criteria[1].passed is True
        assert result.criteria[2].passed is True
        assert result.status == fw.TestStatus.RUNNING  # snap-ls criterion never auto-resolves

    def test_diff_mismatch_fails(self, monkeypatch):
        output = (
            "===STEP:snapls===\nsnap1\nEXIT:0\n"
            "===STEP:rollback===\nRolling back...\nEXIT:0\n"
            "===STEP:mapmount===\nEXIT:0\n"
            "===STEP:diff===\nonly in /mnt/verify_clone: extra.txt\nEXIT:1\n"
            "===STEP:children===\nEXIT:0\n"
        )
        _fake_execute_with_retry(monkeypatch, output)
        result = fw.run_test_case(gb.TcPost012RbdSnapshotClone(), _ctx(client_host="client1"))
        assert result.criteria[2].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_rollback_error_fails(self, monkeypatch):
        output = (
            "===STEP:snapls===\nsnap1\nEXIT:0\n"
            "===STEP:rollback===\nsome error\nEXIT:2\n"
            "===STEP:mapmount===\nEXIT:0\n"
            "===STEP:diff===\nEXIT:0\n"
            "===STEP:children===\nEXIT:0\n"
        )
        _fake_execute_with_retry(monkeypatch, output)
        result = fw.run_test_case(gb.TcPost012RbdSnapshotClone(), _ctx(client_host="client1"))
        assert result.criteria[1].passed is False
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-POST-013
# ---------------------------------------------------------------------------


class TestTcPost013:
    def test_count_and_checksum_match_pass(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"cephfs.sha256": "abc  a.txt\n"})

        calls = {"n": 0}

        def fake(host, command):
            calls["n"] += 1
            if calls["n"] == 1:
                return f"{gb.CEPHFS_EXPECTED_FILE_COUNT}\n"
            return CHECKSUM_STEP_OK

        monkeypatch.setattr(gb, "execute_with_retry", fake)
        result = fw.run_test_case(gb.TcPost013CephfsChecksum(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is True
        assert result.criteria[1].passed is True
        assert result.status == fw.TestStatus.PASS

    def test_file_count_mismatch_fails(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"cephfs.sha256": "abc  a.txt\n"})

        def fake(host, command):
            return "199999\n"

        monkeypatch.setattr(gb, "execute_with_retry", fake)
        result = fw.run_test_case(gb.TcPost013CephfsChecksum(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-POST-014 / 015 / 016 (disclosed partial-automation gaps)
# ---------------------------------------------------------------------------


class TestTcPost014:
    def test_always_manual_review(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ls ": "base\n"})
        result = fw.run_test_case(gb.TcPost014CephfsSnapshot(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is None
        assert result.status == fw.TestStatus.RUNNING


class TestTcPost015:
    def test_matching_object_count_passes_count_criterion(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"s3_manifest.csv": "obj1\nobj2\nobj3\n"})
        stats = {"usage": {"rgw.main": {"num_objects": 3}}}
        _fake_run_ceph_command(monkeypatch, {"radosgw-admin bucket stats": json.dumps(stats)})
        result = fw.run_test_case(gb.TcPost015S3DataIntegrity(), _ctx())
        assert result.criteria[0].passed is None  # verify_s3_manifest.py gap, always disclosed
        assert result.criteria[1].passed is True

    def test_mismatched_object_count_fails_second_criterion(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"s3_manifest.csv": "obj1\nobj2\nobj3\n"})
        stats = {"usage": {"rgw.main": {"num_objects": 1}}}
        _fake_run_ceph_command(monkeypatch, {"radosgw-admin bucket stats": json.dumps(stats)})
        result = fw.run_test_case(gb.TcPost015S3DataIntegrity(), _ctx())
        assert result.criteria[1].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_missing_baseline_is_error(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {})
        result = fw.run_test_case(gb.TcPost015S3DataIntegrity(), _ctx())
        assert result.status == fw.TestStatus.ERROR

    def test_malformed_bucket_stats_json_is_error(self, monkeypatch):
        _fake_read_baseline_text(monkeypatch, {"s3_manifest.csv": "obj1\nobj2\nobj3\n"})
        _fake_run_ceph_command(monkeypatch, {"radosgw-admin bucket stats": "not json"})
        result = fw.run_test_case(gb.TcPost015S3DataIntegrity(), _ctx())
        assert result.status == fw.TestStatus.ERROR

    def test_header_row_excluded_from_manifest_count(self, monkeypatch):
        manifest = "object_name,size,md5\nobj1,100,abcd\nobj2,200,efgh\nobj3,300,ijkl\n"
        _fake_read_baseline_text(monkeypatch, {"s3_manifest.csv": manifest})
        stats = {"usage": {"rgw.main": {"num_objects": 3}}}
        _fake_run_ceph_command(monkeypatch, {"radosgw-admin bucket stats": json.dumps(stats)})
        result = fw.run_test_case(gb.TcPost015S3DataIntegrity(), _ctx())
        # Without header exclusion this would compare 3 (live) vs 4 (rows including header) and fail.
        assert result.criteria[1].passed is True


class TestTcPost016:
    def test_always_manual_review(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"s3cmd ls": "2026-08-01 v1 testfile.txt\n"})
        result = fw.run_test_case(gb.TcPost016S3ObjectVersioning(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is None


# ---------------------------------------------------------------------------
# TC-POST-017 (background)
# ---------------------------------------------------------------------------


class TestFindPgStats:
    def test_top_level_pg_stats(self):
        assert gb._find_pg_stats({"pg_stats": [{"pgid": "1.0"}]}) == [{"pgid": "1.0"}]

    def test_nested_under_some_wrapper_key(self):
        """Regression test for the code-review finding that the old
        fallback guessed a specific wrapper key name ("pg_map") that didn't
        match this codebase's own "pgmap" convention and was never
        verified -- the fix searches one level deep for ANY wrapper
        containing pg_stats, regardless of its exact key name."""
        data = {"pgmap": {"pg_stats": [{"pgid": "2.0"}]}}
        assert gb._find_pg_stats(data) == [{"pgid": "2.0"}]

    def test_missing_entirely_returns_empty(self):
        assert gb._find_pg_stats({"something_else": {}}) == []


class TestTcPost017:
    def test_start_triggers_deep_scrub_and_snapshots_stamps(self, monkeypatch):
        pg_dump = {"pg_stats": [{"pgid": "1.0", "last_deep_scrub_stamp": "2026-08-04T00:00:00.000000+0000"}]}
        _fake_run_ceph_command(
            monkeypatch, {"ceph osd deep-scrub all": "", "ceph pg dump --format json": json.dumps(pg_dump)}
        )
        state = gb.TcPost017DeepScrubCluster().start(_ctx())
        assert "1.0" in state["baseline_stamps"]
        assert state["inconsistent_seen"] is False

    def test_poll_not_all_scrubbed_stays_open(self, monkeypatch):
        pg_dump = {
            "pg_stats": [
                {"pgid": "1.0", "last_deep_scrub_stamp": "2026-08-04T00:00:00.000000+0000"},
                {"pgid": "1.1", "last_deep_scrub_stamp": "2020-01-01T00:00:00.000000+0000"},
            ]
        }
        _fake_run_ceph_command(
            monkeypatch, {"ceph pg dump --format json": json.dumps(pg_dump), "ceph health detail": "HEALTH_OK"}
        )
        test_case = gb.TcPost017DeepScrubCluster()
        from datetime import datetime as _dt

        state = {"start_time": _dt(2026, 8, 3), "baseline_stamps": {}, "inconsistent_seen": False}
        new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert result.status == fw.TestStatus.RUNNING
        assert result.criteria[1].passed is None

    def test_poll_all_scrubbed_and_healthy_passes(self, monkeypatch):
        pg_dump = {"pg_stats": [{"pgid": "1.0", "last_deep_scrub_stamp": "2026-08-04T01:00:00.000000+0000"}]}
        _fake_run_ceph_command(
            monkeypatch, {"ceph pg dump --format json": json.dumps(pg_dump), "ceph health detail": "HEALTH_OK"}
        )
        test_case = gb.TcPost017DeepScrubCluster()
        from datetime import datetime as _dt

        state = {"start_time": _dt(2026, 8, 3), "baseline_stamps": {}, "inconsistent_seen": False}
        _new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert result.status == fw.TestStatus.PASS

    def test_inconsistent_is_sticky(self, monkeypatch):
        pg_dump = {"pg_stats": [{"pgid": "1.0", "last_deep_scrub_stamp": "2026-08-04T01:00:00.000000+0000"}]}
        _fake_run_ceph_command(
            monkeypatch, {"ceph pg dump --format json": json.dumps(pg_dump), "ceph health detail": "HEALTH_OK"}
        )
        test_case = gb.TcPost017DeepScrubCluster()
        from datetime import datetime as _dt

        state = {"start_time": _dt(2026, 8, 3), "baseline_stamps": {}, "inconsistent_seen": True}
        new_state, result = fw.poll_test_case(test_case, _ctx(), state)
        assert result.criteria[0].passed is False
        assert new_state["inconsistent_seen"] is True
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-POST-018 / 019 / 020 / 021 / 022 / 024 / 025
# ---------------------------------------------------------------------------


class TestTcPost018:
    def test_no_warning_no_mutation_passes(self, monkeypatch):
        calls = []

        def fake(host, command):
            calls.append(command)
            return "HEALTH_OK"

        monkeypatch.setattr(gb, "run_ceph_command", fake)
        result = fw.run_test_case(gb.TcPost018GlobalIdInsecure(), _ctx())
        assert result.criteria[0].passed is True
        assert not any("config set" in c for c in calls)

    def test_warning_present_applies_config_set_then_passes(self, monkeypatch):
        calls = []

        def fake(host, command):
            calls.append(command)
            if "config set" in command:
                return ""
            if len(calls) <= 1:
                return "AUTH_INSECURE_GLOBAL_ID_RECLAIM: warning"
            return "HEALTH_OK"

        monkeypatch.setattr(gb, "run_ceph_command", fake)
        result = fw.run_test_case(gb.TcPost018GlobalIdInsecure(), _ctx())
        assert any("config set" in c for c in calls)
        assert result.criteria[0].passed is True

    def test_warning_persists_after_mutation_fails(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {"ceph config set": "", "ceph health detail": "AUTH_INSECURE_GLOBAL_ID_RECLAIM: still warning"},
        )
        result = fw.run_test_case(gb.TcPost018GlobalIdInsecure(), _ctx())
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL


class TestTcPost019:
    def test_no_omap_warnings_passes(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph health detail": "HEALTH_OK", "ceph df detail": "ok"})
        result = fw.run_test_case(gb.TcPost019OmapWarningsGone(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_both_omap_warnings_fail(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph health detail": "BLUESTORE_NO_PER_POOL_OMAP and BLUESTORE_NO_PER_PG_OMAP present",
                "ceph df detail": "ok",
            },
        )
        result = fw.run_test_case(gb.TcPost019OmapWarningsGone(), _ctx())
        assert result.status == fw.TestStatus.FAIL


class TestTcPost020:
    def test_no_suggested_change_passes_second_criterion(self, monkeypatch):
        pools = [{"pool_name": "rbd_rep", "pg_num": 32, "pg_num_final": 32}]
        _fake_run_ceph_command(monkeypatch, {"autoscale-status": json.dumps(pools)})
        result = fw.run_test_case(gb.TcPost020PgAutoscaler(), _ctx())
        assert result.criteria[1].passed is True
        assert result.status == fw.TestStatus.RUNNING  # first criterion always None (no baseline)

    def test_suggested_change_is_manual_review(self, monkeypatch):
        pools = [{"pool_name": "rbd_rep", "pg_num": 32, "pg_num_final": 128}]
        _fake_run_ceph_command(monkeypatch, {"autoscale-status": json.dumps(pools)})
        result = fw.run_test_case(gb.TcPost020PgAutoscaler(), _ctx())
        assert result.criteria[1].passed is None

    def test_malformed_json_is_error_not_silent_empty_pools(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"autoscale-status": "not json"})
        result = fw.run_test_case(gb.TcPost020PgAutoscaler(), _ctx())
        assert result.status == fw.TestStatus.ERROR


class TestTcPost021:
    def test_upmap_mode_passes_first_criterion(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch, {"ceph balancer status": json.dumps({"mode": "upmap"}), "ceph balancer eval": ""}
        )
        result = fw.run_test_case(gb.TcPost021Balancer(), _ctx())
        assert result.criteria[0].passed is True

    def test_non_upmap_mode_fails_first_criterion(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch, {"ceph balancer status": json.dumps({"mode": "crush-compat"}), "ceph balancer eval": ""}
        )
        result = fw.run_test_case(gb.TcPost021Balancer(), _ctx())
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_malformed_json_is_error(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph balancer status": "not json", "ceph balancer eval": ""})
        result = fw.run_test_case(gb.TcPost021Balancer(), _ctx())
        assert result.status == fw.TestStatus.ERROR


class TestTcPost022:
    def test_always_manual_review(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph mgr module ls": "iostat\nrestful\n"})
        result = fw.run_test_case(gb.TcPost022MgrModules(), _ctx())
        assert result.criteria[0].passed is None


class TestTcPost023:
    def test_always_declined_as_skip(self):
        result = fw.run_test_case(gb.TcPost023DashboardWorks(), _ctx())
        assert result.status == fw.TestStatus.SKIP


class TestTcPost024:
    def test_metrics_present_passes_first_criterion(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"curl -s": "# HELP ceph_health_status\n# TYPE ceph_health_status gauge\n"})
        result = fw.run_test_case(gb.TcPost024PrometheusMetrics(), _ctx())
        assert result.criteria[0].passed is True

    def test_no_metrics_fails_first_criterion(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"curl -s": "curl: (7) Failed to connect"})
        result = fw.run_test_case(gb.TcPost024PrometheusMetrics(), _ctx())
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL


class TestTcPost025:
    def test_nonempty_status_passes(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph telemetry status": "channel_basic: on\n"})
        result = fw.run_test_case(gb.TcPost025Telemetry(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_empty_status_fails(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph telemetry status": ""})
        result = fw.run_test_case(gb.TcPost025Telemetry(), _ctx())
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-POST-030..039 (regression, throwaway resources)
# ---------------------------------------------------------------------------


class TestTcPost030:
    def test_create_delete_success_passes(self, monkeypatch):
        output = (
            "===STEP:create===\nEXIT:0\n"
            "===STEP:verify===\nregression_test_pool\nEXIT:0\n"
            "===STEP:delete===\npool deleted\nEXIT:0\n"
        )
        _fake_execute_with_retry(monkeypatch, output)
        result = fw.run_test_case(gb.TcPost030PoolCreateDelete(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_delete_failure_fails(self, monkeypatch):
        output = (
            "===STEP:create===\nEXIT:0\n"
            "===STEP:verify===\nregression_test_pool\nEXIT:0\n"
            "===STEP:delete===\nerror\nEXIT:1\n"
        )
        _fake_execute_with_retry(monkeypatch, output)
        result = fw.run_test_case(gb.TcPost030PoolCreateDelete(), _ctx())
        assert result.status == fw.TestStatus.FAIL


class TestTcPost031:
    def test_full_sequence_success_passes(self, monkeypatch):
        _fake_execute_with_retry(monkeypatch, "===STEP:full===\nEXIT:0\n")
        result = fw.run_test_case(gb.TcPost031RbdImageCreateDelete(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.PASS

    def test_requires_client_host(self):
        with pytest.raises(fw.TestCaseError):
            gb.TcPost031RbdImageCreateDelete().run(_ctx(client_host=None))


class TestTcPost032:
    def test_full_sequence_success_passes(self, monkeypatch):
        _fake_execute_with_retry(monkeypatch, "===STEP:full===\nEXIT:0\n")
        result = fw.run_test_case(gb.TcPost032RbdSnapshotCloneNew(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_failure_fails(self, monkeypatch):
        _fake_execute_with_retry(monkeypatch, "===STEP:full===\nerror\nEXIT:2\n")
        result = fw.run_test_case(gb.TcPost032RbdSnapshotCloneNew(), _ctx())
        assert result.status == fw.TestStatus.FAIL


class TestTcPost033:
    def test_not_configured_is_not_applicable(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"rbd mirror pool status": "mirroring not enabled"})
        result = fw.run_test_case(gb.TcPost033RbdMirroring(), _ctx())
        assert result.criteria[0].passed is None

    def test_error_state_fails(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"rbd mirror pool status": "testimage1: up+error"})
        result = fw.run_test_case(gb.TcPost033RbdMirroring(), _ctx())
        assert result.status == fw.TestStatus.FAIL

    def test_healthy_replaying_passes(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"rbd mirror pool status": "testimage1: up+replaying"})
        result = fw.run_test_case(gb.TcPost033RbdMirroring(), _ctx())
        assert result.status == fw.TestStatus.PASS


class TestTcPost034:
    def test_clean_io_no_warning_passes(self, monkeypatch):
        output = "===STEP:io===\nEXIT:0\n===STEP:slowcheck===\nEXIT:1\n"
        _fake_execute_with_retry(monkeypatch, output)
        result = fw.run_test_case(gb.TcPost034CephfsReadWrite(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.PASS

    def test_slow_metadata_warning_fails(self, monkeypatch):
        output = "===STEP:io===\nEXIT:0\n===STEP:slowcheck===\nMDS_SLOW_METADATA_IO: ...\nEXIT:0\n"
        _fake_execute_with_retry(monkeypatch, output)
        result = fw.run_test_case(gb.TcPost034CephfsReadWrite(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.FAIL


class TestTcPost035:
    def test_no_filesystem_raises(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph fs ls -f json": "[]"})
        result = fw.run_test_case(gb.TcPost035CephfsMultiMds(), _ctx())
        assert result.status == fw.TestStatus.ERROR

    def test_two_active_ranks_pass(self, monkeypatch):
        fs_status = {"mdsmap": [{"state": "active"}, {"state": "active"}]}
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph fs ls -f json": json.dumps([{"name": "cephfs"}]),
                "ceph fs status": json.dumps(fs_status),
            },
        )
        result = fw.run_test_case(gb.TcPost035CephfsMultiMds(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_one_active_rank_fails(self, monkeypatch):
        fs_status = {"mdsmap": [{"state": "active"}, {"state": "standby-replay"}]}
        _fake_run_ceph_command(
            monkeypatch,
            {
                "ceph fs ls -f json": json.dumps([{"name": "cephfs"}]),
                "ceph fs status": json.dumps(fs_status),
            },
        )
        result = fw.run_test_case(gb.TcPost035CephfsMultiMds(), _ctx())
        assert result.status == fw.TestStatus.FAIL

    def test_malformed_fs_ls_json_is_error(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph fs ls -f json": "not json"})
        result = fw.run_test_case(gb.TcPost035CephfsMultiMds(), _ctx())
        assert result.status == fw.TestStatus.ERROR

    def test_malformed_fs_status_json_is_error(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {"ceph fs ls -f json": json.dumps([{"name": "cephfs"}]), "ceph fs status": "not json"},
        )
        result = fw.run_test_case(gb.TcPost035CephfsMultiMds(), _ctx())
        assert result.status == fw.TestStatus.ERROR


class TestTcPost037:
    def test_requires_rgw_endpoint_vip(self):
        with pytest.raises(fw.TestCaseError):
            gb.TcPost037S3FullCrud().run(_ctx(client_host="client1", rgw_endpoint_vip=None))

    def test_full_crud_success_passes(self, monkeypatch):
        _fake_execute_with_retry(monkeypatch, "===STEP:crud===\nEXIT:0\n")
        result = fw.run_test_case(
            gb.TcPost037S3FullCrud(), _ctx(client_host="client1", rgw_endpoint_vip="http://rgw-vip:8080")
        )
        assert result.status == fw.TestStatus.PASS


class TestTcPost038:
    def test_admin_success_passes(self, monkeypatch):
        _fake_execute_with_retry(monkeypatch, "===STEP:admin===\nEXIT:0\n")
        result = fw.run_test_case(gb.TcPost038RgwAdmin(), _ctx())
        assert result.status == fw.TestStatus.PASS

    def test_requires_rgw_host(self):
        with pytest.raises(fw.TestCaseError):
            gb.TcPost038RgwAdmin().run(_ctx(rgw_hosts=[]))


class TestTcPost039:
    def test_requires_both_zone_endpoints(self):
        with pytest.raises(fw.TestCaseError):
            gb.TcPost039RgwMultisiteSync().run(
                _ctx(client_host="client1", rgw_endpoint_zone_a="http://a", rgw_endpoint_zone_b=None)
            )

    def test_synced_and_caught_up_passes(self, monkeypatch):
        _fake_execute_with_retry(monkeypatch, "===STEP:putA===\nEXIT:0\n===STEP:getB===\nEXIT:0\n")
        _fake_run_ceph_command(monkeypatch, {"radosgw-admin sync status": "data sync source: caught up"})
        result = fw.run_test_case(
            gb.TcPost039RgwMultisiteSync(),
            _ctx(client_host="client1", rgw_endpoint_zone_a="http://a", rgw_endpoint_zone_b="http://b"),
        )
        assert result.status == fw.TestStatus.PASS

    def test_not_caught_up_fails(self, monkeypatch):
        _fake_execute_with_retry(monkeypatch, "===STEP:putA===\nEXIT:0\n===STEP:getB===\nEXIT:0\n")
        _fake_run_ceph_command(monkeypatch, {"radosgw-admin sync status": "data sync source: behind"})
        result = fw.run_test_case(
            gb.TcPost039RgwMultisiteSync(),
            _ctx(client_host="client1", rgw_endpoint_zone_a="http://a", rgw_endpoint_zone_b="http://b"),
        )
        assert result.status == fw.TestStatus.FAIL


# ---------------------------------------------------------------------------
# TC-POST-036 / 040..045 -- deliberately-declined-automation group (AC scope)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "test_case_cls",
    [
        gb.TcPost036MdsFailover,
        gb.TcPost040AddOsd,
        gb.TcPost041RemoveOsd,
        gb.TcPost042ReplaceMon,
        gb.TcPost043RestartCluster,
        gb.TcPost044ErasureCodeFailure,
        gb.TcPost045InternalScripts,
    ],
)
def test_declined_automation_cases_always_skip(test_case_cls):
    result = fw.run_test_case(test_case_cls(), _ctx(client_host="client1"))
    assert result.status == fw.TestStatus.SKIP
    assert result.notes
