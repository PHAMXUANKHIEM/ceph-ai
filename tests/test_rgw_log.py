import json

import pytest

import watcher.ceph_client as ceph_client
from config.settings import settings
from watcher.rgw_log import RgwLogError, fetch_rgw_log

RGW_HOST = "10.20.1.90"


class _FakeChannel:
    def __init__(self, exit_status=0):
        self._exit_status = exit_status

    def recv_exit_status(self):
        return self._exit_status


class _FakeStream:
    def __init__(self, text: str, exit_status: int = 0):
        self._text = text
        self.channel = _FakeChannel(exit_status)

    def read(self):
        return self._text.encode()


class FakeSSHClient:
    """Same shape as tests/test_collector.py's fake — records every command
    it was asked to run and returns canned text per host, keyed off
    `FakeSSHClient.output_by_host` (constant across every command to that
    host in a test, since each test here only ever runs one)."""

    output_by_host: dict = {}
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
        self._host = hostname

    def exec_command(self, command, timeout=None):
        FakeSSHClient.calls.append((self._host, command))
        text = FakeSSHClient.output_by_host.get(self._host, "")
        return None, _FakeStream(text), _FakeStream("")

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_ssh(monkeypatch):
    FakeSSHClient.output_by_host = {}
    FakeSSHClient.calls = []
    monkeypatch.setattr(ceph_client.paramiko, "SSHClient", FakeSSHClient)
    yield FakeSSHClient


def _configure_rgw(monkeypatch, container="ceph-rgw-B"):
    monkeypatch.setattr(settings, "ceph_rgw_nodes", RGW_HOST)
    monkeypatch.setattr(settings, "ceph_rgw_container_name", container)


def test_docker_mode_no_filter_tails_the_configured_container(fake_ssh, monkeypatch):
    _configure_rgw(monkeypatch)
    fake_ssh.output_by_host = {RGW_HOST: "line1\nline2\n"}

    output = fetch_rgw_log(RGW_HOST)

    assert output == "line1\nline2\n"
    host, command = fake_ssh.calls[0]
    assert host == RGW_HOST
    assert command == "docker logs ceph-rgw-B --tail 100 2>&1"


def test_docker_mode_with_filter_greps_over_a_wider_scan_window(fake_ssh, monkeypatch):
    _configure_rgw(monkeypatch)
    fake_ssh.output_by_host = {RGW_HOST: "matching line only\n"}

    output = fetch_rgw_log(RGW_HOST, "bucket-42")

    assert output == "matching line only\n"
    _host, command = fake_ssh.calls[0]
    assert command == (
        "docker logs ceph-rgw-B --tail 3000 2>&1 | grep -i -- bucket-42 | tail -n 300"
    )


def test_filter_text_is_shell_quoted_against_injection(fake_ssh, monkeypatch):
    _configure_rgw(monkeypatch)
    fake_ssh.output_by_host = {RGW_HOST: ""}

    fetch_rgw_log(RGW_HOST, "; rm -rf / #")

    _host, command = fake_ssh.calls[0]
    # shlex.quote wraps the hostile token in single quotes rather than
    # letting ";"/"#" terminate or comment out the piped command.
    assert "'; rm -rf / #'" in command
    assert command.count("|") == 2  # still exactly the grep + tail pipeline, nothing appended


def test_podman_mode_uses_podman_logs(fake_ssh, monkeypatch):
    _configure_rgw(monkeypatch)
    monkeypatch.setattr(settings, "ceph_exec_mode", "podman")
    fake_ssh.output_by_host = {RGW_HOST: "podman rgw log\n"}

    fetch_rgw_log(RGW_HOST)

    _host, command = fake_ssh.calls[0]
    assert command.startswith("podman logs ceph-rgw-B --tail 100")


def test_docker_mode_without_container_name_configured_raises_before_any_ssh_call(fake_ssh, monkeypatch):
    monkeypatch.setattr(settings, "ceph_rgw_nodes", RGW_HOST)
    monkeypatch.setattr(settings, "ceph_rgw_container_name", "")

    with pytest.raises(RgwLogError):
        fetch_rgw_log(RGW_HOST)
    assert fake_ssh.calls == []


def test_none_mode_uses_journalctl_with_radosgw_unit_glob(fake_ssh, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    fake_ssh.output_by_host = {RGW_HOST: "journalctl rgw log\n"}

    fetch_rgw_log(RGW_HOST)

    _host, command = fake_ssh.calls[0]
    assert command == "journalctl -u 'ceph-radosgw@*' -n 100 --no-pager 2>&1"


def test_none_mode_with_filter_pipes_through_grep(fake_ssh, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    fake_ssh.output_by_host = {RGW_HOST: "matched\n"}

    fetch_rgw_log(RGW_HOST, "500 Internal")

    _host, command = fake_ssh.calls[0]
    assert command == (
        "journalctl -u 'ceph-radosgw@*' -n 3000 --no-pager 2>&1"
        " | grep -i -- '500 Internal' | tail -n 300"
    )


def test_cephadm_mode_discovers_daemon_name_and_reads_its_log(fake_ssh, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    daemons = json.dumps([
        {"name": "mon.khiempx1"},
        {"name": "rgw.default.default.khiempx1.abcde"},
    ])

    def output_for(host, command):
        if command == "cephadm ls --no-detail":
            return daemons
        return "cephadm rgw log line\n"

    def routed_exec(self, command, timeout=None):
        fake_ssh.calls.append((self._host, command))
        return None, _FakeStream(output_for(self._host, command)), _FakeStream("")

    monkeypatch.setattr(fake_ssh, "exec_command", routed_exec)

    output = fetch_rgw_log(RGW_HOST)

    assert output == "cephadm rgw log line\n"
    commands = [c for _h, c in fake_ssh.calls]
    assert "cephadm ls --no-detail" in commands
    assert any(c.startswith("cephadm logs --name rgw.default.default.khiempx1.abcde") for c in commands)


def test_cephadm_mode_no_rgw_daemon_found_raises(fake_ssh, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    fake_ssh.output_by_host = {RGW_HOST: json.dumps([{"name": "mon.khiempx1"}])}

    with pytest.raises(RgwLogError):
        fetch_rgw_log(RGW_HOST)


def test_unreachable_host_raises_rgw_log_error(fake_ssh, monkeypatch):
    _configure_rgw(monkeypatch)

    def failing_connect(self, hostname, username, key_filename, timeout):
        raise OSError("no route to host")

    monkeypatch.setattr(fake_ssh, "connect", failing_connect)

    with pytest.raises(RgwLogError):
        fetch_rgw_log(RGW_HOST)


def test_blank_filter_text_is_treated_as_no_filter(fake_ssh, monkeypatch):
    _configure_rgw(monkeypatch)
    fake_ssh.output_by_host = {RGW_HOST: "line\n"}

    fetch_rgw_log(RGW_HOST, "   ")

    _host, command = fake_ssh.calls[0]
    assert "grep" not in command
