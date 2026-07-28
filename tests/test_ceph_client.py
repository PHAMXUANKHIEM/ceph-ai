import json

import pytest

import watcher.ceph_client as ceph_client
from watcher.ceph_client import (
    CephQueryError,
    configured_rbd_pools,
    get_mon_nodes,
    get_upgrade_status,
    pause_upgrade,
    propose_next_version,
    query_cluster_health,
    query_cluster_health_with,
    query_rbd_iostat,
    query_rbd_trash,
    read_public_key,
    resume_upgrade,
    run_ceph_json_command,
    run_diagnostic_command,
    ssh_key_path_error,
    summarize_cluster_versions,
)


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
    """Stands in for paramiko.SSHClient. `behavior` maps host -> outcome:
    - "unreachable" -> connect() raises
    - "fail" -> exec_command returns a nonzero exit status
    - a JSON-serializable payload -> exec_command succeeds with that body
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
            raise OSError(f"no route to host {hostname}")
        self._host = hostname

    def exec_command(self, command, timeout=None):
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


def test_get_mon_nodes_parses_settings(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "1.1.1.1, 2.2.2.2 ,3.3.3.3")
    assert get_mon_nodes() == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_query_cluster_health_returns_parsed_json_from_first_node(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249,10.20.1.253")
    fake_ssh.behavior = {
        "10.20.1.150": {"status": "HEALTH_OK", "checks": {}},
    }

    result = query_cluster_health()

    assert result == {"status": "HEALTH_OK", "checks": {}}
    assert fake_ssh.calls == ["10.20.1.150"]


def test_query_cluster_health_falls_back_to_next_node_when_first_unreachable(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249,10.20.1.253")
    fake_ssh.behavior = {
        "10.20.1.150": "unreachable",
        "10.20.1.249": {"status": "HEALTH_WARN", "checks": {"MON_CLOCK_SKEW": {}}},
    }

    result = query_cluster_health()

    assert result["status"] == "HEALTH_WARN"
    assert fake_ssh.calls == ["10.20.1.150", "10.20.1.249"]


def test_query_cluster_health_falls_back_past_command_failure(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249,10.20.1.253")
    fake_ssh.behavior = {
        "10.20.1.150": "fail",
        "10.20.1.249": {"status": "HEALTH_OK", "checks": {}},
    }

    result = query_cluster_health()

    assert result["status"] == "HEALTH_OK"


def test_query_cluster_health_raises_when_all_nodes_fail(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249,10.20.1.253")
    fake_ssh.behavior = {
        "10.20.1.150": "unreachable",
        "10.20.1.249": "unreachable",
        "10.20.1.253": "fail",
    }

    with pytest.raises(CephQueryError):
        query_cluster_health()


def test_query_cluster_health_raises_clear_error_when_no_nodes_configured(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "")

    with pytest.raises(CephQueryError, match="no MON nodes configured"):
        query_cluster_health()


def test_query_cluster_health_falls_back_when_response_is_malformed(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249,10.20.1.253")
    fake_ssh.behavior = {
        "10.20.1.150": {"unexpected": "shape, no status field"},
        "10.20.1.249": {"status": "HEALTH_OK", "checks": {}},
    }

    result = query_cluster_health()

    assert result["status"] == "HEALTH_OK"
    assert fake_ssh.calls == ["10.20.1.150", "10.20.1.249"]


def test_query_cluster_health_rejects_response_with_invalid_status_value(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {
        "10.20.1.150": {"status": "NOT_A_REAL_STATUS", "checks": {}},
    }

    with pytest.raises(CephQueryError):
        query_cluster_health()


# --- Story 5.1: parameterized variant, for testing not-yet-saved Dashboard
# form values before they're written to .env -------------------------------


def test_query_cluster_health_with_returns_parsed_json_from_first_node(fake_ssh):
    fake_ssh.behavior = {"10.9.9.1": {"status": "HEALTH_OK", "checks": {}}}

    result = query_cluster_health_with(
        ["10.9.9.1", "10.9.9.2"], "ceph-mon-B", "root", "/root/.ssh/some_key"
    )

    assert result == {"status": "HEALTH_OK", "checks": {}}
    assert fake_ssh.calls == ["10.9.9.1"]


def test_query_cluster_health_with_falls_back_to_next_node(fake_ssh):
    fake_ssh.behavior = {
        "10.9.9.1": "unreachable",
        "10.9.9.2": {"status": "HEALTH_WARN", "checks": {"MON_CLOCK_SKEW": {}}},
    }

    result = query_cluster_health_with(
        ["10.9.9.1", "10.9.9.2"], "ceph-mon-B", "root", "/root/.ssh/some_key"
    )

    assert result["status"] == "HEALTH_WARN"


def test_query_cluster_health_with_raises_when_all_nodes_fail(fake_ssh):
    fake_ssh.behavior = {"10.9.9.1": "unreachable", "10.9.9.2": "unreachable"}

    with pytest.raises(CephQueryError):
        query_cluster_health_with(["10.9.9.1", "10.9.9.2"], "ceph-mon-B", "root", "/root/.ssh/some_key")


def test_query_cluster_health_with_raises_clear_error_when_no_nodes_given():
    with pytest.raises(CephQueryError, match="no MON nodes configured"):
        query_cluster_health_with([], "ceph-mon-B", "root", "/root/.ssh/some_key")


def test_query_cluster_health_with_uses_given_container_and_credentials_not_settings(
    fake_ssh, monkeypatch
):
    # settings holds different (unrelated) values — query_cluster_health_with
    # must ignore them entirely and use only what was passed in explicitly.
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "9.9.9.9")
    monkeypatch.setattr(ceph_client.settings, "ceph_container_name", "wrong-container")
    monkeypatch.setattr(ceph_client.settings, "ssh_user", "wrong-user")
    monkeypatch.setattr(ceph_client.settings, "ssh_key_path", "/wrong/key")
    fake_ssh.behavior = {"10.9.9.1": {"status": "HEALTH_OK", "checks": {}}}

    captured_users = []
    original_connect = fake_ssh.connect

    def spy_connect(self, hostname, username, key_filename, timeout):
        captured_users.append((username, key_filename))
        return original_connect(self, hostname, username, key_filename, timeout=timeout)

    monkeypatch.setattr(fake_ssh, "connect", spy_connect)

    result = query_cluster_health_with(
        ["10.9.9.1"], "ceph-mon-B", "custom-user", "/custom/key"
    )

    assert result["status"] == "HEALTH_OK"
    assert captured_users == [("custom-user", "/custom/key")]


def test_query_cluster_health_wrapper_still_reads_from_settings(fake_ssh, monkeypatch):
    # Regression guard: the refactor into query_cluster_health_with() must not
    # change query_cluster_health()'s existing behavior/signature.
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {"10.20.1.150": {"status": "HEALTH_OK", "checks": {}}}

    result = query_cluster_health()

    assert result == {"status": "HEALTH_OK", "checks": {}}


def test_ssh_key_path_error_returns_none_for_existing_readable_file(tmp_path):
    key_file = tmp_path / "test_key"
    key_file.write_text("fake key material")

    assert ssh_key_path_error(str(key_file)) is None


def test_ssh_key_path_error_rejects_directory(tmp_path):
    error = ssh_key_path_error(str(tmp_path))  # tmp_path itself is a directory

    assert error is not None
    assert "thư mục" in error


def test_ssh_key_path_error_reports_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist"

    error = ssh_key_path_error(str(missing))

    assert error is not None
    assert "không tồn tại" in error


# No test for the "exists but unreadable" branch — os.access(R_OK) bypasses
# permission bits entirely when running as root (verified: chmod 0o000 still
# reports readable), which is how this project's dev/test environment runs.
# The branch itself is simple enough (os.access negative case) to trust
# without a test that would be meaningless in this environment.


def test_read_public_key_returns_content_of_paired_pub_file(tmp_path):
    key_file = tmp_path / "test_key"
    key_file.write_text("fake private key material")
    pub_file = tmp_path / "test_key.pub"
    pub_file.write_text("ssh-ed25519 AAAAfakepubkey watcher@host\n")

    assert read_public_key(str(key_file)) == "ssh-ed25519 AAAAfakepubkey watcher@host"


def test_read_public_key_returns_none_when_pub_file_missing(tmp_path):
    key_file = tmp_path / "test_key"
    key_file.write_text("fake private key material")
    # no test_key.pub alongside it

    assert read_public_key(str(key_file)) is None


def test_read_public_key_returns_none_when_private_key_path_itself_invalid(tmp_path):
    missing = tmp_path / "does_not_exist"

    assert read_public_key(str(missing)) is None


def test_command_escapes_container_name_to_prevent_injection(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    monkeypatch.setattr(ceph_client.settings, "ceph_container_name", "ceph-mon-B; rm -rf /")
    fake_ssh.behavior = {"10.20.1.150": {"status": "HEALTH_OK", "checks": {}}}

    captured_commands = []
    original_exec_command = fake_ssh.exec_command

    def spy_exec_command(self, command, timeout=None):
        captured_commands.append(command)
        return original_exec_command(self, command, timeout=timeout)

    monkeypatch.setattr(fake_ssh, "exec_command", spy_exec_command)

    query_cluster_health()

    assert captured_commands == ["docker exec 'ceph-mon-B; rm -rf /' ceph health detail --format json"]


# --- Multi-deploy-mode support: docker (default) / podman (plain podman
# cluster, one fixed container name) / cephadm (real cephadm — `cephadm
# shell`, no container name) / none (ceph-deploy or a package install with
# `ceph` native on the host, no container at all) ----------------------------


def test_build_exec_command_docker_mode():
    from watcher.ceph_client import build_exec_command

    assert build_exec_command("docker", "ceph-mon-B", "ceph -s") == "docker exec ceph-mon-B ceph -s"


def test_build_exec_command_podman_mode():
    from watcher.ceph_client import build_exec_command

    # Plain podman cluster with one fixed, known container name — NOT
    # cephadm (see test_build_exec_command_cephadm_mode_ignores_container).
    assert (
        build_exec_command("podman", "ceph-mon-B", "ceph -s")
        == "podman exec ceph-mon-B ceph -s"
    )


def test_build_exec_command_cephadm_mode_ignores_container():
    from watcher.ceph_client import build_exec_command

    # Verified against a real cephadm/reef cluster: `cephadm shell` infers
    # fsid/config/keyring itself — no container name needed, and a direct
    # `podman exec` into the long-running mon container fails anyway (no
    # admin keyring mounted there).
    assert (
        build_exec_command("cephadm", "irrelevant", "ceph health detail --format json")
        == "cephadm shell -- ceph health detail --format json"
    )


def test_build_exec_command_none_mode_ignores_container():
    from watcher.ceph_client import build_exec_command

    # "none" = ceph-deploy/package install, ceph binaries native on the host
    # — no container to exec into, so the container arg is simply unused.
    assert build_exec_command("none", "irrelevant", "ceph -s") == "ceph -s"


def test_build_exec_command_rejects_unknown_mode():
    from watcher.ceph_client import build_exec_command

    with pytest.raises(ValueError, match="unknown ceph_exec_mode"):
        build_exec_command("docker-compose", "c", "ceph -s")


def test_query_cluster_health_with_podman_mode(fake_ssh, monkeypatch):
    fake_ssh.behavior = {"10.9.9.1": {"status": "HEALTH_OK", "checks": {}}}
    captured_commands = []
    original_exec_command = fake_ssh.exec_command

    def spy_exec_command(self, command, timeout=None):
        captured_commands.append(command)
        return original_exec_command(self, command, timeout=timeout)

    monkeypatch.setattr(fake_ssh, "exec_command", spy_exec_command)

    result = query_cluster_health_with(
        ["10.9.9.1"], "ceph-abcd-mon.host1", "root", "/root/.ssh/some_key", "podman"
    )

    assert result["status"] == "HEALTH_OK"
    assert captured_commands == ["podman exec ceph-abcd-mon.host1 ceph health detail --format json"]


def test_query_cluster_health_with_cephadm_mode_uses_shell_and_longer_timeout(fake_ssh, monkeypatch):
    fake_ssh.behavior = {"10.9.9.1": {"status": "HEALTH_OK", "checks": {}}}
    captured_commands = []
    captured_timeouts = []
    original_exec_command = fake_ssh.exec_command

    def spy_exec_command(self, command, timeout=None):
        captured_commands.append(command)
        captured_timeouts.append(timeout)
        return original_exec_command(self, command, timeout=timeout)

    monkeypatch.setattr(fake_ssh, "exec_command", spy_exec_command)

    result = query_cluster_health_with(
        ["10.9.9.1"], "irrelevant", "root", "/root/.ssh/some_key", "cephadm"
    )

    assert result["status"] == "HEALTH_OK"
    assert captured_commands == ["cephadm shell -- ceph health detail --format json"]
    # cephadm shell spins up a fresh container per call — needs more headroom
    # than the docker/podman default (see CEPHADM_COMMAND_TIMEOUT_SECONDS).
    from watcher.ceph_client import CEPHADM_COMMAND_TIMEOUT_SECONDS, COMMAND_TIMEOUT_SECONDS

    assert captured_timeouts == [CEPHADM_COMMAND_TIMEOUT_SECONDS]
    assert CEPHADM_COMMAND_TIMEOUT_SECONDS > COMMAND_TIMEOUT_SECONDS


def test_query_cluster_health_with_none_mode_runs_bare_command(fake_ssh, monkeypatch):
    fake_ssh.behavior = {"10.9.9.1": {"status": "HEALTH_OK", "checks": {}}}
    captured_commands = []
    original_exec_command = fake_ssh.exec_command

    def spy_exec_command(self, command, timeout=None):
        captured_commands.append(command)
        return original_exec_command(self, command, timeout=timeout)

    monkeypatch.setattr(fake_ssh, "exec_command", spy_exec_command)

    result = query_cluster_health_with(
        ["10.9.9.1"], "", "root", "/root/.ssh/some_key", "none"
    )

    assert result["status"] == "HEALTH_OK"
    assert captured_commands == ["ceph health detail --format json"]


def test_query_cluster_health_defaults_to_docker_mode_from_settings(fake_ssh, monkeypatch):
    # settings.ceph_exec_mode defaults to "docker" — confirms
    # query_cluster_health() actually reads it (not just hardcoding "docker").
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "none")
    fake_ssh.behavior = {"10.20.1.150": {"status": "HEALTH_OK", "checks": {}}}
    captured_commands = []
    original_exec_command = fake_ssh.exec_command

    def spy_exec_command(self, command, timeout=None):
        captured_commands.append(command)
        return original_exec_command(self, command, timeout=timeout)

    monkeypatch.setattr(fake_ssh, "exec_command", spy_exec_command)

    query_cluster_health()

    assert captured_commands == ["ceph health detail --format json"]


def test_run_diagnostic_command_rejects_unknown_command_id():
    # Only DIAGNOSTIC_COMMANDS keys may ever run — this is what makes the
    # Nodes-page CLI safe from arbitrary-shell injection: there is no path
    # from a client-supplied string straight into `command`.
    with pytest.raises(ValueError):
        run_diagnostic_command("10.20.1.150", "not_a_real_command")


def test_run_diagnostic_command_wraps_via_build_exec_command_for_cephadm_mode(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "cephadm")
    captured = {}

    def fake_run_remote_command(host, command, command_timeout=ceph_client.COMMAND_TIMEOUT_SECONDS):
        captured["host"] = host
        captured["command"] = command
        captured["timeout"] = command_timeout
        return "cluster:\n  id: abc\n"

    monkeypatch.setattr(ceph_client, "_run_remote_command", fake_run_remote_command)

    output = run_diagnostic_command("10.20.1.150", "ceph_status")

    assert captured["host"] == "10.20.1.150"
    assert captured["command"] == "cephadm shell -- ceph -s"
    assert captured["timeout"] == ceph_client.CEPHADM_COMMAND_TIMEOUT_SECONDS
    assert output == "cluster:\n  id: abc\n"


# --- Cluster Upgrade feature -------------------------------------------------


def test_propose_next_version_known_major():
    assert propose_next_version("18.2.4") == "19.2.0"
    assert propose_next_version("17.2.7") == "18.2.0"


def test_propose_next_version_unknown_major_returns_none():
    # Not a fabricated guess — a major version this table hasn't been
    # updated for yet must not silently suggest something wrong.
    assert propose_next_version("99.0.0") is None


def test_propose_next_version_unparseable_string_returns_none():
    assert propose_next_version("not-a-version") is None


def test_summarize_cluster_versions_uniform_cluster(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {
        "10.20.1.150": {
            "mon": {"ceph version 18.2.4 (abc) reef (stable)": 3},
            "mgr": {"ceph version 18.2.4 (abc) reef (stable)": 2},
            "osd": {"ceph version 18.2.4 (abc) reef (stable)": 6},
            "overall": {"ceph version 18.2.4 (abc) reef (stable)": 11},
        }
    }

    summary = summarize_cluster_versions()

    assert summary["current_version"] == "18.2.4"
    assert summary["is_mixed"] is False
    assert summary["distinct_versions"] == ["18.2.4"]
    assert summary["per_type"]["mon"] == ["18.2.4"]
    assert "overall" not in summary["per_type"]


def test_summarize_cluster_versions_mixed_cluster_has_no_current_version(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {
        "10.20.1.150": {
            "mon": {"ceph version 18.2.4 (abc) reef (stable)": 3},
            "osd": {"ceph version 19.2.0 (def) squid (stable)": 6},
        }
    }

    summary = summarize_cluster_versions()

    assert summary["current_version"] is None
    assert summary["is_mixed"] is True
    assert summary["distinct_versions"] == ["18.2.4", "19.2.0"]


def test_get_upgrade_status_requires_cephadm(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "docker")
    with pytest.raises(CephQueryError, match="cephadm"):
        get_upgrade_status()


def test_get_upgrade_status_returns_parsed_payload(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {"10.20.1.150": {"in_progress": True, "target_image": "19.2.0", "progress": "1/5"}}

    status = get_upgrade_status()

    assert status["in_progress"] is True
    assert status["progress"] == "1/5"
    assert status["progress_percent"] == 20.0


def test_get_upgrade_status_progress_percent_none_when_progress_missing(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {"10.20.1.150": {"in_progress": False}}

    status = get_upgrade_status()

    assert status["progress_percent"] is None


@pytest.mark.parametrize(
    "progress,expected",
    [
        (None, None),
        ("", None),
        ("1/5", 20.0),
        ("1/5 daemons upgraded", 20.0),
        ("5/5", 100.0),
        ("0/0", None),
        ("not a fraction", None),
    ],
)
def test_upgrade_progress_percent_parses_fraction(progress, expected):
    assert ceph_client._upgrade_progress_percent(progress) == expected


def test_pause_upgrade_requires_cephadm(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "docker")
    with pytest.raises(CephQueryError, match="cephadm"):
        pause_upgrade()


def test_pause_upgrade_sends_expected_command(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    captured = {}

    def fake_run_remote_command(host, command, command_timeout=ceph_client.COMMAND_TIMEOUT_SECONDS):
        captured["host"] = host
        captured["command"] = command

    monkeypatch.setattr(ceph_client, "_run_remote_command", fake_run_remote_command)

    pause_upgrade()

    assert captured["host"] == "10.20.1.150"
    assert captured["command"] == "cephadm shell -- ceph orch upgrade pause"


def test_resume_upgrade_sends_expected_command(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    captured = {}

    def fake_run_remote_command(host, command, command_timeout=ceph_client.COMMAND_TIMEOUT_SECONDS):
        captured["command"] = command

    monkeypatch.setattr(ceph_client, "_run_remote_command", fake_run_remote_command)

    resume_upgrade()

    assert captured["command"] == "cephadm shell -- ceph orch upgrade resume"


def test_run_diagnostic_command_wraps_via_build_exec_command_for_docker_mode(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "docker")
    monkeypatch.setattr(ceph_client.settings, "ceph_container_name", "ceph-mon-B")
    captured = {}

    def fake_run_remote_command(host, command, command_timeout=ceph_client.COMMAND_TIMEOUT_SECONDS):
        captured["command"] = command
        captured["timeout"] = command_timeout
        return "0  ssd  1.0  1.00000  1 TiB  ..."

    monkeypatch.setattr(ceph_client, "_run_remote_command", fake_run_remote_command)

    run_diagnostic_command("10.20.1.150", "ceph_osd_df")

    assert captured["command"] == "docker exec ceph-mon-B ceph osd df"
    assert captured["timeout"] == ceph_client.COMMAND_TIMEOUT_SECONDS


def test_run_diagnostic_command_truncates_long_output(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "none")
    long_output = "x" * (ceph_client.DIAGNOSTIC_OUTPUT_MAX_CHARS + 500)
    monkeypatch.setattr(
        ceph_client, "_run_remote_command", lambda host, command, command_timeout=None: long_output
    )

    output = run_diagnostic_command("10.20.1.150", "ceph_df")

    assert len(output) == ceph_client.DIAGNOSTIC_OUTPUT_MAX_CHARS


# --- run_ceph_json_command (mcp_ceph_server.py's Ceph cluster-query tools) --


def test_run_ceph_json_command_returns_parsed_json_from_first_node(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249")
    fake_ssh.behavior = {"10.20.1.150": {"num_osds": 6, "num_up_osds": 6}}

    host, parsed = run_ceph_json_command("ceph osd stat")

    assert host == "10.20.1.150"
    assert parsed == {"num_osds": 6, "num_up_osds": 6}


def test_run_ceph_json_command_falls_back_across_mon_nodes(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249")
    fake_ssh.behavior = {
        "10.20.1.150": "unreachable",
        "10.20.1.249": {"pools": []},
    }

    host, parsed = run_ceph_json_command("ceph osd pool ls detail")

    assert host == "10.20.1.249"
    assert parsed == {"pools": []}


def test_run_ceph_json_command_raises_when_all_nodes_fail(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150,10.20.1.249")
    fake_ssh.behavior = {"10.20.1.150": "unreachable", "10.20.1.249": "fail"}

    with pytest.raises(CephQueryError):
        run_ceph_json_command("ceph mon stat")


def test_run_ceph_json_command_raises_when_no_mon_nodes_configured(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "")

    with pytest.raises(CephQueryError, match="no MON nodes configured"):
        run_ceph_json_command("ceph df")


def test_run_ceph_json_command_falls_back_to_raw_output_on_invalid_json(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    monkeypatch.setattr(
        ceph_client, "_run_remote_command", lambda host, command, command_timeout=None: "not valid json"
    )

    host, parsed = run_ceph_json_command("ceph pg stat")

    assert host == "10.20.1.150"
    assert parsed == {"raw_output": "not valid json"}


def test_run_ceph_json_command_appends_format_json_and_uses_mcp_timeout(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "docker")
    monkeypatch.setattr(ceph_client.settings, "ceph_container_name", "ceph-mon-B")
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    captured = {}

    def fake_run_remote_command(host, command, command_timeout=None):
        captured["command"] = command
        captured["timeout"] = command_timeout
        return "{}"

    monkeypatch.setattr(ceph_client, "_run_remote_command", fake_run_remote_command)

    run_ceph_json_command("ceph df")

    assert captured["command"] == "docker exec ceph-mon-B ceph df --format json"
    assert captured["timeout"] == ceph_client.MCP_COMMAND_TIMEOUT_SECONDS


def test_run_ceph_json_command_uses_cephadm_timeout_in_cephadm_mode(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    captured = {}

    def fake_run_remote_command(host, command, command_timeout=None):
        captured["command"] = command
        captured["timeout"] = command_timeout
        return "{}"

    monkeypatch.setattr(ceph_client, "_run_remote_command", fake_run_remote_command)

    run_ceph_json_command("ceph mon stat")

    assert captured["command"] == "cephadm shell -- ceph mon stat --format json"


# --- configured_rbd_pools / query_rbd_iostat (2026-07-28, Volume
# performance monitoring — see watcher/volume_monitor.py. NOT verified
# against a real cluster's actual `rbd perf image iostat --format json`
# output; these tests only pin THIS codebase's own parsing logic against a
# best-effort assumed shape, documented in query_rbd_iostat's own
# docstring.) ------------------------------------------------------------


def test_configured_rbd_pools_parses_settings(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_rbd_pools", "vms, volumes ,backups")
    assert configured_rbd_pools() == ["vms", "volumes", "backups"]


def test_configured_rbd_pools_empty_by_default(monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_rbd_pools", "")
    assert configured_rbd_pools() == []


def test_query_rbd_iostat_parses_list_shaped_response(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {
        "10.20.1.150": [
            {
                "image": "disk-1",
                "read_ops": 40,
                "write_ops": 60,
                "read_latency_ms": 1.5,
                "write_latency_ms": 2.5,
            }
        ]
    }

    samples = query_rbd_iostat("vms")

    assert samples == [
        {
            "pool": "vms",
            "image": "disk-1",
            "iops": 100.0,
            "read_latency_ms": 1.5,
            "write_latency_ms": 2.5,
        }
    ]


def test_query_rbd_iostat_parses_dict_with_images_key(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {
        "10.20.1.150": {
            "images": [
                {"image": "disk-2", "read_ops": 10, "write_ops": 5, "read_latency_ms": 0.5, "write_latency_ms": 0.2}
            ]
        }
    }

    samples = query_rbd_iostat("vms")

    assert len(samples) == 1
    assert samples[0]["image"] == "disk-2"
    assert samples[0]["iops"] == 15.0


def test_query_rbd_iostat_returns_empty_list_for_unexpected_shape(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {"10.20.1.150": {"unexpected": "shape"}}

    assert query_rbd_iostat("vms") == []  # must not raise


def test_query_rbd_iostat_skips_entries_without_an_image_name(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {
        "10.20.1.150": [
            {"read_ops": 1, "write_ops": 1},  # no "image"/"name" key
            {"image": "disk-3", "read_ops": 2, "write_ops": 3, "read_latency_ms": 1, "write_latency_ms": 1},
        ]
    }

    samples = query_rbd_iostat("vms")

    assert len(samples) == 1
    assert samples[0]["image"] == "disk-3"


def test_query_rbd_iostat_raises_when_all_mon_nodes_fail(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {"10.20.1.150": "unreachable"}

    with pytest.raises(CephQueryError):
        query_rbd_iostat("vms")


# --- query_rbd_trash (2026-07-28, Volume Trash — dashboard/routes/volumes.py.
# Same "NOT verified against a real cluster's actual `rbd trash ls
# --format json` output" caveat as query_rbd_iostat above.) -------------


def test_query_rbd_trash_parses_list_shaped_response(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {
        "10.20.1.150": [
            {
                "id": "1234567890ab",
                "name": "old-disk",
                "deletion_time": "2026-07-28 10:00:00",
                "status": "expired",
            }
        ]
    }

    entries = query_rbd_trash("vms")

    assert entries == [
        {
            "id": "1234567890ab",
            "name": "old-disk",
            "deletion_time": "2026-07-28 10:00:00",
            "status": "expired",
        }
    ]


def test_query_rbd_trash_returns_empty_list_for_unexpected_shape(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {"10.20.1.150": {"unexpected": "shape"}}

    assert query_rbd_trash("vms") == []  # must not raise


def test_query_rbd_trash_skips_entries_without_an_id(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {
        "10.20.1.150": [
            {"name": "no-id-entry"},  # no "id" key
            {"id": "abc123", "name": "real-entry", "deletion_time": "x", "status": "y"},
        ]
    }

    entries = query_rbd_trash("vms")

    assert len(entries) == 1
    assert entries[0]["id"] == "abc123"


def test_query_rbd_trash_raises_when_all_mon_nodes_fail(fake_ssh, monkeypatch):
    monkeypatch.setattr(ceph_client.settings, "ceph_mon_nodes", "10.20.1.150")
    fake_ssh.behavior = {"10.20.1.150": "unreachable"}

    with pytest.raises(CephQueryError):
        query_rbd_trash("vms")
