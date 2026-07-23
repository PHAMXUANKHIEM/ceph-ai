import logging
import os

import paramiko

from config.settings import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
COMMAND_TIMEOUT_SECONDS = 30
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
