import json

import pytest

import watcher.ceph_client as ceph_client
from dashboard.ceph_tools import FIXED_TOOL_COMMANDS, run_ceph_command_tool, run_fixed_tool


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
    """Same shape as tests/test_ceph_client.py's fake — `behavior` maps
    host -> outcome: "unreachable" (connect() raises), "fail" (nonzero
    exit), or a JSON-serializable payload (success)."""

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
        FakeSSHClient.calls.append((hostname, None))
        outcome = FakeSSHClient.behavior.get(hostname, "unreachable")
        if outcome == "unreachable":
            raise OSError(f"no route to host {hostname}")
        self._host = hostname

    def exec_command(self, command, timeout=None):
        FakeSSHClient.calls[-1] = (self._host, command)
        outcome = FakeSSHClient.behavior[self._host]
        if outcome == "fail":
            return None, _FakeStream("", exit_status=1), _FakeStream("boom")
        return None, _FakeStream(json.dumps(outcome), exit_status=0), _FakeStream("")

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_ssh(monkeypatch):
    FakeSSHClient.behavior = {}
    FakeSSHClient.calls = []
    monkeypatch.setattr(ceph_client.paramiko, "SSHClient", FakeSSHClient)
    yield FakeSSHClient


# --- run_fixed_tool ----------------------------------------------------------


def test_run_fixed_tool_returns_parsed_json(fake_ssh, monkeypatch):
    from config.settings import settings

    mon = settings.ceph_mon_nodes.split(",")[0]
    fake_ssh.behavior = {mon: {"num_pools": 3}}

    result = run_fixed_tool("get_pool_list")

    assert result == {"num_pools": 3}
    assert fake_ssh.calls[-1][1] == f"docker exec {settings.ceph_container_name} ceph osd pool ls detail --format json"


def test_run_fixed_tool_returns_error_dict_when_all_mon_nodes_fail(fake_ssh):
    result = run_fixed_tool("get_cluster_status")

    assert "error" in result


def test_run_fixed_tool_rejects_unknown_tool_name():
    with pytest.raises(ValueError):
        run_fixed_tool("not_a_real_tool")


def test_every_fixed_tool_command_starts_with_ceph():
    for command in FIXED_TOOL_COMMANDS.values():
        assert command.startswith("ceph ")


# --- run_ceph_command_tool: allowed commands --------------------------------


def test_run_ceph_command_tool_allows_plain_read_only_command(fake_ssh, monkeypatch):
    from config.settings import settings

    mon = settings.ceph_mon_nodes.split(",")[0]
    fake_ssh.behavior = {mon: {"osds": []}}

    result = run_ceph_command_tool("ceph osd dump")

    assert result == {"osds": []}
    assert fake_ssh.calls[-1][1] == f"docker exec {settings.ceph_container_name} ceph osd dump --format json"


def test_run_ceph_command_tool_wraps_a_bare_list_result(fake_ssh, monkeypatch):
    # run_ceph_json_command can return a bare list for some subcommands
    # (e.g. "ceph osd pool ls detail" with >1 pool) — run_ceph_command_tool
    # always returns a dict so the AI-facing tool_result shape is uniform.
    from config.settings import settings

    mon = settings.ceph_mon_nodes.split(",")[0]
    fake_ssh.behavior = {mon: [{"pool": "a"}, {"pool": "b"}]}

    result = run_ceph_command_tool("ceph osd pool ls detail")

    assert result == {"result": [{"pool": "a"}, {"pool": "b"}]}


def test_run_ceph_command_tool_translates_hallucinated_ceph_rbd_trash(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "dashboard.ceph_tools.query_rbd_trash",
        lambda pool: captured.append(pool) or [{"id": "trash-1", "name": "old-volume"}],
    )

    result = run_ceph_command_tool("ceph rbd trash ls volumes")

    assert captured == ["volumes"]
    assert result == {"result": [{"id": "trash-1", "name": "old-volume"}]}


# --- run_ceph_command_tool: blocked commands --------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "osd dump",  # missing "ceph " prefix
        "ceph osd pool delete volumes volumes --yes-i-really-really-mean-it",
        "ceph auth rm client.admin",
        "ceph osd purge 3 --yes-i-really-mean-it",
        "ceph config set global mon_allow_pool_delete true",
        "ceph tell mon.* injectargs '--mon-allow-pool-delete=true'",  # single quotes: no blocked char, still fine
        "ceph osd pool rm volumes volumes --yes-i-really-really-mean-it",  # "rm" keyword
        "ceph status > /tmp/out",
        "ceph status | grep health",
        "ceph status; rm -rf /",
        "ceph status && rm -rf /",
        "ceph status `whoami`",
        "ceph status $(whoami)",
        "ceph osd pool create newpool 8 8",  # "create" keyword
        "ceph mon add mon.d 1.2.3.4:6789",  # "create" keyword ("mon add" also matches explicitly)
        "ceph auth get-or-create client.foo mon 'allow r'",  # "create" keyword
    ],
)
def test_run_ceph_command_tool_blocks_dangerous_commands(command, fake_ssh):
    result = run_ceph_command_tool(command)

    assert result["blocked"] is True
    assert fake_ssh.calls == []  # never even attempts SSH


def test_run_ceph_command_tool_blocked_reason_is_specific_for_missing_prefix():
    result = run_ceph_command_tool("osd dump")
    assert "ceph " in result["reason"]


def test_run_ceph_command_tool_blocked_reason_names_the_keyword():
    result = run_ceph_command_tool("ceph osd pool delete volumes volumes --yes-i-really-really-mean-it")
    assert "delete" in result["reason"]


def test_run_ceph_command_tool_case_insensitive_keyword_block(fake_ssh):
    result = run_ceph_command_tool("ceph auth RM client.admin")

    assert result["blocked"] is True
    assert fake_ssh.calls == []
