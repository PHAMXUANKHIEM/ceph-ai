"""SSHStorageBackend (Story 9.2, Architecture AD-9/AD-10).

Writes backup objects to a fixed landing directory on a site-2 host over
SFTP. Deliberately self-contained — does NOT import `worker/executor/
ssh_executor.py` (that module runs remote COMMANDS via `exec_command`,
buffering the whole stdout in memory; this backend transfers FILES via
SFTP, a different paramiko API with different memory characteristics for
large objects) — only the connection-setup *style* (host key handling,
`RejectPolicy`, `key_filename`) is mirrored, per the story's Dev Notes.

AD-10: this backend's credential must only ever be granted write access to
`landing_dir` on the destination host — never `chattr`/snapshot-management
rights there. Immutability for this backend is NOT enforced from here; it
depends on an out-of-band mechanism on the destination host itself (see
Architecture Deferred section). `apply_retention()`/`delete()` below are
plain SFTP removes — if the destination host has made objects
outside-of-band immutable (e.g. `chattr +i`), the SFTP remove will fail
and this backend correctly propagates that as an exception, never silently
swallowing it as success.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from datetime import datetime, timezone
from typing import BinaryIO

import paramiko

from worker.backup.storage.base import BackupObjectInfo, RetentionPolicyLike, UploadResult

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB — matches Story 9.1's chunk-count granularity
KNOWN_HOSTS_PATH = os.path.expanduser("~/.ssh/ceph_backup_known_hosts")


class SSHStorageBackendError(Exception):
    """Raised for any SSH/SFTP failure — connection, transfer, or a
    destination-side refusal (e.g. an immutable file rejecting delete).
    Deliberately a distinct type from `worker.executor.ssh_executor.
    ExecutorError` — these two modules are independent by design (see
    module docstring)."""


class SSHStorageBackend:
    def __init__(self, host: str, user: str, key_path: str, landing_dir: str):
        self._host = host
        self._user = user
        self._key_path = key_path
        self._landing_dir = landing_dir.rstrip("/")

    def _connect(self) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        client = paramiko.SSHClient()
        if os.path.exists(KNOWN_HOSTS_PATH):
            client.load_host_keys(KNOWN_HOSTS_PATH)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=self._host,
                username=self._user,
                key_filename=self._key_path,
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
            sftp = client.open_sftp()
        except Exception as exc:
            client.close()
            raise SSHStorageBackendError(f"{self._host}: failed to connect/open SFTP: {exc}") from exc
        return client, sftp

    def _remote_path(self, remote_key: str) -> str:
        if (
            not remote_key
            or remote_key.startswith("/")
            or "\\" in remote_key
            or any(part in ("", ".", "..") for part in remote_key.split("/"))
        ):
            raise SSHStorageBackendError(f"unsafe relative backup key: {remote_key!r}")
        return f"{self._landing_dir}/{remote_key}"

    def upload(self, stream: BinaryIO, remote_key: str) -> UploadResult:
        client, sftp = self._connect()
        final_path = self._remote_path(remote_key)
        part_path = f"{final_path}.part"
        try:
            self._ensure_parent_dir(sftp, final_path)
            digest = hashlib.sha256()
            size = 0
            with sftp.open(part_path, "wb") as remote_file:
                remote_file.set_pipelined(True)
                while True:
                    chunk = stream.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    remote_file.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            # Atomic-ish rename: a reader/verify() call never observes a
            # partially-written file under the FINAL key name — matches
            # Story 9.1's idempotency requirement (no partial file ever
            # looks valid).
            sftp.posix_rename(part_path, final_path)
            return UploadResult(key=remote_key, size=size, sha256=digest.hexdigest())
        except Exception as exc:
            try:
                sftp.remove(part_path)
            except Exception:
                pass
            raise SSHStorageBackendError(f"{self._host}: upload failed for {remote_key}: {exc}") from exc
        finally:
            sftp.close()
            client.close()

    def _ensure_parent_dir(self, sftp: paramiko.SFTPClient, remote_path: str) -> None:
        parent = remote_path.rsplit("/", 1)[0]
        parts = parent.split("/")
        current = ""
        for part in parts:
            if not part:
                continue
            current = f"{current}/{part}"
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current)

    def verify(self, remote_key: str, expected_size: int, expected_sha256: str) -> bool:
        client, sftp = self._connect()
        try:
            path = self._remote_path(remote_key)
            try:
                attrs = sftp.stat(path)
            except FileNotFoundError:
                return False
            if attrs.st_size != expected_size:
                return False
            digest = hashlib.sha256()
            with sftp.open(path, "rb") as remote_file:
                while True:
                    chunk = remote_file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest() == expected_sha256
        except Exception as exc:
            raise SSHStorageBackendError(f"{self._host}: verify failed for {remote_key}: {exc}") from exc
        finally:
            sftp.close()
            client.close()

    def list(self, prefix: str) -> list[BackupObjectInfo]:
        client, sftp = self._connect()
        try:
            dir_path = self._remote_path(prefix) if prefix else self._landing_dir
            try:
                entries = sftp.listdir_attr(dir_path)
            except FileNotFoundError:
                return []
            results = []
            for entry in entries:
                if stat.S_ISDIR(entry.st_mode or 0):
                    continue
                if entry.filename.endswith(".part"):
                    continue
                key = f"{prefix}/{entry.filename}" if prefix else entry.filename
                results.append(
                    BackupObjectInfo(
                        key=key,
                        size=entry.st_size or 0,
                        created_at=datetime.fromtimestamp(entry.st_mtime or 0, tz=timezone.utc),
                        sha256=None,
                    )
                )
            return results
        except Exception as exc:
            raise SSHStorageBackendError(f"{self._host}: list failed for prefix {prefix}: {exc}") from exc
        finally:
            sftp.close()
            client.close()

    def download(self, remote_key: str, dest: BinaryIO) -> None:
        client, sftp = self._connect()
        try:
            with sftp.open(self._remote_path(remote_key), "rb") as remote_file:
                while True:
                    chunk = remote_file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    dest.write(chunk)
        except Exception as exc:
            raise SSHStorageBackendError(f"{self._host}: download failed for {remote_key}: {exc}") from exc
        finally:
            sftp.close()
            client.close()

    def delete(self, remote_key: str) -> None:
        client, sftp = self._connect()
        try:
            sftp.remove(self._remote_path(remote_key))
        except Exception as exc:
            # Deliberately propagated, not swallowed — a destination-side
            # refusal (e.g. an out-of-band immutable file) must surface as
            # a real failure (AD-10 docstring above).
            raise SSHStorageBackendError(f"{self._host}: delete failed for {remote_key}: {exc}") from exc
        finally:
            sftp.close()
            client.close()

    def apply_retention(self, prefix: str, policy: RetentionPolicyLike) -> list[str]:
        objects = sorted(self.list(prefix), key=lambda o: o.created_at, reverse=True)
        deletable = [o for o in objects if o.key not in policy.protected_keys]
        keep_count = policy.keep_full_count + policy.keep_incremental_count
        to_delete = deletable[keep_count:]
        deleted_keys = []
        for obj in to_delete:
            self.delete(obj.key)
            deleted_keys.append(obj.key)
        return deleted_keys
