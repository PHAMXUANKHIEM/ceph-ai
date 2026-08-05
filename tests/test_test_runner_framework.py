"""Epic 10 Story 10.3: tests for the base TestCase/TestResult/CriterionResult
framework. Hand-rolled fakes + monkeypatch, matching this project's existing
ssh_executor test convention (tests/test_ssh_pool.py) rather than MagicMock.
"""

from worker.executor.ssh_executor import ExecutorError
from worker.executor.test_runner import framework as fw


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


class OneShotOkTestCase(fw.TestCase):
    id = "TC-FAKE-OK"
    name = "fake ok"
    priority = fw.TestPriority.P1

    def run(self, ctx):
        return fw.TestResult(
            test_id=self.id,
            status=fw.TestStatus.PENDING,
            criteria=[fw.CriterionResult("always true", passed=True)],
        )


class OneShotFailTestCase(fw.TestCase):
    id = "TC-FAKE-FAIL"
    name = "fake fail"
    priority = fw.TestPriority.P1

    def run(self, ctx):
        return fw.TestResult(
            test_id=self.id,
            status=fw.TestStatus.PENDING,
            criteria=[fw.CriterionResult("always false", passed=False)],
        )


class OneShotPreconditionTestCase(fw.TestCase):
    id = "TC-FAKE-PRECOND"
    name = "fake precondition"
    priority = fw.TestPriority.P1

    def run(self, ctx):
        raise fw.TestCaseError("missing precondition")


class OneShotSshFailureTestCase(fw.TestCase):
    id = "TC-FAKE-SSH"
    name = "fake ssh failure"
    priority = fw.TestPriority.P1

    def run(self, ctx):
        raise ExecutorError("host unreachable")


class OneShotBugTestCase(fw.TestCase):
    id = "TC-FAKE-BUG"
    name = "fake real bug"
    priority = fw.TestPriority.P1

    def run(self, ctx):
        raise TypeError("boom")


class OneShotDeclinedTestCase(fw.TestCase):
    id = "TC-FAKE-DECLINED"
    name = "fake declined"
    priority = fw.TestPriority.P1

    def run(self, ctx):
        raise fw.TestCaseDeclined("never automatable by design")


def _ctx(**overrides):
    defaults = dict(mon_host="mon1", osd_hosts=[], rgw_hosts=[], client_host=None)
    defaults.update(overrides)
    return fw.TestRunContext(**defaults)


class TestCriterionAndDecideStatus:
    def test_all_passed_is_pass(self):
        result = fw.TestResult(
            test_id="x",
            status=fw.TestStatus.PENDING,
            criteria=[fw.CriterionResult("a", True), fw.CriterionResult("b", True)],
        )
        assert result.decide_status() == fw.TestStatus.PASS

    def test_any_failed_is_fail(self):
        result = fw.TestResult(
            test_id="x",
            status=fw.TestStatus.PENDING,
            criteria=[fw.CriterionResult("a", True), fw.CriterionResult("b", False)],
        )
        assert result.decide_status() == fw.TestStatus.FAIL

    def test_none_criterion_keeps_running_when_no_failure(self):
        result = fw.TestResult(
            test_id="x",
            status=fw.TestStatus.PENDING,
            criteria=[fw.CriterionResult("a", True), fw.CriterionResult("b", None)],
        )
        assert result.decide_status() == fw.TestStatus.RUNNING

    def test_no_criteria_is_running(self):
        result = fw.TestResult(test_id="x", status=fw.TestStatus.PENDING, criteria=[])
        assert result.decide_status() == fw.TestStatus.RUNNING

    def test_false_wins_even_with_none_present(self):
        result = fw.TestResult(
            test_id="x",
            status=fw.TestStatus.PENDING,
            criteria=[fw.CriterionResult("a", None), fw.CriterionResult("b", False)],
        )
        assert result.decide_status() == fw.TestStatus.FAIL


