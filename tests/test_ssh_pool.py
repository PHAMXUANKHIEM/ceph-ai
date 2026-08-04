"""Unit tests for the pooled/retrying SSH additions to
worker/executor/ssh_executor.py (Epic 10, Story 10.1) -- execute_with_retry,
execute_background/BackgroundCommandHandle, test_all_nodes,
close_pooled_connection/close_all_pooled_connections.

Kept in a separate file from tests/test_ssh_executor.py (which only covers
execute_command(), untouched by this Epic) because the pooled path needs a
noticeably richer fake paramiko.SSHClient (a stateful transport +
open_session()-based background channel) than execute_command()'s simple
connect-once/read-once fake. Follows this project's existing convention
(see test_ssh_executor.py) of hand-rolled fake classes + monkeypatch --
no unittest.mock/MagicMock is used anywhere else in this test suite.
"""

import threading

import paramiko
import pytest

import worker.executor.ssh_executor as ssh_executor
from worker.executor.ssh_executor import (
    BackgroundCommandHandle,
    ExecutorError,
    close_all_pooled_connections,
    close_pooled_connection,
    execute_background,
    execute_with_retry,
)

# Imported under an alias -- pytest would otherwise try to collect the
# ssh_executor.test_all_nodes() function itself as a test case, since it
# matches the `test_*` naming pattern once bound as a name in this module.
from worker.executor.ssh_executor import test_all_nodes as check_all_nodes


class _FakeExecChannel:
    """Backs stdout.channel.recv_exit_status() for the blocking
    execute_with_retry() path, which drains via stdout.read()/stderr.read()
    -- the same simple shape execute_command() already uses -- so nothing
    fancier than an exit-status holder is needed here."""

    def __init__(self, exit_status: int):
        self._exit_status = exit_status

    def recv_exit_status(self) -> int:
        return self._exit_status


class _FakeExecStream:
    def __init__(self, text: str, exit_status: int = 0):
        self._text = text
        self.channel = _FakeExecChannel(exit_status)

    def read(self) -> bytes:
        return self._text.encode()


class _FakeBackgroundChannel:
    """Backs execute_background()'s non-blocking poll surface
    (exec_command/recv_ready/recv/recv_stderr_ready/recv_stderr/
    exit_status_ready/recv_exit_status), with test helpers to push output
    and mark completion incrementally."""

    def __init__(self):
        self.exec_command_calls = []
        self.timeout = None
        self._exit_status_ready = False
        self._exit_status = None
        self._stdout_queue = []
        self._stderr_queue = []

    def settimeout(self, value):
        self.timeout = value

    def exec_command(self, command):
        self.exec_command_calls.append(command)

    def recv_ready(self):
        return len(self._stdout_queue) > 0

    def recv(self, _n):
        return self._stdout_queue.pop(0)

    def recv_stderr_ready(self):
        return len(self._stderr_queue) > 0

    def recv_stderr(self, _n):
        return self._stderr_queue.pop(0)

    def exit_status_ready(self):
        return self._exit_status_ready

    def recv_exit_status(self):
        return self._exit_status

    # -- test helpers, not part of the paramiko.Channel surface --------
    def push_stdout(self, data: bytes):
        self._stdout_queue.append(data)

    def push_stderr(self, data: bytes):
        self._stderr_queue.append(data)

    def finish(self, exit_status: int = 0):
        self._exit_status_ready = True
        self._exit_status = exit_status


class _FakeTransport:
    def __init__(self, active: bool = True):
        self._active = active
        self.opened_sessions = []

    def is_active(self):
        return self._active

    def open_session(self):
        channel = _FakeBackgroundChannel()
        self.opened_sessions.append(channel)
        return channel


