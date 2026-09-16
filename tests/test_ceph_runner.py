import paramiko
import pytest
import threading

from shared.ceph_runner import (
    CephCommandRunner,
    CephConnectionPool,
    CephRunnerError,
    CephSSHConfig,
    get_metrics,
)
from shared.request_context import reset_request_id, set_request_id


class FakeTransport:
    def is_active(self):
        return True

    def is_authenticated(self):
        return True


class FakeChannel:
    def __init__(self, payload=b"ok", *, stalled=False):
        self.payload = payload
        self.stalled = stalled
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def recv_ready(self):
        return bool(self.payload) and not self.stalled

    def recv(self, size):
        payload, self.payload = self.payload[:size], self.payload[size:]
        return payload

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, _size):
        return b""

    def exit_status_ready(self):
        return not self.stalled and not self.payload

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True


class FakeStream:
    def __init__(self, channel):
        self.channel = channel


class FakeClient:
    def __init__(self, *, stalled=False, auth_failed=False):
        self.transport = FakeTransport()
        self.stalled = stalled
        self.auth_failed = auth_failed
        self.connect_args = None
        self.exec_timeouts = []
        self.close_calls = 0

    def set_missing_host_key_policy(self, _policy):
        pass

    def connect(self, **kwargs):
        self.connect_args = kwargs
        if self.auth_failed:
            raise paramiko.AuthenticationException("bad credentials")

    def get_transport(self):
        return self.transport

    def exec_command(self, _command, timeout=None):
        assert timeout is not None
        self.exec_timeouts.append(timeout)
        channel = FakeChannel(stalled=self.stalled)
        return None, FakeStream(channel), FakeStream(channel)

    def close(self):
        self.close_calls += 1


def make_config(**overrides):
    values = dict(
        user="root",
        key_path="/tmp/key",
        known_hosts_path="/tmp/known_hosts",
        connect_timeout=5,
        banner_timeout=6,
        auth_timeout=7,
        max_connections=2,
    )
    values.update(overrides)
    return CephSSHConfig(**values)


def test_pool_passes_separate_connect_banner_and_auth_timeouts_and_reuses_client():
    clients = []

    def factory():
        client = FakeClient()
        clients.append(client)
        return client

    with CephConnectionPool(make_config(), client_factory=factory) as pool:
        runner = CephCommandRunner(pool)
        assert runner.run("10.0.0.1", "ceph -s", 1) == "ok"
        assert runner.run("10.0.0.1", "ceph df", 1) == "ok"

    assert len(clients) == 1
    assert 0 < clients[0].connect_args["timeout"] <= 1
    assert 0 < clients[0].connect_args["banner_timeout"] <= 1
    assert 0 < clients[0].connect_args["auth_timeout"] <= 1
    assert all(0 < timeout <= 1 for timeout in clients[0].exec_timeouts)
    assert clients[0].close_calls == 1


def test_pool_close_releases_open_connection_metric():
    before = get_metrics()["connections_open"]
    with CephConnectionPool(make_config(), client_factory=FakeClient) as pool:
        CephCommandRunner(pool).run("10.0.0.9", "ceph -s", 1)
        assert get_metrics()["connections_open"] == before + 1
    assert get_metrics()["connections_open"] == before


def test_command_metrics_include_safe_command_label_and_duration():
    with CephConnectionPool(make_config(), client_factory=FakeClient) as pool:
        CephCommandRunner(pool).run("10.0.0.8", "ceph -s -f json", 1)
        event = next(
            event for event in reversed(get_metrics()["recent"])
            if event["event"] == "command_success_total" and event["node"] == "10.0.0.8"
        )

    assert event["event"] == "command_success_total"
    assert event["command"] == "ceph -s"
    assert event["duration_ms"] >= 0


def test_command_metrics_carry_request_correlation_without_credentials():
    token = set_request_id("trace-123")
    try:
        with CephConnectionPool(make_config(), client_factory=FakeClient) as pool:
            CephCommandRunner(pool).run("10.0.0.7", "ceph -s", 1)
    finally:
        reset_request_id(token)

    event = next(
        event for event in reversed(get_metrics()["recent"])
        if event["event"] == "command_success_total" and event["node"] == "10.0.0.7"
    )
    assert event["request_id"] == "trace-123"
    assert "key_path" not in repr(event)


def test_authentication_failure_is_structured_and_not_retried():
    clients = []

    def factory():
        client = FakeClient(auth_failed=True)
        clients.append(client)
        return client

    pool = CephConnectionPool(make_config(), client_factory=factory)
    with pytest.raises(CephRunnerError) as caught:
        CephCommandRunner(pool).run("10.0.0.2", "ceph -s", 1)
    pool.close()

    assert caught.value.kind == "auth_failed"
    assert caught.value.stage == "connect"
    assert len(clients) == 1
    failure = next(
        event for event in reversed(get_metrics()["recent"])
        if event["event"] == "connect_failures_total" and event["node"] == "10.0.0.2"
    )
    assert failure["event"] == "connect_failures_total"
    assert failure["duration_ms"] >= 0


def test_command_has_total_deadline_and_closes_stalled_connection():
    clients = []

    def factory():
        client = FakeClient(stalled=True)
        clients.append(client)
        return client

    pool = CephConnectionPool(make_config(), client_factory=factory)
    with pytest.raises(CephRunnerError, match="exceeded") as caught:
        CephCommandRunner(pool).run("10.0.0.3", "ceph -s", 0.03)
    pool.close()

    assert caught.value.kind == "timeout"
    assert clients[0].close_calls >= 1


def test_host_lease_wait_is_bounded_and_reported_as_queue_timeout():
    pool = CephConnectionPool(make_config(), client_factory=FakeClient)
    with pool._lock:
        host_lock = pool._host_locks.setdefault("10.0.0.4", threading.Lock())
    host_lock.acquire()
    try:
        with pytest.raises(CephRunnerError) as caught:
            CephCommandRunner(pool).run("10.0.0.4", "ceph -s", 0.02)
    finally:
        host_lock.release()
        pool.close()

    assert caught.value.kind == "pool_wait_timeout"
    assert any(
        event["event"] == "queue_wait_timeout_total"
        and event["node"] == "10.0.0.4"
        for event in get_metrics()["recent"]
    )
