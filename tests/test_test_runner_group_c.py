"""Epic 10 Story 10.5: tests for the 8 Group C (compatibility) test cases.
Fakes worker.executor.test_runner.group_c's own module-level
`run_ceph_command`/`run_script`/`execute_background` names (same "fake at
the module boundary" convention tests/test_test_runner_group_b.py already
established) rather than paramiko itself.
"""

import pytest

from worker.executor.test_runner import framework as fw
from worker.executor.test_runner import group_c as gc


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

    monkeypatch.setattr(gc, "run_ceph_command", fake)


def _fake_run_script(monkeypatch, canned_output):
    monkeypatch.setattr(gc, "run_script", lambda host, script: canned_output)


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


def test_group_c_tests_registry_has_8_default_constructible_classes():
    assert len(gc.GROUP_C_TESTS) == 8
    ids = [cls().id for cls in gc.GROUP_C_TESTS]
    assert len(ids) == len(set(ids))
    assert ids == [f"TC-COMPAT-{i:03d}" for i in range(1, 9)]


class TestTcCompat001:
    def test_start_requires_client_host(self):
        with pytest.raises(fw.TestCaseError):
            gc.TcCompat001OldClientDuringUpgrade().start(_ctx(client_host=None))

    def test_start_requires_rgw_endpoint(self):
        with pytest.raises(fw.TestCaseError):
            gc.TcCompat001OldClientDuringUpgrade().start(_ctx(client_host="client1", rgw_endpoint_vip=None))

    def test_start_launches_background_load(self, monkeypatch):
        captured = {}

        def fake_eb(host, command):
            captured["host"] = host
            captured["command"] = command
            return FakeHandle()

        monkeypatch.setattr(gc, "execute_background", fake_eb)
        state = gc.TcCompat001OldClientDuringUpgrade().start(_ctx(client_host="client1", rgw_endpoint_vip="http://vip:8080"))
        assert captured["host"] == "client1"
        assert "fio" in captured["command"]
        assert state["error_seen"] is False

    def test_poll_clean_run_keeps_criterion_open(self):
        handle = FakeHandle(stdout_chunks=["iops=1000"], done=False)
        new_state, result = fw.poll_test_case(
            gc.TcCompat001OldClientDuringUpgrade(), _ctx(), {"handle": handle, "error_seen": False}
        )
        assert result.status == fw.TestStatus.RUNNING
        assert result.criteria[0].passed is None
        assert result.criteria[1].passed is None

    def test_poll_io_error_fails_first_criterion(self):
        handle = FakeHandle(stdout_chunks=["Input/output error on file"], done=False)
        _new_state, result = fw.poll_test_case(
            gc.TcCompat001OldClientDuringUpgrade(), _ctx(), {"handle": handle, "error_seen": False}
        )
        assert result.status == fw.TestStatus.FAIL
        assert result.criteria[0].passed is False


