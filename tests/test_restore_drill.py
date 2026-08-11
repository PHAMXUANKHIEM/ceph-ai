import hashlib
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.restore_drill as restore_drill
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


BACKUP_CONTENT = b"original backup bytes for the restore drill"


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


class _FakeStdoutRead:
    def __init__(self, payload: bytes, exit_status: int):
        self._data = payload
        self._pos = 0
        self.channel = _FakeExitChannel(exit_status)

    def read(self, size=-1):
        chunk = self._data[self._pos : self._pos + size] if size and size > 0 else self._data[self._pos :]
        self._pos += len(chunk)
        return chunk


class _FakeStderr:
    def __init__(self, text: str = ""):
        self._text = text.encode()

    def read(self):
        return self._text


class FakeSSHClient:
    import_exit_status = 0
    import_stderr = ""
    export_payload = BACKUP_CONTENT  # what the re-export "returns" — controls checksum match/mismatch
    export_exit_status = 0
    export_stderr = ""
    imported_bytes: bytearray = bytearray()

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
        if cmd.startswith("rbd import"):
            FakeSSHClient.imported_bytes = bytearray()
            stdin = _FakeStdinCapture(FakeSSHClient.imported_bytes)
            stdout = _FakeStdoutRead(b"", FakeSSHClient.import_exit_status)
            stderr = _FakeStderr(FakeSSHClient.import_stderr)
            return stdin, stdout, stderr
        if cmd.startswith("rbd export"):
            stdout = _FakeStdoutRead(FakeSSHClient.export_payload, FakeSSHClient.export_exit_status)
            return None, stdout, _FakeStderr(FakeSSHClient.export_stderr)
        raise AssertionError(f"unexpected command: {cmd}")

    def close(self):
        pass


class FakeBackend:
    def __init__(self, content: bytes = BACKUP_CONTENT):
        self.content = content

    def download(self, remote_key, dest):
        dest.write(self.content)


DEFAULT_DRILL_POLICY = {
    "restore_drill": {"pool": "vms", "image": "web01", "scratch_pool": "scratch", "scratch_image": "drill01"}
}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeSSHClient.import_exit_status = 0
    FakeSSHClient.export_exit_status = 0
    FakeSSHClient.export_payload = BACKUP_CONTENT
    monkeypatch.setattr(restore_drill.paramiko, "SSHClient", FakeSSHClient)
    monkeypatch.setattr(restore_drill, "load_backup_policy", lambda: DEFAULT_DRILL_POLICY)
    monkeypatch.setattr(restore_drill.settings, "ceph_mon_nodes", "10.20.1.112,10.20.1.95,10.20.1.21", raising=False)
    monkeypatch.setattr(restore_drill, "get_backend", lambda slot, settings: FakeBackend())

    cleanup_calls = []
    monkeypatch.setattr(restore_drill, "execute_command", lambda host, cmd: cleanup_calls.append(cmd) or "")

    alerts = []
    monkeypatch.setattr(
        restore_drill.alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append((severity, message))
    )
    yield SimpleNamespace(cleanup_calls=cleanup_calls, alerts=alerts)


def _write_progress(action_pk, progress):
    pass


def _allow_execution(incident_id):
    return False


def _deny_execution(incident_id):
    return True


def _make_success_full_backup_job():
    with db_module.SessionLocal() as session:
        job = BackupJob(
            run_id="run-1",
            pool="vms",
            image="web01",
            job_type="full",
            status="SUCCESS",
            backup_target_slot="a",
            remote_key="full/vms/web01/backup-20260101T000000Z.bin",
            created_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        session.add(job)
        session.commit()
        return job.id


def test_restore_drill_succeeds_when_checksum_matches(isolated_db, fakes):
    _make_success_full_backup_job()

    succeeded = restore_drill.run("action-1", {}, "incident-1", _write_progress, _allow_execution)

    assert succeeded is True
    assert bytes(FakeSSHClient.imported_bytes) == BACKUP_CONTENT
    with db_module.SessionLocal() as session:
        drills = session.query(BackupJob).filter(BackupJob.job_type == "restore_drill").all()
    assert len(drills) == 1
    assert drills[0].status == "SUCCESS"
    assert drills[0].pool == "vms" and drills[0].image == "web01"
    # scratch image always cleaned up, success or failure
    assert any("rbd rm scratch/drill01" == c for c in fakes.cleanup_calls)
    assert fakes.alerts == []


def test_restore_drill_fails_on_checksum_mismatch_and_alerts_critical(isolated_db, fakes):
    _make_success_full_backup_job()
    FakeSSHClient.export_payload = b"corrupted, different bytes entirely"

    succeeded = restore_drill.run("action-1", {}, "incident-1", _write_progress, _allow_execution)

    assert succeeded is False
    with db_module.SessionLocal() as session:
        drills = session.query(BackupJob).filter(BackupJob.job_type == "restore_drill").all()
    assert len(drills) == 1
    assert drills[0].status == "FAILED"
    assert "checksum mismatch" in drills[0].error_message
    assert any("rbd rm scratch/drill01" == c for c in fakes.cleanup_calls)  # cleanup still happens
    assert len(fakes.alerts) == 1
    assert fakes.alerts[0][0] == "critical"


def test_restore_drill_fails_when_rbd_import_exits_nonzero(isolated_db, fakes):
    _make_success_full_backup_job()
    FakeSSHClient.import_exit_status = 1
    FakeSSHClient.import_stderr = "no space left on device"

    succeeded = restore_drill.run("action-1", {}, "incident-1", _write_progress, _allow_execution)

    assert succeeded is False
    with db_module.SessionLocal() as session:
        drills = session.query(BackupJob).filter(BackupJob.job_type == "restore_drill").all()
    assert drills[0].status == "FAILED"
    assert "no space left on device" in drills[0].error_message
    assert len(fakes.alerts) == 1
    assert fakes.alerts[0][0] == "critical"


def test_restore_drill_fails_when_no_successful_full_backup_exists(isolated_db, fakes):
    succeeded = restore_drill.run("action-1", {}, "incident-1", _write_progress, _allow_execution)

    assert succeeded is False
    with db_module.SessionLocal() as session:
        drills = session.query(BackupJob).filter(BackupJob.job_type == "restore_drill").all()
    assert len(drills) == 1
    assert drills[0].status == "FAILED"
    assert len(fakes.alerts) == 1
    assert fakes.alerts[0][0] == "critical"


def test_restore_drill_not_configured_returns_false(isolated_db, fakes, monkeypatch):
    monkeypatch.setattr(restore_drill, "load_backup_policy", lambda: {"restore_drill": {}})

    succeeded = restore_drill.run("action-1", {}, "incident-1", _write_progress, _allow_execution)

    assert succeeded is False
