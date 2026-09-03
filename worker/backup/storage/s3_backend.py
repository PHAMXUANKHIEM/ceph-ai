"""S3StorageBackend (Story 9.2, Architecture AD-9/AD-10).

Writes backup objects to an S3-compatible bucket (real AWS S3 or MinIO —
`endpoint_url` blank means real AWS, set means a custom endpoint like
MinIO) via boto3. All SHA256 values crossing this Protocol are lowercase
hex strings (matching `hashlib.sha256().hexdigest()`), the same
representation `SSHStorageBackend` uses — never base64 — so callers never
need to know which backend they're talking to.

AD-10: Object Lock (`immutable_enabled=True`) can only take effect if the
destination bucket was created WITH Object Lock enabled — S3 does not
allow enabling it retroactively on an existing bucket. This backend does
NOT create buckets or enable Object Lock; it only sets per-object
`ObjectLockMode`/`ObjectLockRetainUntilDate` at upload time, which S3
itself will reject if the bucket isn't Object-Lock-enabled — that
rejection is propagated as `S3StorageBackendError`, never swallowed.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from typing import BinaryIO

import boto3
from botocore.exceptions import ClientError

from worker.backup.storage.base import BackupObjectInfo, RetentionPolicyLike, UploadResult

logger = logging.getLogger(__name__)

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
# Buffer uploads to a spooled temp file (spills to disk past this size)
# rather than holding the whole object in RAM — RBD full exports can be
# many GB; boto3's upload_fileobj() also uses this to drive its own
# multipart transfer internally for large objects.
SPOOL_MAX_BYTES = 64 * 1024 * 1024


class S3StorageBackendError(Exception):
    """Raised for any S3 failure — connection, transfer, or a destination-
    side refusal (e.g. Object Lock retention still in force on delete)."""


def _is_not_found(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    return code in ("404", "NoSuchKey", "NotFound")


class S3StorageBackend:
    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        immutable_enabled: bool = False,
        immutable_lock_days: int = 7,
    ):
        self._endpoint_url = endpoint_url or None
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._immutable_enabled = immutable_enabled
        self._immutable_lock_days = immutable_lock_days

    def _client(self):
        return boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )

    def upload(self, stream: BinaryIO, remote_key: str) -> UploadResult:
        digest = hashlib.sha256()
        size = 0
        try:
            with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES) as tmp:
                while True:
                    chunk = stream.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                tmp.seek(0)

                extra_args: dict = {"ChecksumAlgorithm": "SHA256"}
                if self._immutable_enabled:
                    retain_until = datetime.now(timezone.utc) + timedelta(days=self._immutable_lock_days)
                    extra_args["ObjectLockMode"] = "COMPLIANCE"
                    extra_args["ObjectLockRetainUntilDate"] = retain_until

                self._client().upload_fileobj(tmp, self._bucket, remote_key, ExtraArgs=extra_args)
            return UploadResult(key=remote_key, size=size, sha256=digest.hexdigest())
        except ClientError as exc:
            # Deliberately propagated (not swallowed) — e.g. bucket without
            # Object Lock enabled rejecting ObjectLockMode is a real
            # config error the operator must see (AD-10 module docstring).
            raise S3StorageBackendError(f"upload failed for {remote_key}: {exc}") from exc

    def verify(self, remote_key: str, expected_size: int, expected_sha256: str) -> bool:
        client = self._client()
        try:
            response = client.head_object(Bucket=self._bucket, Key=remote_key, ChecksumMode="ENABLED")
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise S3StorageBackendError(f"verify failed for {remote_key}: {exc}") from exc
        if response.get("ContentLength") != expected_size:
            return False
        checksum_b64 = response.get("ChecksumSHA256")
        if checksum_b64 is None:
            logger.warning(
                "S3StorageBackend.verify: bucket did not return ChecksumSHA256 for %s "
                "(bucket may not support S3 additional checksums) — downloading to verify independently",
                remote_key,
            )
            try:
                object_response = client.get_object(Bucket=self._bucket, Key=remote_key)
                body = object_response["Body"]
                digest = hashlib.sha256()
                actual_size = 0
                while True:
                    chunk = body.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    actual_size += len(chunk)
                return actual_size == expected_size and digest.hexdigest() == expected_sha256
            except ClientError as exc:
                if _is_not_found(exc):
                    return False
                raise S3StorageBackendError(f"verify download failed for {remote_key}: {exc}") from exc
            finally:
                if "body" in locals() and hasattr(body, "close"):
                    body.close()
        expected_b64 = base64.b64encode(bytes.fromhex(expected_sha256)).decode()
        return checksum_b64 == expected_b64

    def list(self, prefix: str) -> list[BackupObjectInfo]:
        try:
            paginator = self._client().get_paginator("list_objects_v2")
            results = []
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    last_modified = obj["LastModified"]
                    if last_modified.tzinfo is None:
                        last_modified = last_modified.replace(tzinfo=timezone.utc)
                    results.append(
                        BackupObjectInfo(
                            key=obj["Key"],
                            size=obj["Size"],
                            created_at=last_modified,
                            sha256=None,
                        )
                    )
            return results
        except ClientError as exc:
            raise S3StorageBackendError(f"list failed for prefix {prefix}: {exc}") from exc

    def download(self, remote_key: str, dest: BinaryIO) -> None:
        try:
            self._client().download_fileobj(self._bucket, remote_key, dest)
        except ClientError as exc:
            raise S3StorageBackendError(f"download failed for {remote_key}: {exc}") from exc

    def delete(self, remote_key: str) -> None:
        try:
            self._client().delete_object(Bucket=self._bucket, Key=remote_key)
        except ClientError as exc:
            # Deliberately propagated — e.g. Object Lock COMPLIANCE mode
            # still in force must surface as a real failure (AD-10).
            raise S3StorageBackendError(f"delete failed for {remote_key}: {exc}") from exc

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
