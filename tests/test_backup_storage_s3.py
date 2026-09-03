import base64
import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

import worker.backup.storage.s3_backend as s3_backend
from worker.backup.storage.base import RetentionPolicyLike
from worker.backup.storage.s3_backend import S3StorageBackend, S3StorageBackendError


class _FakeObject:
    def __init__(self, body: bytes, checksum_sha256: str, retain_until=None):
        self.body = body
        self.checksum_sha256 = checksum_sha256
        self.retain_until = retain_until


class FakeS3Client:
    """In-memory stand-in for boto3's S3 client — no `moto` dependency
    added (not part of this story's approved dependency list); mirrors just
    the handful of calls S3StorageBackend makes."""

    def __init__(self):
        self.objects: dict[str, _FakeObject] = {}
        self.omit_checksums = False

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        data = fileobj.read()
        extra = ExtraArgs or {}
        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
        retain_until = extra.get("ObjectLockRetainUntilDate")
        self.objects[key] = _FakeObject(data, checksum, retain_until)

    def head_object(self, Bucket, Key, ChecksumMode=None):
        obj = self.objects.get(Key)
        if obj is None:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        response = {"ContentLength": len(obj.body)}
        if ChecksumMode == "ENABLED" and not self.omit_checksums:
            response["ChecksumSHA256"] = obj.checksum_sha256
        return response

    def get_object(self, Bucket, Key):
        obj = self.objects.get(Key)
        if obj is None:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "GetObject")

        class _Body:
            def __init__(self, data):
                self._stream = io.BytesIO(data)

            def read(self, size=-1):
                return self._stream.read(size)

            def close(self):
                self._stream.close()

        return {"Body": _Body(obj.body)}

    def get_paginator(self, operation_name):
        assert operation_name == "list_objects_v2"
        client = self

        class _Paginator:
            def paginate(self, Bucket, Prefix=""):
                contents = [
                    {
                        "Key": key,
                        "Size": len(obj.body),
                        "LastModified": datetime.now(timezone.utc) - timedelta(seconds=len(client.objects) - i),
                    }
                    for i, (key, obj) in enumerate(client.objects.items())
                    if key.startswith(Prefix)
                ]
                yield {"Contents": contents}

        return _Paginator()

    def download_fileobj(self, bucket, key, dest):
        obj = self.objects.get(key)
        if obj is None:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "GetObject")
        dest.write(obj.body)

    def delete_object(self, Bucket, Key):
        obj = self.objects.get(Key)
        if obj is None:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "DeleteObject")
        if obj.retain_until is not None and obj.retain_until > datetime.now(timezone.utc):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Object is locked (Object Lock retention)"}},
                "DeleteObject",
            )
        del self.objects[Key]


@pytest.fixture(autouse=True)
def fake_s3(monkeypatch):
    client = FakeS3Client()
    monkeypatch.setattr(s3_backend.boto3, "client", lambda *a, **kw: client)
    yield client


def _backend(immutable_enabled=False, immutable_lock_days=7):
    return S3StorageBackend(
        endpoint_url="http://minio.example:9000",
        access_key="fake-access",
        secret_key="fake-secret",
        bucket="ceph-backups",
        immutable_enabled=immutable_enabled,
        immutable_lock_days=immutable_lock_days,
    )


def test_upload_then_verify_roundtrip():
    backend = _backend()
    data = b"some backup bytes" * 100

    result = backend.upload(io.BytesIO(data), "full/img1.bin")

    assert result.size == len(data)
    assert backend.verify("full/img1.bin", result.size, result.sha256) is True


def test_verify_fails_on_size_mismatch():
    backend = _backend()
    backend.upload(io.BytesIO(b"abcdef"), "full/img2.bin")

    assert backend.verify("full/img2.bin", expected_size=999, expected_sha256="deadbeef") is False


def test_verify_missing_key_returns_false():
    backend = _backend()

    assert backend.verify("does/not/exist.bin", expected_size=1, expected_sha256="deadbeef") is False


def test_verify_without_s3_checksum_downloads_and_hashes_object(fake_s3):
    backend = _backend()
    data = b"checksum fallback bytes"
    result = backend.upload(io.BytesIO(data), "full/no-additional-checksum.bin")
    fake_s3.omit_checksums = True

    assert backend.verify("full/no-additional-checksum.bin", result.size, result.sha256) is True

    fake_s3.objects["full/no-additional-checksum.bin"].body = b"tampered same-ish payload"
    assert backend.verify("full/no-additional-checksum.bin", result.size, result.sha256) is False


def test_list_returns_uploaded_objects():
    backend = _backend()
    backend.upload(io.BytesIO(b"one"), "full/a.bin")
    backend.upload(io.BytesIO(b"two"), "full/b.bin")

    objects = backend.list("full")

    assert {o.key for o in objects} == {"full/a.bin", "full/b.bin"}


def test_delete_removes_object():
    backend = _backend()
    backend.upload(io.BytesIO(b"x"), "full/todelete.bin")

    backend.delete("full/todelete.bin")

    assert backend.list("full") == []


def test_delete_of_missing_key_raises_backend_error():
    backend = _backend()

    with pytest.raises(S3StorageBackendError):
        backend.delete("full/never-existed.bin")


def test_apply_retention_keeps_only_configured_count():
    backend = _backend()
    for i in range(5):
        backend.upload(io.BytesIO(f"data{i}".encode()), f"full/img_{i}.bin")

    policy = RetentionPolicyLike(keep_full_count=2, keep_incremental_count=0)
    deleted = backend.apply_retention("full", policy)

    remaining = {o.key for o in backend.list("full")}
    assert len(remaining) == 2
    assert len(deleted) == 3


def test_apply_retention_never_deletes_protected_keys():
    backend = _backend()
    for i in range(4):
        backend.upload(io.BytesIO(f"data{i}".encode()), f"full/img_{i}.bin")

    policy = RetentionPolicyLike(
        keep_full_count=1, keep_incremental_count=0, protected_keys=frozenset({"full/img_0.bin"})
    )
    backend.apply_retention("full", policy)

    remaining = {o.key for o in backend.list("full")}
    assert "full/img_0.bin" in remaining


def test_upload_with_immutable_enabled_sets_object_lock_and_blocks_delete():
    """AD-10: an Object-Lock-protected object must refuse delete while the
    retention period is in force — a real, propagated failure, not a
    silently-swallowed success."""
    backend = _backend(immutable_enabled=True, immutable_lock_days=7)
    backend.upload(io.BytesIO(b"locked data"), "full/locked.bin")

    with pytest.raises(S3StorageBackendError):
        backend.delete("full/locked.bin")

    # object must still be present — the failed delete must not have
    # partially removed it
    assert any(o.key == "full/locked.bin" for o in backend.list("full"))


def test_upload_without_immutable_allows_delete():
    backend = _backend(immutable_enabled=False)
    backend.upload(io.BytesIO(b"not locked"), "full/free.bin")

    backend.delete("full/free.bin")

    assert backend.list("full") == []
