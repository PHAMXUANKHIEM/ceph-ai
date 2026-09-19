#!/usr/bin/env python3
"""Small, allow-listed host helper for restarting Ceph AI containers.

This process is socket-activated by systemd.  The Dashboard gets access only
to this socket, never to the host system D-Bus or Podman socket.
"""

import json
import os
import socket
import subprocess
import syslog
import threading
import time


COMPOSE_PROJECT = os.environ.get("CEPH_AI_COMPOSE_PROJECT", "ceph-ai")
SERVICES = frozenset(("dashboard-web", "worker", "watcher", "code-repair"))
MAX_REQUEST_BYTES = 4096


def _resolve_container(service: str) -> tuple:
    """Resolve exactly one canonical Compose container by labels."""
    if service not in SERVICES:
        return None, "service is not allow-listed"
    try:
        result = subprocess.run(
            [
                "/usr/bin/podman", "ps", "-a",
                "--filter", "label=io.podman.compose.project=" + COMPOSE_PROJECT,
                "--filter", "label=com.docker.compose.service=" + service,
                "--format", "{{.Names}}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "container lookup failed: " + str(exc)
    if result.returncode != 0:
        return None, result.stderr.strip() or "container lookup failed"
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        return None, "no matching container found"
    if len(names) > 1:
        return None, "multiple matching containers found"
    return names[0], None


def _inspect(container: str) -> dict:
    try:
        result = subprocess.run(
            [
                "/usr/bin/podman", "inspect", container,
                "--format",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "missing", "health": "missing"}
    if result.returncode != 0:
        return {"state": "missing", "health": "missing"}
    state, _, health = result.stdout.strip().partition("|")
    return {"state": state or "unknown", "health": health or "none"}


def _force_finish_stop(container: str) -> dict:
    """Ensure a slow container stop reaches a startable state."""
    state = _inspect(container)
    if state["state"] in ("stopped", "exited", "created", "missing"):
        return state

    try:
        subprocess.run(
            ["/usr/bin/podman", "kill", "--signal", "KILL", container],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return state
    for _ in range(15):
        state = _inspect(container)
        if state["state"] in ("stopped", "exited", "created"):
            return state
        time.sleep(1)
    return state


def _restart(request: dict) -> dict:
    service = request.get("service")
    if not isinstance(service, str):
        return {"restarted": False, "error": "service is not allow-listed"}
    container, lookup_error = _resolve_container(service)
    if not container:
        return {"restarted": False, "error": lookup_error}

    wait = bool(request.get("wait", True))
    if not wait:
        try:
            process = subprocess.Popen(
                ["/usr/bin/podman", "restart", "--time", "45", container],
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        except OSError as exc:
            return {"restarted": False, "error": "restart launch failed: " + str(exc)}
        threading.Thread(
            target=_log_async_result,
            args=(process, container),
            daemon=True,
        ).start()
        # The Dashboard itself will be stopped by this operation before it can
        # render a completion response. 'accepted' is deliberately distinct
        # from 'restarted' so callers do not claim the operation already won.
        return {"restarted": False, "accepted": True, "container": container}

    stop_error = ""
    try:
        stop = subprocess.run(
            ["/usr/bin/podman", "stop", "--time", "45", container],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
        stop_error = stop.stderr.strip()
    except subprocess.TimeoutExpired:
        stop = None
        stop_error = "podman stop timed out"
    state = _force_finish_stop(container)
    if state["state"] not in ("stopped", "exited", "created"):
        error = stop_error or "container did not stop cleanly"
        return {"restarted": False, "container": container, "error": error}

    start = subprocess.run(
        ["/usr/bin/podman", "start", container],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=30,
    )
    if start.returncode != 0:
        return {"restarted": False, "container": container, "error": start.stderr.strip() or "podman start failed"}

    # podman restart returning zero means the process is running again.  Read
    # the state for a useful status without waiting on long healthcheck grace
    # periods (Worker/Watcher healthchecks intentionally use stale thresholds).
    state = _inspect(container)
    for _ in range(15):
        if state["state"] == "running":
            break
        if state["state"] in ("stopped", "exited", "missing"):
            break
        time.sleep(1)
        state = _inspect(container)
    return {"restarted": state["state"] == "running", "container": container, **state}


def _log_async_result(process: subprocess.Popen, container: str) -> None:
    try:
        stdout, stderr = process.communicate(timeout=75)
        if process.returncode == 0:
            syslog.syslog(syslog.LOG_INFO, "ceph-ai restart completed for %s: %s" % (container, stdout.strip()))
        else:
            syslog.syslog(syslog.LOG_ERR, "ceph-ai restart failed for %s: %s" % (container, stderr.strip()))
    except Exception as exc:
        syslog.syslog(syslog.LOG_ERR, "ceph-ai restart monitoring failed for %s: %s" % (container, exc))


def _handle(connection: socket.socket) -> None:
    connection.settimeout(10)
    chunks = []
    received = 0
    while received < MAX_REQUEST_BYTES:
        chunk = connection.recv(min(1024, MAX_REQUEST_BYTES - received))
        if not chunk:
            break
        chunks.append(chunk)
        received += len(chunk)
        if b"\n" in chunk:
            break
    payload = b"".join(chunks)
    try:
        request = json.loads(payload.decode("utf-8").strip())
        response = _restart(request if isinstance(request, dict) else {})
    except Exception as exc:  # malformed or timed-out requests are fail-closed
        response = {"restarted": False, "error": str(exc)}
    try:
        connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
    except OSError:
        syslog.syslog(syslog.LOG_WARNING, "ceph-ai restart client disconnected before response")


def main() -> None:
    if os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("socket activation is required")
    listener = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    while True:
        connection, _ = listener.accept()
        with connection:
            _handle(connection)


if __name__ == "__main__":
    main()