class FakePooledSSHClient:
    """Fake paramiko.SSHClient for the pooled/retry/background code path.

    Class-level `behavior` maps host -> connect() outcome:
    - "unreachable" -> connect() always raises OSError
    - int N         -> connect() raises OSError on the first N calls for
                        this host, then succeeds (retry-then-succeed)
    - absent/"ok"   -> connect() always succeeds

    Class-level `exec_behavior` maps host -> exec_command() outcome:
    - "raise"                       -> raises OSError (simulates a broken pipe)
    - (exit_status, stdout, stderr) -> normal result

    Every constructed instance is appended to `instances`, so tests can
    assert how many distinct clients were created (pooling verification).
    """

    behavior: dict = {}
    exec_behavior: dict = {}
    instances: list = []
    connect_calls: dict = {}

    def __init__(self):
        self._host = None
        self.transport = _FakeTransport(active=True)
        self.closed = False
        FakePooledSSHClient.instances.append(self)

    def set_missing_host_key_policy(self, policy):
        pass

    def load_host_keys(self, path):
        pass

    def save_host_keys(self, path):
        pass

    def connect(self, hostname, username, key_filename, timeout):
        calls = FakePooledSSHClient.connect_calls
        calls[hostname] = calls.get(hostname, 0) + 1
        outcome = FakePooledSSHClient.behavior.get(hostname, "ok")
        if outcome == "unreachable":
            raise OSError("no route to host")
        if isinstance(outcome, int) and calls[hostname] <= outcome:
            raise OSError("connection reset by peer")
        self._host = hostname

    def get_transport(self):
        return self.transport

    def exec_command(self, command, timeout=None):
        outcome = FakePooledSSHClient.exec_behavior.get(self._host, (0, "", ""))
        if outcome == "raise":
            raise OSError("broken pipe")
        exit_status, stdout_text, stderr_text = outcome
        return None, _FakeExecStream(stdout_text, exit_status), _FakeExecStream(stderr_text)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_paramiko(monkeypatch):
    FakePooledSSHClient.behavior = {}
    FakePooledSSHClient.exec_behavior = {}
    FakePooledSSHClient.instances = []
    FakePooledSSHClient.connect_calls = {}
    monkeypatch.setattr(ssh_executor.paramiko, "SSHClient", FakePooledSSHClient)
    monkeypatch.setattr(ssh_executor.time, "sleep", lambda *_a, **_kw: None)
    # ssh_executor._pool is a module-level singleton shared by the whole
    # app -- give every test a fresh one so pooled (fake) connections from
    # one test can't leak into the next.
    monkeypatch.setattr(ssh_executor, "_pool", ssh_executor._ConnectionPool())
    yield FakePooledSSHClient


# -- execute_with_retry: pooling ------------------------------------------


def test_execute_with_retry_reuses_pooled_connection():
    FakePooledSSHClient.exec_behavior["host1"] = (0, "hello\n", "")

    out1 = execute_with_retry("host1", "echo hello", user="root", key_path="/root/.ssh/id_rsa")
    out2 = execute_with_retry("host1", "echo hello", user="root", key_path="/root/.ssh/id_rsa")

    assert out1 == "hello\n"
    assert out2 == "hello\n"
    # Only one underlying client was ever constructed for this host -- the
    # second call reused the pooled connection instead of reconnecting.
    assert len(FakePooledSSHClient.instances) == 1


def test_execute_with_retry_nonzero_exit_raises_without_retry():
    FakePooledSSHClient.exec_behavior["host1"] = (1, "", "command not found")

    with pytest.raises(ExecutorError, match="exited 1"):
        execute_with_retry("host1", "bad-command", user="root", key_path="/root/.ssh/id_rsa")

    # A non-zero exit is a normal SSH round trip, not a connection failure
    # -- it must not be retried.
    assert FakePooledSSHClient.connect_calls.get("host1") == 1


# -- retry-then-raise / retry-then-succeed ---------------------------------


def test_unreachable_host_retries_3x_5s_apart_then_raises():
    sleeps = []
    ssh_executor.time.sleep = lambda s: sleeps.append(s)
    FakePooledSSHClient.behavior["unreachable-host"] = "unreachable"

    with pytest.raises(ExecutorError) as excinfo:
        execute_with_retry("unreachable-host", "echo hi", retries=3, delay=5, user="root", key_path="/k")

    assert "unreachable-host" in str(excinfo.value)
    assert FakePooledSSHClient.connect_calls["unreachable-host"] == 3
    assert sleeps == [5, 5]


