import pytest

import worker.executor.ssh_executor as ssh_executor
from worker.executor.ssh_executor import ExecutorError, execute_command


class _FakeChannel:
    def __init__(self, exit_status: int):
        self._exit_status = exit_status

    def recv_exit_status(self) -> int:
        return self._exit_status


class _FakeStream:
    def __init__(self, text: str, exit_status: int = 0):
        self._text = text
        self.channel = _FakeChannel(exit_status)

    def read(self) -> bytes:
        return self._text.encode()


class FakeSSHClient:
    """behavior maps host -> outcome:
    - "unreachable" -> connect() raises
    - (exit_status, stdout_text, stderr_text) -> exec_command returns that
    """

    behavior: dict = {}
    calls: list = []

    def __init__(self):
        self._host = None

    def set_missing_host_key_policy(self, policy):
        pass

    def load_host_keys(self, path):
        pass

    def save_host_keys(self, path):
        pass

    def connect(self, hostname, username, key_filename, timeout):
        FakeSSHClient.calls.append(hostname)
        outcome = FakeSSHClient.behavior.get(hostname, "unreachable")
        if outcome == "unreachable":
            raise OSError("no route to host")
        self._host = hostname

    def exec_command(self, command, timeout=None):
        exit_status, stdout_text, stderr_text = FakeSSHClient.behavior[self._host]
        return None, _FakeStream(stdout_text, exit_status), _FakeStream(stderr_text)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_ssh(monkeypatch):
    FakeSSHClient.behavior = {}
    FakeSSHClient.calls = []
    monkeypatch.setattr(ssh_executor.paramiko, "SSHClient", FakeSSHClient)
    yield FakeSSHClient


def test_execute_command_returns_stdout_on_success(fake_ssh):
    fake_ssh.behavior = {"10.20.1.249": (0, "ntp resynced\n", "")}

    output = execute_command("10.20.1.249", "some command")

    assert output == "ntp resynced\n"


def test_execute_command_raises_on_nonzero_exit(fake_ssh):
    fake_ssh.behavior = {"10.20.1.249": (1, "", "command not found")}

    with pytest.raises(ExecutorError, match="exited 1"):
        execute_command("10.20.1.249", "some bad command")


def test_execute_command_raises_on_connection_failure(fake_ssh):
    fake_ssh.behavior = {"10.20.1.249": "unreachable"}

    with pytest.raises(ExecutorError, match="failed to execute command"):
        execute_command("10.20.1.249", "some command")
