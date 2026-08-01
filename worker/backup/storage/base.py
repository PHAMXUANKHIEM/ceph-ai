"""BackupStorageBackend Protocol (Story 9.2, Architecture AD-9).

A single abstraction over "where a backup byte-stream ends up" so the
Backup Engine (Story 9.1), retention sweep, and RestoreDrill (Story 9.4)
never branch on transport type — they only ever call through this
Protocol. `SSHStorageBackend`/`S3StorageBackend` are the two v1
implementations (`ssh_backend.py`/`s3_backend.py`); `factory.py::get_backend()`
picks the binding at runtime from `BackupTarget.transport`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO, Protocol


@dataclass
class UploadResult:
    key: str
    size: int
    sha256: str


@dataclass
class BackupObjectInfo:
    key: str
    size: int
    created_at: datetime
    sha256: str | None = None


@dataclass
class RetentionPolicyLike:
    """Minimal shape `apply_retention()` needs — the real `RetentionPolicy`
    ORM model (Story 9.1, `shared/models.py`) satisfies this by having the
    same attribute names; kept as a separate dataclass here so
    `worker/backup/storage/` has no import dependency on `shared/models.py`
    (storage backends are pure I/O, no ORM)."""

    keep_full_count: int
    keep_incremental_count: int
    # Keys that a later object's lineage depends on (e.g. the full export an
    # incremental chain is based on) — apply_retention() must never delete
    # one of these even if it falls outside keep_full_count by age alone.
    protected_keys: frozenset[str] = field(default_factory=frozenset)


class BackupStorageBackend(Protocol):
    def upload(self, stream: BinaryIO, remote_key: str) -> UploadResult:
        """Stream `stream` to `remote_key` at the destination, returning the
        size and SHA256 actually written. Must not buffer the entire
        stream in memory — implementations read it in chunks."""
        ...

    def verify(self, remote_key: str, expected_size: int, expected_sha256: str) -> bool:
        """Confirm the object at `remote_key` matches `expected_size`/
        `expected_sha256` exactly. Returns False (never raises for a
        mismatch) so callers can decide how to react — only a transport/
        connection failure should raise."""
        ...

    def list(self, prefix: str) -> list[BackupObjectInfo]:
        """List objects under `prefix`, most information available without
        downloading content (size/created_at always populated; sha256 only
        if the backend can supply it cheaply — S3 ETag for non-multipart
        uploads, none for SSH unless a sidecar checksum file exists)."""
        ...

    def download(self, remote_key: str, dest: BinaryIO) -> None:
        """Write the object at `remote_key` into `dest`, streamed
        (implementations must not buffer the entire object in memory)."""
        ...

    def delete(self, remote_key: str) -> None:
        """Delete a single object. Must raise (not silently ignore) if the
        destination refuses the delete (e.g. an S3 Object Lock retention
        period still in force) — callers must see that failure, not a
        false "deleted"."""
        ...

    def apply_retention(self, prefix: str, policy: RetentionPolicyLike) -> list[str]:
        """Delete objects under `prefix` beyond `policy`'s keep counts,
        skipping anything in `policy.protected_keys`. `prefix` scopes the
        sweep to one image/target's own objects (e.g. "full/pool1/img1")
        — retention must never cross images, so there is no "sweep
        everything" mode. Returns the list of keys actually deleted (for
        AuditEntry logging by the caller — this Protocol itself never
        writes audit records, it has no `shared/` import)."""
        ...