def test_execute_with_retry_succeeds_after_transient_connect_failures():
    # Fails on the first 2 connect() attempts, succeeds on the 3rd.
    FakePooledSSHClient.behavior["flaky-host"] = 2
    FakePooledSSHClient.exec_behavior["flaky-host"] = (0, "up\n", "")

    out = execute_with_retry("flaky-host", "echo up", retries=3, delay=5, user="root", key_path="/k")

    assert out == "up\n"
    assert FakePooledSSHClient.connect_calls["flaky-host"] == 3


def test_execute_with_retry_repeated_exec_failure_retries_then_raises():
    FakePooledSSHClient.exec_behavior["host1"] = "raise"

    with pytest.raises(ExecutorError):
        execute_with_retry("host1", "echo hi", retries=3, delay=5, user="root", key_path="/k")

    # Each failed exec drops the pooled connection (it may be dead), so a
    # fresh client is constructed on each of the 3 attempts.
    assert len(FakePooledSSHClient.instances) == 3


def test_non_ssh_exception_is_not_retried_and_propagates(monkeypatch):
    """A programming-bug exception must not be retried or masked as
    ExecutorError -- it should propagate as-is on the first attempt."""

    def _boom(*_a, **_kw):
        raise TypeError("boom: not an SSH failure")

    monkeypatch.setattr(FakePooledSSHClient, "connect", _boom)

    with pytest.raises(TypeError):
        execute_with_retry("host1", "echo hi", user="root", key_path="/k")

    assert len(FakePooledSSHClient.instances) == 1


# -- dead-connection eviction ----------------------------------------------


def test_dead_pooled_connection_is_evicted_and_reconnected():
    FakePooledSSHClient.exec_behavior["host1"] = (0, "still alive\n", "")

    out1 = execute_with_retry("host1", "echo hi", user="root", key_path="/k")
    assert out1 == "still alive\n"
    assert len(FakePooledSSHClient.instances) == 1

    # Simulate the node rebooting: the pooled client's transport dies.
    FakePooledSSHClient.instances[0].transport._active = False

    out2 = execute_with_retry("host1", "echo hi", user="root", key_path="/k")
    assert out2 == "still alive\n"
    # The dead client was evicted and a second, fresh client constructed.
    assert len(FakePooledSSHClient.instances) == 2
    assert ssh_executor._pool._clients["host1"] is FakePooledSSHClient.instances[1]


# -- per-host-lock race fix -------------------------------------------------


def test_concurrent_execute_to_same_host_does_not_double_connect():
    """Regression test for the connection-pool race: multiple threads
    calling execute_with_retry() for the SAME host concurrently must result
    in exactly one underlying paramiko.SSHClient being constructed, with no
    connection leaked."""
    FakePooledSSHClient.exec_behavior["shared-host"] = (0, "ok\n", "")

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads
    errors = []

    def _worker(i):
        try:
            barrier.wait()
            results[i] = execute_with_retry("shared-host", "echo ok", user="root", key_path="/k")
        except Exception as exc:  # pragma: no cover -- surfaced via `errors`
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert all(r == "ok\n" for r in results)
    assert len(FakePooledSSHClient.instances) == 1


# -- large output round-trips correctly (deadlock-safe drain) --------------


def test_execute_with_retry_large_output_round_trips():
    """execute_with_retry() drains via stdout.read()/stderr.read() BEFORE
    checking exit status -- the same ordering execute_command() already
    uses to avoid the classic paramiko deadlock (read fully, THEN check
    exit status, never the reverse). This just confirms that ordering
    survives pooling/retry wrapping for a large multi-chunk payload."""
    big_stdout = "o" * 200_000
    big_stderr = "e" * 50_000
    FakePooledSSHClient.exec_behavior["host1"] = (0, big_stdout, big_stderr)

    out = execute_with_retry("host1", "produce-lots-of-output", user="root", key_path="/k")

    assert out == big_stdout
    assert len(out) == 200_000


# -- execute_background / BackgroundCommandHandle ---------------------------


def test_execute_background_returns_pollable_handle():
    FakePooledSSHClient.exec_behavior["host1"] = (0, "", "")  # unused by background path

    handle = execute_background("host1", "long-running-fio", user="root", key_path="/k")

    channel = FakePooledSSHClient.instances[0].transport.opened_sessions[0]
    assert channel.exec_command_calls == ["long-running-fio"]
    assert handle.is_done() is False
    assert handle.exit_code() is None

    channel.push_stdout(b"progress: 10%\n")
    out, err = handle.read_new_output()
    assert out == "progress: 10%\n"
    assert err == ""
    # Draining is incremental -- nothing left until more is pushed.
    assert handle.read_new_output() == ("", "")

    channel.finish(exit_code := 0)
    assert handle.is_done() is True
    assert handle.exit_code() == exit_code


