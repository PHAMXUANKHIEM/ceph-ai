"""Story 9.7, Task 1/5 — `worker/backup/restore.py::restore_image()`, the
single shared RBD restore-import path used by both the DR cluster-rebuild
phases (Task 2) and `restore_rbd_image_to_production` (Task 3)."""

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.restore as restore
from config.settings import settings
from shared import db as db_module
from shared.db import Base
from shared.models import BackupJob


@pytest.fixture()
def isolated_db(monkeypatch):
    test_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(test_engine, "connect", lambda dbapi_conn, _: dbapi_conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(test_engine)
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    )
    yield test_engine


FULL_CONTENT = b"full export bytes"
DIFF1_CONTENT = b"diff export #1"
DIFF2_CONTENT = b"diff export #2"


class _FakeStdinCapture:
    def __init__(self, sink: bytearray):
        self._sink = sink

    def write(self, data):
        self._sink.extend(data)

    def close(self):
        pass


class _FakeExitChannel:
    def __init__(self, exit_status):
        self._exit_status = exit_status

    def recv_exit_status(self):
        return self._exit_status


class _FakeStderr:
    def __init__(self, text: str = ""):
        self._text = text.encode()

    def read(self):
        return self._text


class FakeSSHClient:
    # cmd -> exit_status override, keyed by exact command string
    exit_status_by_cmd: dict = {}
    stderr_by_cmd: dict = {}
    imported_calls: list = []  # list of (cmd, bytes) in call order

    def __init__(self):
        pass

    def load_host_keys(self, path):
        pass

    def set_missing_host_key_policy(self, policy):
        pass

    def save_host_keys(self, path):
        pass

    def connect(self, hostname, username, key_filename, timeout):
        pass

    def exec_command(self, cmd):
        sink = bytearray()
        FakeSSHClient.imported_calls.append([cmd, sink])
        stdin = _FakeStdinCapture(sink)
        exit_status = FakeSSHClient.exit_status_by_cmd.get(cmd, 0)
        stdout = _FakeStdoutExit(exit_status)
        stderr = _FakeStderr(FakeSSHClient.stderr_by_cmd.get(cmd, ""))
        return stdin, stdout, stderr

    def close(self):
        pass


class _FakeStdoutExit:
    def __init__(self, exit_status):
        self.channel = _FakeExitChannel(exit_status)


class FakeStorageBackend:
    """In-memory backend keyed by remote_key. `verify()` independently
    recomputes the hash/size of what was originally `put()` — the
    "ground truth" server-side object — while `download()` normally
    serves that same content, unless `corrupt()` registers a DIFFERENT
    payload for a key (simulating a transfer that got corrupted in
    transit), so tests can distinguish "verify() is a real round-trip
    check" from "verify() trivially agrees with whatever download() just
    served"."""

    def __init__(self):
        self._objects: dict[str, bytes] = {}
        self._download_override: dict[str, bytes] = {}

    def put(self, remote_key: str, content: bytes) -> None:
        self._objects[remote_key] = content

    def corrupt(self, remote_key: str, corrupted_content: bytes) -> None:
        self._download_override[remote_key] = corrupted_content

    def download(self, remote_key, dest):
        dest.write(self._download_override.get(remote_key, self._objects[remote_key]))

    def verify(self, remote_key, expected_size, expected_sha256):
        content = self._objects.get(remote_key)
        if content is None:
            return False
        return len(content) == expected_size and hashlib.sha256(content).hexdigest() == expected_sha256


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeSSHClient.exit_status_by_cmd = {}
    FakeSSHClient.stderr_by_cmd = {}
    FakeSSHClient.imported_calls = []
    monkeypatch.setattr(restore.paramiko, "SSHClient", FakeSSHClient)
    # ceph_mon_nodes lives on the shared Settings singleton -- restore.py no
    # longer imports `settings` itself (multi-tenant remediation Phase 3:
    # mon-node resolution now goes through worker/backup/cluster_scope.py::
    # first_mon_node() -> shared/cluster_nodes.py::configured_nodes(), which
    # reads this SAME object).
    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112,10.20.1.95", raising=False)
    yield


def _make_full_job(pool="vms", image="web01", remote_key="full/vms/web01/backup-1.bin", created_at=None):
    with db_module.SessionLocal() as session:
        job = BackupJob(
            run_id="run-1",
            pool=pool,
            image=image,
            job_type="full",
            status="SUCCESS",
            backup_target_slot="a",
            remote_key=remote_key,
            created_at=created_at or datetime.utcnow(),
            finished_at=created_at or datetime.utcnow(),
        )
        session.add(job)
        session.commit()
        return job.id


def _make_diff_job(base_job_id, pool="vms", image="web01", remote_key="incremental/vms/web01/diff-1.bin", created_at=None, status="SUCCESS"):
    with db_module.SessionLocal() as session:
        job = BackupJob(
            run_id="run-1",
            pool=pool,
            image=image,
            job_type="incremental",
            status=status,
            backup_target_slot="a",
            base_job_id=base_job_id,
            remote_key=remote_key,
            created_at=created_at or datetime.utcnow(),
            finished_at=created_at or datetime.utcnow(),
        )
        session.add(job)
        session.commit()
        return job.id


