"""The ONE place that branches on `transport` (Architecture AD-9) — every
other backup code path calls only `BackupStorageBackend`'s Protocol
methods, never checks `transport` itself.
"""

from config.settings import Settings
from worker.backup.storage.base import BackupStorageBackend
from worker.backup.storage.s3_backend import S3StorageBackend
from worker.backup.storage.ssh_backend import SSHStorageBackend


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
