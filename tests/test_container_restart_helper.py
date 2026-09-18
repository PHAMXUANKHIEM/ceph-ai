import importlib.util
import socket
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "deploy" / "container_restart_helper.py"
SPEC = importlib.util.spec_from_file_location("container_restart_helper", MODULE_PATH)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_rejects_non_allowlisted_service_without_podman(monkeypatch):
    called = []
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: called.append(args))

    result = helper._restart({"service": "host-shell"})

    assert result == {"restarted": False, "error": "service is not allow-listed"}
    assert called == []


def test_resolves_container_by_compose_labels(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return Completed(stdout="ceph-ai_worker_7\n")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    container, error = helper._resolve_container("worker")

    assert container == "ceph-ai_worker_7"
    assert error is None
    assert "label=io.podman.compose.project=ceph-ai" in commands[0]
    assert "label=com.docker.compose.service=worker" in commands[0]


def test_rejects_multiple_matching_containers(monkeypatch):
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda *args, **kwargs: Completed(stdout="ceph-ai_worker_1\nceph-ai_worker_2\n"),
    )

    container, error = helper._resolve_container("worker")

    assert container is None
    assert error == "multiple matching containers found"


def test_waiting_restart_stops_starts_and_reports_running(monkeypatch):
    commands = []
    inspect_states = iter(("stopped|none\n", "running|starting\n"))

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["ps", "-a"]:
            return Completed(stdout="ceph-ai_worker_1\n")
        if command[1] == "inspect":
            return Completed(stdout=next(inspect_states))
        return Completed()

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    result = helper._restart({"service": "worker", "wait": True})

    assert result["restarted"] is True
    assert result["container"] == "ceph-ai_worker_1"
    assert [command[1] for command in commands] == ["ps", "stop", "inspect", "start", "inspect"]


def test_async_restart_reports_accepted_not_completed(monkeypatch):
    class FakeProcess:
        pass

    started = []
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda *args, **kwargs: Completed(stdout="ceph-ai_dashboard-web_1\n"),
    )
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    class FakeThread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            return None

    monkeypatch.setattr(helper.threading, "Thread", FakeThread)

    result = helper._restart({"service": "dashboard-web", "wait": False})

    assert result == {
        "restarted": False,
        "accepted": True,
        "container": "ceph-ai_dashboard-web_1",
    }
    assert started


def test_socket_handler_returns_json_line(monkeypatch):
    monkeypatch.setattr(
        helper,
        "_restart",
        lambda request: {"restarted": False, "accepted": True, "container": request["service"]},
    )
    client, server = socket.socketpair()
    try:
        client.sendall(b'{"service":"worker"}\n')
        helper._handle(server)
        assert client.recv(4096) == b'{"restarted":false,"accepted":true,"container":"worker"}\n'
    finally:
        client.close()
        server.close()
