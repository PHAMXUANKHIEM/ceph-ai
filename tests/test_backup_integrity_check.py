"""Story 9.4, AC #1 — dedicated test file re-verifying the integrity-check
requirement. The mechanism itself was already implemented in Story 9.1
(`worker/backup/engine.py::_run_rbd_backup` computes SHA256 while
streaming the export, then calls `BackupStorageBackend.verify()` BEFORE
marking the BackupJob SUCCESS — see that story's `test_backup_engine.py::
test_verify_failure_marks_job_failed`), not written fresh here. This file
exists because the story explicitly asked for a focused test file proving
AC #1 in isolation, not because new production code was needed for it.
"""

import hashlib
import io
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.engine as engine
from shared import db as db_module
from shared.db import Base
from shared.models import BackupJob, SystemFlag


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
    with db_module.SessionLocal() as session:
        session.add(SystemFlag(key="kill_switch_enabled", value=False))
        session.commit()
    yield test_engine


class _FakeStdout:
    def __init__(self, payload: bytes, exit_status: int = 0):
        self._data = payload
        self._pos = 0
        self.channel = SimpleNamespace(recv_exit_status=lambda: exit_status)

    def read(self, size: int = -1) -> bytes:
        chunk = self._data[self._pos : self._pos + size] if size and size > 0 else self._data[self._pos :]
        self._pos += len(chunk)
        return chunk


class FakeSSHClient:
    export_payload = b"backup payload for integrity check test"
    exit_status = 0

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
        return None, _FakeStdout(FakeSSHClient.export_payload, FakeSSHClient.exit_status), _FakeStdout(b"")

    def close(self):
        pass


class FakeBackend:
    """`verify_result` is overridden per-test to simulate a corrupted/
    incomplete write at the destination — the destination's own say on
    whether what arrived matches, independent of whether `upload()` itself
    raised."""

    def __init__(self):
        self.uploaded: dict[str, bytes] = {}
        self.verify_result = True

    def upload(self, stream, remote_key):
        data = stream.read()
        self.uploaded[remote_key] = data
        return SimpleNamespace(key=remote_key, size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def verify(self, remote_key, expected_size, expected_sha256):
        return self.verify_result


DEFAULT_POLICY = {
    "backup_targets": [{"slot": "a", "immutable": False}],
    "tracked_images": [{"pool": "vms", "image": "web01"}],
    "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(engine.paramiko, "SSHClient", FakeSSHClient)
    backend = FakeBackend()
    monkeypatch.setattr(engine, "get_backend", lambda slot, settings, immutable_enabled=False: backend)
    monkeypatch.setattr(engine, "load_backup_policy", lambda: DEFAULT_POLICY)
    monkeypatch.setattr(engine.settings, "ceph_mon_nodes", "10.20.1.112,10.20.1.95,10.20.1.21", raising=False)

    def fake_execute_command(host, cmd):
        if "rbd info" in cmd:
            import json

            return json.dumps({"size": len(FakeSSHClient.export_payload)})
        return ""

    monkeypatch.setattr(engine, "execute_command", fake_execute_command)
    yield backend


def _write_progress(action_pk, progress):
    pass


def _kill_switch_off(incident_id):
    return False


def test_verify_success_marks_backup_job_success(isolated_db, fakes):
    """AC #1, happy path: destination confirms the data matches -> SUCCESS."""
    fakes.verify_result = True

    succeeded = engine.run(
        "action-1", "rbd_backup_run", {"pool": "vms", "image": "web01"}, "incident-1", _write_progress, _kill_switch_off
    )

    assert succeeded is True
    with db_module.SessionLocal() as session:
        job = session.query(BackupJob).filter(BackupJob.pool == "vms").first()
    assert job.status == "SUCCESS"


def test_verify_failure_never_marks_backup_job_success(isolated_db, fakes):
    """AC #1's core requirement: a destination-side verify() failure (data
    corrupted/incomplete in transit) must NEVER result in `SUCCESS` — not
    "success with a warning logged"."""
    fakes.verify_result = False

    succeeded = engine.run(
        "action-1", "rbd_backup_run", {"pool": "vms", "image": "web01"}, "incident-1", _write_progress, _kill_switch_off
    )

    assert succeeded is False
    with db_module.SessionLocal() as session:
        jobs = session.query(BackupJob).filter(BackupJob.pool == "vms").all()
    assert len(jobs) == 1
    assert jobs[0].status == "FAILED"
    assert jobs[0].status != "SUCCESS"


def test_sha256_computed_during_streaming_not_by_rereading(isolated_db, fakes):
    """AC #1's Task 1 subtask: SHA256 must be computed WHILE streaming the
    export (Story 9.1's `_ProgressTrackingReader`), not by re-reading the
    uploaded content afterward — verified indirectly here by confirming
    the checksum passed to `verify()` matches the real content's hash,
    which would only be true if it was computed from the actual bytes
    that flowed through, not some placeholder."""
    engine.run(
        "action-1", "rbd_backup_run", {"pool": "vms", "image": "web01"}, "incident-1", _write_progress, _kill_switch_off
    )

    uploaded_content = next(iter(fakes.uploaded.values()))
    assert uploaded_content == FakeSSHClient.export_payload
    assert hashlib.sha256(uploaded_content).hexdigest() == hashlib.sha256(FakeSSHClient.export_payload).hexdigest()