class TestTcCompat002:
    def test_requires_client_host(self):
        with pytest.raises(fw.TestCaseError):
            gc.TcCompat002KernelRbdClient().run(_ctx(client_host=None))

    def test_success_passes(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:full===\n5.4.0-generic\nDDRC:0\nEXIT:0\n")
        result = fw.run_test_case(gc.TcCompat002KernelRbdClient(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.PASS

    def test_failure_fails(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:full===\nrbd: error\nDDRC:0\nEXIT:1\n")
        result = fw.run_test_case(gc.TcCompat002KernelRbdClient(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.FAIL

    def test_dd_write_failure_fails_even_if_unmap_succeeds(self, monkeypatch):
        """Regression test: EXIT:$? alone (unmap's exit code) used to be the
        only signal checked, so a failed `dd` write followed by a
        successful `rbd unmap` would incorrectly report PASS."""
        _fake_run_script(monkeypatch, "===STEP:full===\ndd: write error\nDDRC:1\nEXIT:0\n")
        result = fw.run_test_case(gc.TcCompat002KernelRbdClient(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.FAIL

    def test_missing_ddrc_marker_is_manual_review(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:full===\nsome unexpected output\nEXIT:0\n")
        result = fw.run_test_case(gc.TcCompat002KernelRbdClient(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is None


class TestTcCompat003:
    def test_success_is_manual_review_not_pass(self, monkeypatch):
        """The blocklist criterion is disclosed manual-review (this engine
        can't identify "this client" in the blocklist output), so the
        overall result can never auto-resolve to PASS -- only RUNNING/FAIL."""
        _fake_run_script(monkeypatch, "===STEP:full===\nDDRC:0\nEXIT:0\n===STEP:blocklist===\nEXIT:0\n")
        result = fw.run_test_case(gc.TcCompat003KernelCephfsClient(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is True
        assert result.criteria[1].passed is None
        assert result.status == fw.TestStatus.RUNNING

    def test_mount_failure_fails(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:full===\nmount error\nDDRC:0\nEXIT:32\n")
        result = fw.run_test_case(gc.TcCompat003KernelCephfsClient(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.FAIL

    def test_dd_write_failure_fails_even_if_umount_succeeds(self, monkeypatch):
        """Regression test: same exit-code-masking bug as TcCompat002 --
        DDRC:$? now checked separately from the trailing EXIT:$? (umount's)."""
        _fake_run_script(monkeypatch, "===STEP:full===\ndd: write error\nDDRC:1\nEXIT:0\n")
        result = fw.run_test_case(gc.TcCompat003KernelCephfsClient(), _ctx(client_host="client1"))
        assert result.criteria[0].passed is False
        assert result.status == fw.TestStatus.FAIL

    def test_blocklist_output_surfaced_in_second_criterion(self, monkeypatch):
        """Regression test: the blocklist step used to be captured into
        raw_output but never inspected at all."""
        _fake_run_script(
            monkeypatch,
            "===STEP:full===\nDDRC:0\nEXIT:0\n===STEP:blocklist===\nclient.1234 10.0.0.5:0/123\nEXIT:0\n",
        )
        result = fw.run_test_case(gc.TcCompat003KernelCephfsClient(), _ctx(client_host="client1"))
        assert "client.1234" in result.criteria[1].detail


class TestTcCompat004:
    def test_success_passes(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:full===\ntest\nEXIT:0\n")
        result = fw.run_test_case(gc.TcCompat004CephFuseOldClient(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.PASS

    def test_failure_fails(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:full===\nfuse: error\nEXIT:1\n")
        result = fw.run_test_case(gc.TcCompat004CephFuseOldClient(), _ctx(client_host="client1"))
        assert result.status == fw.TestStatus.FAIL


class TestTcCompat005:
    def test_always_declined_as_skip(self):
        result = fw.run_test_case(gc.TcCompat005OpenstackIntegration(), _ctx())
        assert result.status == fw.TestStatus.SKIP


class TestTcCompat006:
    def test_always_declined_as_skip(self):
        result = fw.run_test_case(gc.TcCompat006KubernetesCephCsi(), _ctx())
        assert result.status == fw.TestStatus.SKIP


class TestTcCompat007:
    def test_requires_rgw_endpoint_vip(self):
        with pytest.raises(fw.TestCaseError):
            gc.TcCompat007S3Sdk().run(_ctx(client_host="client1", rgw_endpoint_vip=None))

    def test_success_passes(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:full===\nGET_OK\n{'Contents': []}\nDONE\nEXIT:0\n")
        result = fw.run_test_case(
            gc.TcCompat007S3Sdk(), _ctx(client_host="client1", rgw_endpoint_vip="http://vip:8080")
        )
        assert result.status == fw.TestStatus.PASS

    def test_mismatch_fails(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:full===\nGET_MISMATCH\nDONE\nEXIT:0\n")
        result = fw.run_test_case(
            gc.TcCompat007S3Sdk(), _ctx(client_host="client1", rgw_endpoint_vip="http://vip:8080")
        )
        assert result.status == fw.TestStatus.FAIL

    def test_script_failure_fails(self, monkeypatch):
        _fake_run_script(monkeypatch, "===STEP:full===\nTraceback...\nEXIT:1\n")
        result = fw.run_test_case(
            gc.TcCompat007S3Sdk(), _ctx(client_host="client1", rgw_endpoint_vip="http://vip:8080")
        )
        assert result.status == fw.TestStatus.FAIL


class TestTcCompat008:
    def test_value_found_is_manual_review(self, monkeypatch):
        _fake_run_ceph_command(
            monkeypatch,
            {"ceph osd dump": "min_compat_client luminous\n", "ceph features": "{}"},
        )
        result = fw.run_test_case(gc.TcCompat008MinCompatClient(), _ctx())
        assert result.criteria[0].passed is None
        assert "luminous" in result.criteria[0].detail

    def test_value_missing_is_manual_review_too(self, monkeypatch):
        _fake_run_ceph_command(monkeypatch, {"ceph osd dump": "other line\n", "ceph features": "{}"})
        result = fw.run_test_case(gc.TcCompat008MinCompatClient(), _ctx())
        assert result.criteria[0].passed is None
