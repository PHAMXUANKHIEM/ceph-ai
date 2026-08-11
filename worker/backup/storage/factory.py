"""The ONE place that branches on `transport` (Architecture AD-9) — every
other backup code path calls only `BackupStorageBackend`'s Protocol
methods, never checks `transport` itself.
"""

from typing import TYPE_CHECKING

from config.settings import Settings
from worker.backup.storage.base import BackupStorageBackend
from worker.backup.storage.s3_backend import S3StorageBackend
from worker.backup.storage.ssh_backend import SSHStorageBackend

if TYPE_CHECKING:
    from shared.models import Cluster


class BackupTargetNotConfiguredError(Exception):
    """Raised when the requested slot ("a"/"b") has no transport configured
    (blank `backup_target_<slot>_transport`) or an unrecognized one."""


def get_backend(slot: str, settings: Settings, immutable_enabled: bool = False) -> BackupStorageBackend:
    """`immutable_enabled` is caller-supplied, not read from `settings` —
    WHICH slot is the designated immutable copy is a `backup_policy.yaml`
    decision (Story 9.1's engine reads it from there), not something this
    factory decides on its own. Defaulting to False is deliberate: silence
    on the caller's part must never accidentally enable Object Lock
    against a bucket that isn't provisioned for it (AD-10)."""
    transport = getattr(settings, f"backup_target_{slot}_transport", "")
    if transport == "ssh":
        return SSHStorageBackend(
            host=getattr(settings, f"backup_target_{slot}_ssh_host"),
            user=getattr(settings, f"backup_target_{slot}_ssh_user"),
            key_path=getattr(settings, f"backup_target_{slot}_ssh_key_path"),
            landing_dir=getattr(settings, f"backup_target_{slot}_ssh_landing_dir"),
        )
    if transport == "s3":
        return S3StorageBackend(
            endpoint_url=getattr(settings, f"backup_target_{slot}_s3_endpoint"),
            access_key=getattr(settings, f"backup_target_{slot}_s3_access_key"),
            secret_key=getattr(settings, f"backup_target_{slot}_s3_secret_key"),
            bucket=getattr(settings, f"backup_target_{slot}_s3_bucket"),
            immutable_enabled=immutable_enabled,
            immutable_lock_days=getattr(settings, f"backup_target_{slot}_immutable_lock_days"),
        )
    raise BackupTargetNotConfiguredError(
        f"backup_target_{slot}_transport={transport!r} — expected 'ssh' or 's3', slot not configured"
    )


def get_backend_for_cluster(cluster: "Cluster") -> BackupStorageBackend:
    """`get_backend()`'s sibling for an ADDITIONAL cluster (multi-tenant
    remediation Phase 3) — reads `cluster.backup_*` instead of `settings.
    backup_target_<slot>_*`. Only ONE target per cluster (no a/b pair —
    see `shared/models.py::Cluster`'s own docstring), so there's no `slot`
    parameter to branch on; `cluster.backup_immutable_enabled`/
    `backup_immutable_lock_days` stand in for the caller-supplied
    `immutable_enabled`/slot-policy split `get_backend()` above has.
    Raises the SAME `BackupTargetNotConfiguredError` for a blank/
    unrecognized `backup_transport` — callers must not silently skip an
    enabled cluster with a half-configured target."""
    transport = cluster.backup_transport
    if transport == "ssh":
        return SSHStorageBackend(
            host=cluster.backup_ssh_host,
            user=cluster.backup_ssh_user,
            key_path=cluster.backup_ssh_key_path,
            landing_dir=cluster.backup_ssh_landing_dir,
        )
    if transport == "s3":
        return S3StorageBackend(
            endpoint_url=cluster.backup_s3_endpoint,
            access_key=cluster.backup_s3_access_key,
            secret_key=cluster.backup_s3_secret_key,
            bucket=cluster.backup_s3_bucket,
            immutable_enabled=cluster.backup_immutable_enabled,
            immutable_lock_days=cluster.backup_immutable_lock_days,
        )
    raise BackupTargetNotConfiguredError(
        f"cluster {cluster.id} backup_transport={transport!r} — expected 'ssh' or 's3', not configured"
    )
