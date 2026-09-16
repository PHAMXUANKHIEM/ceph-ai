"""Bounded Paramiko connections and total-timeout command execution.

The pool is deliberately synchronous because the existing Ceph callers are
synchronous. Async route/collector code can run this boundary in a bounded
thread pool without putting Paramiko operations on the event loop.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import paramiko
from shared.request_context import get_request_id


logger = logging.getLogger(__name__)
_METRICS_LOCK = threading.Lock()
_METRICS = {
    "connections_opened_total": 0,
    "connections_closed_total": 0,
    "connections_open": 0,
    "connect_failures_total": 0,
    "command_success_total": 0,
    "command_failures_total": 0,
    "command_timeouts_total": 0,
    "output_limit_failures_total": 0,
    "queue_wait_total": 0,
    "queue_wait_timeout_total": 0,
    "recent": [],
}
_CEPH_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|])(?:sudo\s+)?(ceph|rbd|rados|radosgw-admin)\s+([A-Za-z0-9_.:-]+)"
)


def _command_label(command: str) -> str:
    """Extract a bounded command label without exposing arguments or secrets."""
    matches = _CEPH_COMMAND_RE.findall(command)
    if matches:
        executable, subcommand = matches[-1]
        return f"{executable} {subcommand}"
    return "unknown"


def _record_metric(event: str, **fields: object) -> None:
    with _METRICS_LOCK:
        _METRICS[event] += 1
        if fields:
            recent = _METRICS["recent"]
            item = {"event": event, **fields}
            request_id = get_request_id()
            if request_id:
                item["request_id"] = request_id
            recent.append(item)
            del recent[:-200]


def get_metrics() -> dict:
    """Return bounded runner diagnostics without connection credentials."""
    with _METRICS_LOCK:
        return {
            **{key: value for key, value in _METRICS.items() if key != "recent"},
            "recent": [dict(item) for item in _METRICS["recent"]],
        }


class CephRunnerError(RuntimeError):
    """Structured, safe-to-return error from a Ceph SSH operation."""

    def __init__(self, node: str, stage: str, kind: str, message: str, duration_ms: float = 0.0):
        self.node = node
        self.stage = stage
        self.kind = kind
        self.duration_ms = round(duration_ms, 2)
        self.message = message
        super().__init__(f"{node}: {message}")

    def as_dict(self) -> dict[str, object]:
        return {
            "node": self.node,
            "stage": self.stage,
            "kind": self.kind,
            "message": self.message,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class CephSSHConfig:
    user: str
    key_path: str
    known_hosts_path: str
    connect_timeout: float = 5
    banner_timeout: float = 5
    auth_timeout: float = 5
    max_connections: int = 8


class CephConnectionPool:
    """Bounded, host-key-pinned SSH client pool for one credential scope."""

    def __init__(self, config: CephSSHConfig, *, client_factory=None):
        if config.max_connections < 1:
            raise ValueError("max_connections must be positive")
        self.config = config
        self._client_factory = client_factory or paramiko.SSHClient
        self._clients: dict[str, paramiko.SSHClient] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()
        self._closed = False

    def __enter__(self) -> "CephConnectionPool":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _is_healthy(self, client: paramiko.SSHClient) -> bool:
        transport = client.get_transport() if hasattr(client, "get_transport") else None
        return bool(transport and transport.is_active() and transport.is_authenticated())

    def _connect(self, host: str, timeout: float | None = None) -> paramiko.SSHClient:
        if self._closed:
            raise CephRunnerError(host, "connect", "pool_closed", "SSH connection pool is closed")
        with self._lock:
            client = self._clients.get(host)
            if client is not None and self._is_healthy(client):
                return client
            if client is not None:
                self._close_client(host, client)
            if len(self._clients) >= self.config.max_connections:
                raise CephRunnerError(
                    host, "connect", "pool_exhausted",
                    f"SSH connection pool limit {self.config.max_connections} reached",
                )

            started = time.monotonic()
            client = self._client_factory()
            if os.path.exists(self.config.known_hosts_path):
                client.load_host_keys(self.config.known_hosts_path)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            try:
                connect_timeout = self.config.connect_timeout
                banner_timeout = self.config.banner_timeout
                auth_timeout = self.config.auth_timeout
                if timeout is not None:
                    connect_timeout = min(connect_timeout, timeout)
                    banner_timeout = min(banner_timeout, timeout)
                    auth_timeout = min(auth_timeout, timeout)
                client.connect(
                    hostname=host,
                    username=self.config.user,
                    key_filename=self.config.key_path or None,
                    timeout=connect_timeout,
                    banner_timeout=banner_timeout,
                    auth_timeout=auth_timeout,
                )
            except paramiko.AuthenticationException as exc:
                client.close()
                _record_metric(
                    "connect_failures_total", node=host, stage="connect", kind="auth_failed",
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
                raise CephRunnerError(host, "connect", "auth_failed", "SSH authentication failed", (time.monotonic() - started) * 1000) from exc
            except paramiko.BadHostKeyException as exc:
                client.close()
                _record_metric(
                    "connect_failures_total", node=host, stage="connect", kind="host_key_failed",
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
                raise CephRunnerError(host, "connect", "host_key_failed", "SSH host key was rejected", (time.monotonic() - started) * 1000) from exc
            except (TimeoutError, OSError, paramiko.SSHException) as exc:
                client.close()
                kind = "timeout" if isinstance(exc, TimeoutError) else "unreachable"
                _record_metric(
                    "connect_failures_total", node=host, stage="connect", kind=kind,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
                raise CephRunnerError(host, "connect", kind, str(exc) or type(exc).__name__, (time.monotonic() - started) * 1000) from exc
            self._clients[host] = client
            _record_metric(
                "connections_opened_total",
                node=host,
                stage="connect",
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            with _METRICS_LOCK:
                _METRICS["connections_open"] += 1
            return client

    def _close_client(self, host: str, client: paramiko.SSHClient) -> None:
        removed = self._clients.pop(host, None)
        try:
            client.close()
            if removed is not None:
                _record_metric("connections_closed_total", node=host, stage="close")
                with _METRICS_LOCK:
                    _METRICS["connections_open"] = max(0, _METRICS["connections_open"] - 1)
        except Exception:
            logger.warning("failed to close SSH connection to %s", host, exc_info=True)

    @contextmanager
    def lease(self, host: str, *, timeout: float | None = None) -> Iterator[paramiko.SSHClient]:
        """Lease a host connection; concurrent commands on one host serialize."""
        with self._lock:
            host_lock = self._host_locks.setdefault(host, threading.Lock())
        wait_started = time.monotonic()
        acquired = host_lock.acquire(timeout=max(0.0, timeout)) if timeout is not None else host_lock.acquire()
        wait_ms = (time.monotonic() - wait_started) * 1000
        if acquired:
            _record_metric(
                "queue_wait_total", node=host, stage="ssh_lease",
                duration_ms=round(wait_ms, 2),
            )
        else:
            _record_metric(
                "queue_wait_timeout_total", node=host, stage="ssh_lease",
                duration_ms=round(wait_ms, 2),
            )
            raise CephRunnerError(
                host, "connect", "pool_wait_timeout",
                "SSH host lease wait exceeded command deadline", wait_ms,
            )
        try:
            try:
                remaining = None if timeout is None else timeout - (time.monotonic() - wait_started)
                if remaining is not None and remaining <= 0:
                    raise CephRunnerError(
                        host, "connect", "pool_wait_timeout",
                        "SSH host lease wait exceeded command deadline", wait_ms,
                    )
                yield self._connect(host, remaining)
            except Exception:
                with self._lock:
                    client = self._clients.get(host)
                    if client is not None:
                        self._close_client(host, client)
                raise
        finally:
            host_lock.release()

    def invalidate(self, host: str) -> None:
        with self._lock:
            client = self._clients.get(host)
            if client is not None:
                self._close_client(host, client)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            clients = list(self._clients.items())
            self._clients.clear()
        for host, client in clients:
            try:
                client.close()
            except Exception:
                logger.warning("failed to close SSH connection to %s", host, exc_info=True)
            else:
                _record_metric("connections_closed_total", node=host, stage="close")
                with _METRICS_LOCK:
                    _METRICS["connections_open"] = max(0, _METRICS["connections_open"] - 1)


class CephCommandRunner:
    """Execute one remote command with a total wall-clock deadline."""

    def __init__(self, pool: CephConnectionPool, *, max_output_bytes: int = 8 * 1024 * 1024):
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self.pool = pool
        self.max_output_bytes = max_output_bytes

    def run(self, host: str, command: str, timeout: float, *, command_name: str | None = None) -> str:
        if timeout <= 0:
            raise ValueError("command timeout must be positive")
        started = time.monotonic()
        label = command_name or _command_label(command)
        try:
            with self.pool.lease(host, timeout=timeout) as client:
                _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
                output, error_output, exit_status = self._read_channel(
                    host, stdout, stderr, timeout, started
                )
                if exit_status != 0:
                    raise CephRunnerError(
                        host, "command", "command_failed",
                        f"command exited {exit_status}: {error_output[:2000]}",
                        (time.monotonic() - started) * 1000,
                    )
                result = output.decode(errors="replace")
                _record_metric(
                    "command_success_total",
                    node=host,
                    stage="command",
                    command=label,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                    response_bytes=len(output),
                )
                return result
        except CephRunnerError as exc:
            if exc.stage == "command":
                event = "command_timeouts_total" if exc.kind == "timeout" else "command_failures_total"
                if exc.kind == "output_limit":
                    event = "output_limit_failures_total"
                _record_metric(
                    event,
                    node=host,
                    stage="command",
                    command=label,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
            raise
        except Exception as exc:
            _record_metric(
                "command_failures_total",
                node=host,
                stage="command",
                command=label,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            raise CephRunnerError(
                host, "command", "command_failed", str(exc) or type(exc).__name__,
                (time.monotonic() - started) * 1000,
            ) from exc

    def _read_channel(self, host: str, stdout, stderr, timeout: float, started: float) -> tuple[bytes, bytes, int]:
        channel = stdout.channel
        # Small test doubles and older adapters may expose only read(); the
        # production Paramiko channel follows the bounded polling path below.
        if not hasattr(channel, "recv_ready"):
            output = stdout.read()
            error_output = stderr.read()
            if len(output) > self.max_output_bytes:
                raise CephRunnerError(host, "command", "output_limit", "stdout exceeded output limit")
            if len(error_output) > self.max_output_bytes:
                raise CephRunnerError(host, "command", "output_limit", "stderr exceeded output limit")
            return output, error_output, channel.recv_exit_status()

        output = bytearray()
        errors = bytearray()
        deadline = started + timeout
        channel.settimeout(min(0.25, timeout))
        try:
            while True:
                if time.monotonic() >= deadline:
                    channel.close()
                    raise CephRunnerError(
                        host, "command", "timeout", f"command exceeded {timeout:g} seconds",
                        (time.monotonic() - started) * 1000,
                    )
                received = False
                if channel.recv_ready():
                    output.extend(channel.recv(65536))
                    received = True
                if channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(65536))
                    received = True
                if len(output) > self.max_output_bytes or len(errors) > self.max_output_bytes:
                    channel.close()
                    raise CephRunnerError(host, "command", "output_limit", "command output exceeded limit")
                if channel.exit_status_ready():
                    while channel.recv_ready():
                        output.extend(channel.recv(65536))
                    while channel.recv_stderr_ready():
                        errors.extend(channel.recv_stderr(65536))
                    if len(output) > self.max_output_bytes or len(errors) > self.max_output_bytes:
                        raise CephRunnerError(host, "command", "output_limit", "command output exceeded limit")
                    return bytes(output), bytes(errors), channel.recv_exit_status()
                if not received:
                    time.sleep(0.01)
        except CephRunnerError:
            raise
        except (TimeoutError, OSError, paramiko.SSHException) as exc:
            channel.close()
            raise CephRunnerError(
                host, "command", "timeout", str(exc) or "channel read timed out",
                (time.monotonic() - started) * 1000,
            ) from exc