def test_execute_background_retries_then_raises_on_repeated_start_failure():
    FakePooledSSHClient.behavior["host1"] = "unreachable"

    with pytest.raises(ExecutorError):
        execute_background("host1", "long-running-fio", retries=3, delay=5, user="root", key_path="/k")

    assert FakePooledSSHClient.connect_calls["host1"] == 3


def test_background_handle_survives_transport_death():
    """If the channel raises because the transport died mid-poll (e.g. a
    node reboot during an upgrade test), the handle must treat that as
    "done, nothing more coming" rather than letting the exception escape."""

    class _DyingChannel:
        def exit_status_ready(self):
            raise EOFError("transport closed")

        def recv_ready(self):
            raise OSError("socket closed")

        def recv_stderr_ready(self):
            raise OSError("socket closed")

        def recv_exit_status(self):
            raise EOFError("transport closed")

    handle = BackgroundCommandHandle("host1", "long-running-fio", _DyingChannel())

    assert handle.is_done() is True
    assert handle.poll() is True
    assert handle.exit_code() is None
    assert handle.read_new_output() == ("", "")


# -- test_all_nodes ----------------------------------------------------------


def test_test_all_nodes_mixed_reachability_does_not_raise():
    FakePooledSSHClient.behavior["node-down"] = "unreachable"
    FakePooledSSHClient.exec_behavior["node-up"] = (0, "", "")

    nodes = [
        {"host": "node-up", "user": "root", "key_path": "/root/.ssh/id_rsa"},
        {"host": "node-down", "user": "root", "key_path": "/root/.ssh/id_rsa"},
    ]

    results = check_all_nodes(nodes, retries=3, delay=5)

    assert results == {"node-up": True, "node-down": False}


def test_test_all_nodes_accepts_plain_hostnames_using_defaults():
    results = check_all_nodes(["host1", "host2"], user="root", key_path="/k")

    assert results == {"host1": True, "host2": True}


def test_test_all_nodes_missing_host_key_raises_value_error_not_key_error():
    nodes = [{"user": "root", "key_path": "/root/.ssh/id_rsa"}]  # no "host"

    with pytest.raises(ValueError):
        check_all_nodes(nodes)


def test_test_all_nodes_missing_user_or_key_path_raises_value_error():
    # Explicit None (not just absent) so this doesn't depend on whatever
    # settings.ssh_user/ssh_key_path happen to default to.
    nodes = [{"host": "host1", "user": None, "key_path": "/root/.ssh/id_rsa"}]

    with pytest.raises(ValueError):
        check_all_nodes(nodes)


# -- close / close_all --------------------------------------------------------


def test_close_pooled_connection_and_close_all():
    FakePooledSSHClient.exec_behavior["host1"] = (0, "", "")
    FakePooledSSHClient.exec_behavior["host2"] = (0, "", "")
    FakePooledSSHClient.exec_behavior["host3"] = (0, "", "")

    execute_with_retry("host1", "echo hi", user="root", key_path="/k")
    close_pooled_connection("host1")

    assert "host1" not in ssh_executor._pool._clients
    assert FakePooledSSHClient.instances[0].closed is True

    execute_with_retry("host2", "echo hi", user="root", key_path="/k")
    execute_with_retry("host3", "echo hi", user="root", key_path="/k")
    close_all_pooled_connections()

    assert ssh_executor._pool._clients == {}


def test_close_swallows_exceptions_from_underlying_close(monkeypatch):
    FakePooledSSHClient.exec_behavior["host1"] = (0, "", "")
    execute_with_retry("host1", "echo hi", user="root", key_path="/k")

    def _boom_close():
        raise OSError("already dead")

    monkeypatch.setattr(FakePooledSSHClient.instances[0], "close", _boom_close)

    # Must not raise even though the underlying close() blows up.
    close_pooled_connection("host1")
    assert "host1" not in ssh_executor._pool._clients
