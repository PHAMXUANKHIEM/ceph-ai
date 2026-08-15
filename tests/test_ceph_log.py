import watcher.ceph_log as ceph_log


def test_docker_log_command_is_bounded_and_filter_is_shell_quoted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ceph_log, "run_command_on_node",
        lambda host, command, timeout: calls.append((host, command, timeout)) or "matched",
    )

    result = ceph_log.fetch_ceph_log("10.0.0.2", "osd", "slow'; touch /tmp/x; echo '")

    assert result == "matched"
    assert calls[0][0] == "10.0.0.2"
    assert "docker logs" in calls[0][1]
    assert "--tail 3000" in calls[0][1]
    assert "grep -i --" in calls[0][1]


def test_cephadm_discovers_only_requested_service(monkeypatch):
    calls = []

    def run(host, command, timeout):
        calls.append(command)
        if command == "cephadm ls --no-detail":
            return '[{"name":"mon.a"},{"name":"osd.1"},{"name":"osd.2"}]'
        return command

    monkeypatch.setattr(ceph_log, "run_command_on_node", run)
    result = ceph_log._fetch(
        lambda host, command: run(host, command, 15), "10.0.0.2", "osd", None,
        "cephadm", "", "", "",
    )

    assert "--- osd.1 ---" in result
    assert any("--name osd.1" in command for command in calls)
    assert any("--name osd.2" in command for command in calls)
    assert not any("--name mon.a" in command for command in calls)