def test_restore_image_applies_full_then_diffs_in_order(isolated_db):
    storage = FakeStorageBackend()
    storage.put("full/vms/web01/backup-1.bin", FULL_CONTENT)
    storage.put("incremental/vms/web01/diff-1.bin", DIFF1_CONTENT)
    storage.put("incremental/vms/web01/diff-2.bin", DIFF2_CONTENT)

    t0 = datetime.utcnow()
    full_id = _make_full_job(created_at=t0)
    # Registered out of order but must be applied oldest-first by created_at.
    _make_diff_job(
        full_id, remote_key="incremental/vms/web01/diff-2.bin", created_at=t0 + timedelta(minutes=20)
    )
    _make_diff_job(
        full_id, remote_key="incremental/vms/web01/diff-1.bin", created_at=t0 + timedelta(minutes=10)
    )

    result = restore.restore_image("vms", "web01", storage, "vms", "web01")

    assert result.success is True
    assert result.full_job_id == full_id
    assert len(result.applied_diff_job_ids) == 2
    assert result.size_bytes == len(FULL_CONTENT) + len(DIFF1_CONTENT) + len(DIFF2_CONTENT)

    commands = [cmd for cmd, _sink in FakeSSHClient.imported_calls]
    assert commands == [
        "rbd import - vms/web01",
        "rbd import-diff - vms/web01",
        "rbd import-diff - vms/web01",
        "rbd info vms/web01 --format json",
    ]
    payloads = [bytes(sink) for _cmd, sink in FakeSSHClient.imported_calls]
    assert payloads == [FULL_CONTENT, DIFF1_CONTENT, DIFF2_CONTENT, b""]


def test_restore_image_restores_into_a_different_dest(isolated_db):
    storage = FakeStorageBackend()
    storage.put("full/vms/web01/backup-1.bin", FULL_CONTENT)
    _make_full_job()

    result = restore.restore_image("vms", "web01", storage, "restored", "web01-copy")

    assert result.success is True
    assert FakeSSHClient.imported_calls[0][0] == "rbd import - restored/web01-copy"
    assert FakeSSHClient.imported_calls[-1][0] == "rbd info restored/web01-copy --format json"


def test_restore_as_new_cleans_partial_destination_when_verify_fails(isolated_db):
    storage = FakeStorageBackend()
    storage.put("full/vms/web01/backup-1.bin", FULL_CONTENT)
    _make_full_job()
    FakeSSHClient.exit_status_by_cmd["rbd info restored/web01-copy --format json"] = 1

    result = restore.restore_image(
        "vms", "web01", storage, "restored", "web01-copy",
        cleanup_new_destination_on_failure=True,
    )

    assert result.success is False
    assert FakeSSHClient.imported_calls[-1][0] == "rbd rm restored/web01-copy"


def test_restore_image_fails_when_no_successful_full_backup_exists(isolated_db):
    storage = FakeStorageBackend()

    result = restore.restore_image("vms", "web01", storage, "vms", "web01")

    assert result.success is False
    assert "No successful full backup" in result.error_message
    assert FakeSSHClient.imported_calls == []


def test_restore_image_fails_on_download_checksum_mismatch(isolated_db):
    storage = FakeStorageBackend()
    storage.put("full/vms/web01/backup-1.bin", FULL_CONTENT)
    _make_full_job()

    # Simulate a download that got corrupted in transit — verify() still
    # checks against the ORIGINAL uploaded bytes and must catch the mismatch.
    storage.corrupt("full/vms/web01/backup-1.bin", b"corrupted bytes, different length!!")

    result = restore.restore_image("vms", "web01", storage, "vms", "web01")

    assert result.success is False
    assert "checksum/size mismatch" in result.error_message
    assert FakeSSHClient.imported_calls == []


def test_restore_image_fails_when_rbd_import_diff_exits_nonzero(isolated_db):
    storage = FakeStorageBackend()
    storage.put("full/vms/web01/backup-1.bin", FULL_CONTENT)
    storage.put("incremental/vms/web01/diff-1.bin", DIFF1_CONTENT)
    full_id = _make_full_job()
    _make_diff_job(full_id)
    FakeSSHClient.exit_status_by_cmd["rbd import-diff - vms/web01"] = 1
    FakeSSHClient.stderr_by_cmd["rbd import-diff - vms/web01"] = "corrupt diff header"

    result = restore.restore_image("vms", "web01", storage, "vms", "web01")

    assert result.success is False
    assert result.full_job_id == full_id
    assert result.applied_diff_job_ids == []  # failed before this diff was recorded as applied
    assert "corrupt diff header" in result.error_message


def test_restore_image_ignores_failed_incremental_jobs(isolated_db):
    storage = FakeStorageBackend()
    storage.put("full/vms/web01/backup-1.bin", FULL_CONTENT)
    storage.put("incremental/vms/web01/diff-bad.bin", b"never used")
    full_id = _make_full_job()
    _make_diff_job(full_id, remote_key="incremental/vms/web01/diff-bad.bin", status="FAILED")

    result = restore.restore_image("vms", "web01", storage, "vms", "web01")

    assert result.success is True
    assert result.applied_diff_job_ids == []
    commands = [cmd for cmd, _sink in FakeSSHClient.imported_calls]
    assert commands == ["rbd import - vms/web01", "rbd info vms/web01 --format json"]
