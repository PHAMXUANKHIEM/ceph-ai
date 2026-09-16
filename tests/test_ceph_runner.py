import paramiko
import pytest

from shared.ceph_runner import (
    CephCommandRunner,
    CephConnectionPool,
    CephRunnerError,
    CephSSHConfig,
    get_metrics,
)


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
    assert clients[0].connect_args["timeout"] == 5
    assert clients[0].connect_args["banner_timeout"] == 6
    assert clients[0].connect_args["auth_timeout"] == 7
    assert clients[0].close_calls == 1


def test_pool_close_releases_open_connection_metric():
    before = get_metrics()["connections_open"]
    with CephConnectionPool(make_config(), client_factory=FakeClient) as pool:
        CephCommandRunner(pool).run("10.0.0.9", "ceph -s", 1)
        assert get_metrics()["connections_open"] == before + 1
    assert get_metrics()["connections_open"] == before


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
