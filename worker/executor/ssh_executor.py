import logging
import os
from typing import Dict, Optional

import paramiko

from config.settings import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
# paramiko's exec_command timeout is a channel READ timeout (no data for
# this many seconds -> PipeTimeout), not a total-runtime cap — but a real
# `dnf/apt install ceph` can sit silent for minutes while downloading
# packages over the network.
COMMAND_TIMEOUT_SECONDS = 1800
KNOWN_HOSTS_PATH = os.path.expanduser("~/.ssh/ceph_lab_known_hosts")


class ExecutorError(Exception):
    """Raised for remediation-command connection or execution failures."""


def execute_command_bytes(
    host: str, command: str, user: Optional[str] = None, key_path: Optional[str] = None
) -> bytes:
    """Run a command over SSH and return stdout without text decoding.

    Ceph maps are binary.  Keeping this as a separate API prevents callers
    from accidentally round-tripping arbitrary bytes through UTF-8.
    """
    resolved_user = settings.ssh_user if user is None else user
    resolved_key_path = settings.ssh_key_path if key_path is None else key_path
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=host,
            username=resolved_user,
            key_filename=resolved_key_path,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        _stdin, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
        output = stdout.read()
        error_output = stderr.read().decode(errors="replace")
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


def execute_command(host: str, command: str, user: Optional[str] = None, key_path: Optional[str] = None) -> str:
    """Run a text command over SSH and return UTF-8 stdout."""
    output = execute_command_bytes(host, command, user=user, key_path=key_path)
    try:
        return output.decode()
    except UnicodeDecodeError as exc:
        raise ExecutorError(
            f"{host}: command returned binary/non-UTF-8 output; use execute_command_bytes()"
        ) from exc


def read_os_release(host: str, user: Optional[str] = None, key_path: Optional[str] = None) -> Dict[str, str]:
    """Read and parse /etc/os-release for upgrade compatibility checks."""
    output = execute_command(host, "cat /etc/os-release", user=user, key_path=key_path)
    fields: Dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"')
    return fields
