import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import paramiko

from config.settings import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
# paramiko's exec_command timeout is a channel READ timeout (no data for
# this many seconds -> PipeTimeout), not a total-runtime cap — but a real
# `dnf/apt install ceph` can sit silent for minutes while downloading
# packages over the network. 30s was tuned for quick checks like
# "systemctl | grep ceph" and killed real package-install remediation
# commands mid-run (verified live: 2026-07-24, upgrade_ceph_cluster_package_download
# failed with PipeTimeout after 30s even though dnf was still working).
COMMAND_TIMEOUT_SECONDS = 1800
KNOWN_HOSTS_PATH = os.path.expanduser("~/.ssh/ceph_lab_known_hosts")


class ExecutorError(Exception):
    """Raised for any remediation-command failure — connection failure or a
    non-zero exit status. Deliberately NOT related to ClaudeDiagnosisError;
    Worker must not retry a failed remediation command the way it retries a
    failed Claude call (Story 2.1's retry/DLX is for transient diagnosis
    failures, not for a command that's unlikely to succeed on a bare retry)."""


def execute_command(host: str, command: str) -> str:
    """Run `command` on `host` over SSH using the Worker's own keypair
    (AD-3: this module — worker/executor/ — is only ever loaded in the
    Worker process). Returns stdout on success; raises ExecutorError on
    connection failure or non-zero exit.

    Deliberately self-contained (no import from watcher/) — Watcher and
    Worker are independent processes/services and shouldn't depend on each
    other's internals, even though the underlying SSH mechanics are similar.
    """
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=settings.ssh_user,
            key_filename=settings.ssh_key_path,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        client.save_host_keys(KNOWN_HOSTS_PATH)
        _stdin, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
        # Read output fully BEFORE checking exit status — avoids the SSH
        # deadlock a remote command hitting the channel buffer limit would
        # otherwise cause (same fix as watcher/ceph_client.py, Story 1.3).
        output = stdout.read().decode()
        error_output = stderr.read().decode()
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            raise ExecutorError(f"{host}: command exited {exit_status}: {error_output}")
        return output
    except ExecutorError:
        raise
    except Exception as exc:
        raise ExecutorError(f"{host}: failed to execute command: {exc}") from exc
    finally:
        try:
            client.close()
        except Exception:
            logger.warning("execute_command: error closing SSH connection to %s", host)


# ---------------------------------------------------------------------------
# Epic 10 (Ceph Upgrade Test Runner): pooled + retrying SSH, additive to the
# execute_command() connect-per-call path above. execute_command() is left
# completely untouched — it has 15+ existing call sites across this app and
# must not regress. Everything below is new and only reachable by code that
# opts into it (execute_with_retry / execute_background / test_all_nodes).
#
# Design lifted from the throwaway ceph-upgrade-test-runner prototype's
# SSHManager (backend/executor/ssh_manager.py), which went through 3-agent
# adversarial review before this port: per-host connection pool, per-host
# locking so concurrent connects to the SAME host can't double-connect,
# dead-transport eviction, and a BackgroundCommandHandle for long-running
# commands (soak-test fio, per-OSD OMAP convert progress) that a blocking
# call can't represent. Adapted to this file's conventions: reuses
# ExecutorError (no new exception type), reuses KNOWN_HOSTS_PATH and
# CONNECT_TIMEOUT_SECONDS/COMMAND_TIMEOUT_SECONDS instead of the prototype's
# own EXEC_TIMEOUT_SECONDS=300 (that would silently reintroduce the exact
# "timeout too short for a real package install" incident
# COMMAND_TIMEOUT_SECONDS=1800 above was raised to fix), and defaults
# user/key_path to settings.ssh_user/settings.ssh_key_path the same way
# execute_command() implicitly does, while still allowing an explicit
# override per call (the future per-cluster Config UI will need that).
# ---------------------------------------------------------------------------

# SSH/network-layer exceptions that represent a transient connectivity
# failure and are therefore safe to retry. Anything else (e.g. a TypeError
# or KeyError surfacing a programming bug) must propagate immediately
# instead of being retried or masked as ExecutorError.
_RETRYABLE_EXCEPTIONS = (paramiko.SSHException, OSError, EOFError)

DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 5