class TestRunTestCase:
    def test_happy_path_sets_pass_status(self):
        result = fw.run_test_case(OneShotOkTestCase(), _ctx())
        assert result.status == fw.TestStatus.PASS
        assert result.started_at is not None
        assert result.finished_at is not None

    def test_fail_criterion_sets_fail_status(self):
        result = fw.run_test_case(OneShotFailTestCase(), _ctx())
        assert result.status == fw.TestStatus.FAIL

    def test_precondition_error_becomes_error_status(self):
        result = fw.run_test_case(OneShotPreconditionTestCase(), _ctx())
        assert result.status == fw.TestStatus.ERROR
        assert "missing precondition" in result.notes

    def test_ssh_failure_becomes_error_status(self):
        result = fw.run_test_case(OneShotSshFailureTestCase(), _ctx())
        assert result.status == fw.TestStatus.ERROR
        assert "host unreachable" in result.notes

    def test_unexpected_exception_propagates(self):
        try:
            fw.run_test_case(OneShotBugTestCase(), _ctx())
        except TypeError as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError("expected TypeError to propagate")

    def test_declined_becomes_skip_status_not_error(self):
        """TestCaseDeclined (added in Story 10.4) is a TestCaseError
        subclass but must map to SKIP, not ERROR -- distinguishing "never
        automatable by design" from "this run's config was incomplete"."""
        result = fw.run_test_case(OneShotDeclinedTestCase(), _ctx())
        assert result.status == fw.TestStatus.SKIP
        assert "never automatable by design" in result.notes


class BackgroundOkTestCase(fw.TestCase):
    id = "TC-FAKE-BG-OK"
    name = "fake background ok"
    priority = fw.TestPriority.P1
    background = True

    def poll(self, ctx, state):
        new_state = {**state, "ticks": state["ticks"] + 1}
        result = fw.TestResult(
            test_id=self.id,
            status=fw.TestStatus.PENDING,
            criteria=[fw.CriterionResult("still watching", passed=None)],
        )
        return new_state, result


class BackgroundErrorTestCase(fw.TestCase):
    id = "TC-FAKE-BG-ERR"
    name = "fake background error"
    priority = fw.TestPriority.P1
    background = True

    def poll(self, ctx, state):
        raise ExecutorError("connection dropped")


class BackgroundDeclinedTestCase(fw.TestCase):
    id = "TC-FAKE-BG-DECLINED"
    name = "fake background declined"
    priority = fw.TestPriority.P1
    background = True

    def poll(self, ctx, state):
        raise fw.TestCaseDeclined("never automatable by design")


class TestPollTestCase:
    def test_happy_path_threads_state_and_sets_running(self):
        new_state, result = fw.poll_test_case(BackgroundOkTestCase(), _ctx(), {"ticks": 0})
        assert new_state == {"ticks": 1}
        assert result.status == fw.TestStatus.RUNNING

    def test_ssh_failure_preserves_state_and_sets_error(self):
        state = {"ticks": 3}
        new_state, result = fw.poll_test_case(BackgroundErrorTestCase(), _ctx(), state)
        assert new_state == state
        assert result.status == fw.TestStatus.ERROR
        assert "connection dropped" in result.notes

    def test_declined_preserves_state_and_sets_skip(self):
        state = {"ticks": 3}
        new_state, result = fw.poll_test_case(BackgroundDeclinedTestCase(), _ctx(), state)
        assert new_state == state
        assert result.status == fw.TestStatus.SKIP
        assert "never automatable by design" in result.notes


class TestCheckBackgroundHandleHealth:
    def test_keyword_hit_is_error(self):
        handle = FakeHandle(stdout_chunks=["some Input/output error happened"], done=False)
        health = fw.check_background_handle_health(handle, False, ("input/output error",))
        assert health["error_seen"] is True
        assert health["done"] is False

    def test_clean_running_is_not_error(self):
        handle = FakeHandle(stdout_chunks=["iops=1000"], done=False)
        health = fw.check_background_handle_health(handle, False, ("input/output error",))
        assert health["error_seen"] is False

    def test_nonzero_exit_is_error_even_without_keyword(self):
        handle = FakeHandle(stdout_chunks=["fio finished"], exit_code=1, done=True)
        health = fw.check_background_handle_health(handle, False, ("input/output error",))
        assert health["error_seen"] is True
        assert health["exit_code"] == 1

    def test_clean_zero_exit_is_not_error(self):
        handle = FakeHandle(stdout_chunks=["fio finished"], exit_code=0, done=True)
        health = fw.check_background_handle_health(handle, False, ("input/output error",))
        assert health["error_seen"] is False

    def test_error_seen_is_sticky(self):
        handle = FakeHandle(stdout_chunks=["all clear now"], done=False)
        health = fw.check_background_handle_health(handle, True, ("input/output error",))
        assert health["error_seen"] is True


class TestRunCephCommand:
    def test_delegates_to_execute_with_retry(self, monkeypatch):
        calls = []

        def fake_execute_with_retry(host, command):
            calls.append((host, command))
            return "HEALTH_OK"

        monkeypatch.setattr(fw, "execute_with_retry", fake_execute_with_retry)
        output = fw.run_ceph_command("mon1", "ceph -s")
        assert output == "HEALTH_OK"
        assert calls == [("mon1", "ceph -s")]