def read_os_release(host: str) -> Dict[str, str]:
    """Reads and parses /etc/os-release on `host` (ID, VERSION_ID,
    PRETTY_NAME, ...) into a plain dict. A generic SSH primitive, no
    Ceph-specific logic — callers (e.g. shared/ceph_releases.py's
    OS-compatibility check for cluster upgrades) interpret the fields
    themselves. Raises ExecutorError (via execute_command) if the host is
    unreachable; returns an empty dict if the file has no parseable
    KEY=VALUE lines."""
    output = execute_command(host, "cat /etc/os-release")
    fields: Dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"')
    return fields


def _connect_client(host: str, user: str, key_path: str, timeout: int) -> "paramiko.SSHClient":
    """Open a single fresh SSH connection to host. Same host-key handling
    as execute_command() above (load/save KNOWN_HOSTS_PATH, AutoAddPolicy)
    — only used by the pooled path below, execute_command() has its own
    identical-looking inline copy that is intentionally left alone.
    """
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=user, key_filename=key_path, timeout=timeout)
    client.save_host_keys(KNOWN_HOSTS_PATH)
    return client


class _ConnectionPool:
    """Per-host cache of paramiko.SSHClient connections, reused across
    execute_with_retry()/execute_background()/test_all_nodes() calls
    instead of connecting fresh every time the way execute_command() does.
    A single instance of this (`_pool` below) is shared module-wide.
    """

    def __init__(self):
        self._clients: Dict[str, "paramiko.SSHClient"] = {}
        self._lock = threading.Lock()
        # Per-host locks so the "check pool -> connect if absent/dead ->
        # store" sequence in get() is atomic per host without serializing
        # gets for *different* hosts behind one global lock.
        self._host_locks: Dict[str, threading.Lock] = {}
        self._host_locks_guard = threading.Lock()

    def _host_lock(self, host: str) -> threading.Lock:
        with self._host_locks_guard:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    def get(self, host: str, user: str, key_path: str, timeout: int) -> "paramiko.SSHClient":
        """Return a live pooled client for host, (re)connecting under the
        host's own lock if there is no pooled entry yet or the pooled
        entry's transport has died (e.g. the node rebooted). Concurrent
        get() calls for the SAME host serialize on that host's lock so
        only one of them ever opens a new connection; get() calls for
        different hosts do not block each other.
        """
        host_lock = self._host_lock(host)
        with host_lock:
            with self._lock:
                client = self._clients.get(host)
                if client is not None:
                    transport = client.get_transport()
                    if transport is not None and transport.is_active():
                        return client
                    # Dead/stale entry -- evict, fall through to reconnect.
                    del self._clients[host]
            client = _connect_client(host, user, key_path, timeout)
            with self._lock:
                self._clients[host] = client
            return client

    def close(self, host: str) -> None:
        """Close and remove the pooled client for host, if any. Best-effort
        -- an exception from the underlying close() (e.g. transport already
        dead) is swallowed so cleanup of this host never raises.
        """
        with self._lock:
            client = self._clients.pop(host, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.warning("Ignoring error closing pooled SSH connection to %s", host, exc_info=True)

    def close_all(self) -> None:
        """Close and remove every pooled client. Best-effort per host --
        one host's close() failing does not prevent the rest from being
        cleaned up."""
        with self._lock:
            hosts = list(self._clients.keys())
        for host in hosts:
            self.close(host)


_pool = _ConnectionPool()


def _retry_ssh(host: str, func, retries: int, delay: int, action_desc: str):
    """Run func() up to `retries` times, sleeping `delay` seconds between
    attempts. Only _RETRYABLE_EXCEPTIONS (transient SSH/network failures)
    are retried -- anything else propagates immediately on the first
    attempt instead of being retried or masked. Raises ExecutorError
    (reusing the existing exception type, not a new one) with the last
    underlying error if every attempt fails.
    """
    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            return func()
        except _RETRYABLE_EXCEPTIONS as exc:
            last_error = exc
            logger.warning(
                "%s on host '%s' failed on attempt %d/%d: %s", action_desc, host, attempt, retries, exc
            )
            if attempt < retries:
                time.sleep(delay)
    logger.error("%s on host '%s' failed after %d attempts: %s", action_desc, host, retries, last_error)
    raise ExecutorError(f"{host}: {action_desc} failed after {retries} attempts: {last_error}")


def execute_with_retry(
    host: str,
    command: str,
    retries: int = DEFAULT_RETRY_ATTEMPTS,
    delay: int = DEFAULT_RETRY_DELAY_SECONDS,
    user: Optional[str] = None,
    key_path: Optional[str] = None,
    timeout: int = CONNECT_TIMEOUT_SECONDS,
) -> str:
    """Pooled + retried drop-in equivalent of execute_command(): returns
    stdout on success, raises ExecutorError on failure. Unlike
    execute_command(), the underlying SSH connection is pooled and reused
    across calls (see _ConnectionPool), and a connect/exec failure is
    retried `retries` times (`delay` seconds apart) before raising --
    execute_command() itself is unchanged and still connects fresh every
    call with no retry.

    Same deadlock-safe ordering as execute_command(): stdout and stderr are
    each read fully (blocking until EOF) BEFORE the exit status is checked.
    A non-zero exit is a normal, successful SSH round trip -- it raises
    ExecutorError immediately, same as execute_command(), and is NOT
    retried. Only SSH-layer failures (dropped connection, channel errors,
    etc.) trigger the retry-then-raise behavior; when one of those happens,
    the pooled connection for this host is dropped so the next attempt
    reconnects instead of retrying against a possibly-dead client.

    user/key_path default to settings.ssh_user/settings.ssh_key_path (same
    as execute_command()'s implicit behavior) but can be overridden per
    call.
    """
    resolved_user = settings.ssh_user if user is None else user
    resolved_key_path = settings.ssh_key_path if key_path is None else key_path

    def _attempt() -> str:
        client = _pool.get(host, resolved_user, resolved_key_path, timeout)
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
            output = stdout.read().decode(errors="replace")
            error_output = stderr.read().decode(errors="replace")
            exit_status = stdout.channel.recv_exit_status()
        except _RETRYABLE_EXCEPTIONS:
            # The pooled client may now be dead (broken pipe, transport
            # drop mid-command) -- drop it so the next retry attempt
            # reconnects instead of reusing a client that will just fail
            # the same way again.
            _pool.close(host)
            raise
        if exit_status != 0:
            raise ExecutorError(f"{host}: command exited {exit_status}: {error_output}")
        return output

    return _retry_ssh(host, _attempt, retries, delay, "execute_with_retry")


# execute_background() wraps every command as `setsid {command} & bg_pid=$!;
# echo SSHEXEC_PGID:$bg_pid; wait "$bg_pid"` -- this is the marker line that
# wrapper always emits FIRST, before any of the command's own output.
# BackgroundCommandHandle.read_new_output() strips it off (once) and caches
# the pgid so cancel_background() below can kill the whole remote process
# tree by process GROUP, not just the single top-level process.
_PGID_LINE_RE = re.compile(r"^SSHEXEC_PGID:(\d+)\n")


class BackgroundCommandHandle:
    """Opaque handle for a non-blocking background command, returned by
    execute_background(). Wraps the paramiko Channel so a caller can poll
    for completion and incrementally read stdout/stderr without blocking --
    for long-running commands a blocking call can't represent: background
    fio, a 72h soak test, per-OSD OMAP convert progress.

    If the underlying transport dies mid-poll (e.g. a node reboots during
    an upgrade test), the channel calls below can raise. That is treated as
    "connection lost -- nothing more is coming": is_done()/poll() return
    True, exit_code() returns None, and read_new_output() returns ("", "")
    rather than letting the exception surface to the caller.
    """

    def __init__(self, host: str, command: str, channel: "paramiko.Channel"):
        self.host = host
        self.command = command
        self.channel = channel
        # Set by read_new_output() the first time it sees output, off the
        # execute_background() wrapper's own SSHEXEC_PGID: marker line --
        # None until then, and permanently None if that first chunk of
        # output didn't start with the marker (should not happen for any
        # handle actually created by execute_background(), but
        # cancel_background() must still degrade gracefully rather than
        # kill the wrong process group).
        self.pgid: Optional[int] = None
        self._pgid_capture_attempted = False

    def is_done(self) -> bool:
        """True once the remote command has exited (or the connection to
        it has been lost)."""
        try:
            return self.channel.exit_status_ready()
        except (EOFError, OSError, paramiko.SSHException):
            return True

    def poll(self) -> bool:
        """Alias for is_done(), kept for readability at call sites."""
        return self.is_done()

    def read_new_output(self) -> Tuple[str, str]:
        """Drain any newly available stdout/stderr since the last call.
        Returns (new_stdout, new_stderr); either may be empty if nothing
        new is available yet, or if the connection has been lost.

        The very first non-empty stdout chunk is checked once for the
        execute_background() wrapper's own SSHEXEC_PGID:<n> marker line --
        if present, it's captured into self.pgid and stripped out (never
        surfacing to a caller's own output transcript); either way this
        check never runs again after the first attempt.
        """
        new_stdout = ""
        new_stderr = ""
        try:
            while self.channel.recv_ready():
                new_stdout += self.channel.recv(4096).decode(errors="replace")
            while self.channel.recv_stderr_ready():
                new_stderr += self.channel.recv_stderr(4096).decode(errors="replace")
        except (EOFError, OSError, paramiko.SSHException):
            return "", ""
        if not self._pgid_capture_attempted and new_stdout:
            self._pgid_capture_attempted = True
            match = _PGID_LINE_RE.match(new_stdout)
            if match:
                self.pgid = int(match.group(1))
                new_stdout = new_stdout[match.end() :]
        return new_stdout, new_stderr

    def exit_code(self) -> Optional[int]:
        """Exit code once the command is done, else None (including when
        the connection has been lost)."""
        try:
            if self.channel.exit_status_ready():
                return self.channel.recv_exit_status()
        except (EOFError, OSError, paramiko.SSHException):
            return None
        return None


def execute_background(
    host: str,
    command: str,
    user: Optional[str] = None,
    key_path: Optional[str] = None,
    timeout: int = CONNECT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRY_ATTEMPTS,
    delay: int = DEFAULT_RETRY_DELAY_SECONDS,
) -> BackgroundCommandHandle:
    """Kick off `command` on `host` over a pooled connection without
    blocking; returns a BackgroundCommandHandle the caller polls
    (is_done()/read_new_output()/exit_code()) to observe progress
    incrementally instead of blocking on the full command output the way
    execute_command()/execute_with_retry() do. Starting the command itself
    is retried the same way as execute_with_retry(); raises ExecutorError
    if every attempt to start it fails.

    2026-08-07: `command` is wrapped in `setsid ... & echo SSHEXEC_PGID:$!;
    wait "$bg_pid"` -- setsid makes the backgrounded process (and every `&`
    child ITS OWN script forks, e.g. a test case's own concurrent fio +
    while-true loops) the leader of a brand new process group, whose PGID
    equals its own PID. Echoing that PID as the very first line lets
    BackgroundCommandHandle.read_new_output() capture it (see
    handle.pgid), which cancel_background() below then uses to kill the
    ENTIRE remote tree in one shot -- before this, a caller had no way to
    stop one of these effectively-unbounded background loads short of an
    operator manually SSHing in and hunting down every process by hand.
    `wait "$bg_pid"` (not a bare `wait`) so the channel's own exit status
    still reflects `command`'s real exit code -- bash's no-argument `wait`
    is defined to always return 0, which would have silently broken every
    existing poll()'s nonzero-exit detection (check_background_handle_health()).
    """
    resolved_user = settings.ssh_user if user is None else user
    resolved_key_path = settings.ssh_key_path if key_path is None else key_path
    wrapped_command = f'setsid {command} & bg_pid=$!; echo SSHEXEC_PGID:$bg_pid; wait "$bg_pid"'

    def _attempt() -> "paramiko.Channel":
        client = _pool.get(host, resolved_user, resolved_key_path, timeout)
        try:
            transport = client.get_transport()
            channel = transport.open_session()
            channel.settimeout(COMMAND_TIMEOUT_SECONDS)
            channel.exec_command(wrapped_command)
            return channel
        except _RETRYABLE_EXCEPTIONS:
            _pool.close(host)
            raise

    channel = _retry_ssh(host, _attempt, retries, delay, "execute_background")
    return BackgroundCommandHandle(host, command, channel)


def cancel_background(handle: BackgroundCommandHandle) -> bool:
    """Best-effort kill of the ENTIRE remote process tree execute_background()
    started for `handle`, via the PGID its setsid wrapper captured (see
    BackgroundCommandHandle.read_new_output()) -- `kill -TERM -<pgid>`
    (a NEGATIVE pid) targets the whole process GROUP, reaching fio and
    every `&` while-loop a test case's own script forked internally, not
    just the single top-level process. SIGTERM first, then SIGKILL 1s
    later for whatever ignored it (fio does catch SIGTERM cleanly, but the
    plain `while true; do ...; done` loops these test cases also start
    don't trap signals at all and need the follow-up SIGKILL). Both kill
    invocations swallow their own exit status (`2>/dev/null; ...; true`)
    since a process already gone by the second kill is the expected case,
    not a failure.

    Returns True once the kill command was actually SENT (not proof the
    tree is dead, and not raised if the SSH round-trip itself fails --
    logged instead), False if no PGID was ever captured (handle.pgid is
    still None even after one last opportunistic drain attempt here, e.g.
    cancel() raced ahead of every poll() that would normally have captured
    it) -- callers must tell the operator a manual SSH kill may still be
    needed in that case, not silently report success.
    """
    if handle.pgid is None:
        handle.read_new_output()
    if handle.pgid is None:
        return False
    kill_cmd = f"kill -TERM -{handle.pgid} 2>/dev/null; sleep 1; kill -KILL -{handle.pgid} 2>/dev/null; true"
    try:
        execute_with_retry(handle.host, kill_cmd, retries=1)
    except ExecutorError:
        logger.warning(
            "cancel_background: kill command failed to reach %s for pgid %s",
            handle.host,
            handle.pgid,
            exc_info=True,
        )
        return False
    return True


def test_all_nodes(
    nodes: List[Union[str, Dict[str, Any]]],
    user: Optional[str] = None,
    key_path: Optional[str] = None,
    timeout: int = CONNECT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRY_ATTEMPTS,
    delay: int = DEFAULT_RETRY_DELAY_SECONDS,
) -> Dict[str, bool]:
    """Attempt a pooled connection to every host in `nodes`; never raises
    for a per-host connectivity failure -- callers use this to render a
    reachability table (e.g. a Test Runner pre-flight check), not to abort
    on the first bad node.

    `nodes` is a list of hostnames (using user/key_path, defaulting to
    settings.ssh_user/settings.ssh_key_path) or a list of dicts shaped like
    {"host": ..., "user": ..., "key_path": ..., "timeout": ...} to override
    per node.

    Malformed node entries are treated as a caller programming error, not a
    per-host connectivity failure, and raise ValueError immediately rather
    than an opaque KeyError/None-propagated-into-paramiko auth error:
    - a dict missing the required "host" key
    - a dict whose resolved user or key_path is missing/None

    Returns dict[host, bool]: True if the connect attempt (including its
    internal retries) succeeded, False otherwise.
    """
    default_user = settings.ssh_user if user is None else user
    default_key_path = settings.ssh_key_path if key_path is None else key_path

    results: Dict[str, bool] = {}
    for node in nodes:
        if isinstance(node, dict):
            if "host" not in node:
                raise ValueError(f"node dict missing required 'host' key: {node!r}")
            host = node["host"]
            node_user = node.get("user", default_user)
            node_key_path = node.get("key_path", default_key_path)
            if node_user is None or node_key_path is None:
                raise ValueError(f"node '{host}' is missing required 'user' or 'key_path': {node!r}")
            node_timeout = node.get("timeout", timeout)
        else:
            host = node
            node_user, node_key_path, node_timeout = default_user, default_key_path, timeout

        try:
            _retry_ssh(
                host,
                lambda h=host, u=node_user, k=node_key_path, t=node_timeout: _pool.get(h, u, k, t),
                retries,
                delay,
                "test_all_nodes connect",
            )
            results[host] = True
        except ExecutorError:
            results[host] = False
    return results


def close_pooled_connection(host: str) -> None:
    """Close and evict the pooled connection for host, if any. Only
    affects the execute_with_retry()/execute_background()/test_all_nodes()
    pool -- execute_command() never had a pooled connection to close."""
    _pool.close(host)


def close_all_pooled_connections() -> None:
    """Close and evict every pooled connection. Best-effort per host."""
    _pool.close_all()
