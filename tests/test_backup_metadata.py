import hashlib
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.engine as engine
import worker.backup.metadata as metadata
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


class FakeBackend:
    def __init__(self):
        self.uploaded: dict[str, bytes] = {}
        self.fail_verify = False

    def upload(self, stream, remote_key):
        data = stream.read()
        self.uploaded[remote_key] = data
        return SimpleNamespace(key=remote_key, size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def verify(self, remote_key, expected_size, expected_sha256):
        if self.fail_verify:
            return False
        data = self.uploaded.get(remote_key)
        return data is not None and len(data) == expected_size


DEFAULT_POLICY_TARGETS = [{"slot": "a", "immutable": False}, {"slot": "b", "immutable": True}]

FAKE_OUTPUTS = {
    "ceph mon getmap -o -": "fake-monmap-bytes",
    "ceph osd getmap -o -": "fake-osdmap-bytes",
    "ceph osd getcrushmap -o -": "fake-crushmap-bytes",
    "ceph auth export": "fake-auth-export-text",
    "ceph config dump": '{"fake": "config-dump"}',
}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(metadata, "get_backend", lambda slot, settings, immutable_enabled=False: backend)
    monkeypatch.setattr(metadata, "backup_targets_from_policy", lambda: DEFAULT_POLICY_TARGETS)
    monkeypatch.setattr(metadata.settings, "ceph_mon_nodes", "10.20.1.112,10.20.1.95,10.20.1.21", raising=False)

    def fake_execute_command(host, cmd):
        return FAKE_OUTPUTS[cmd]

    monkeypatch.setattr(metadata, "execute_command", fake_execute_command)
    yield backend


def _write_progress(action_pk, progress):
    pass


def _kill_switch_off(incident_id):
    return False


def _kill_switch_on(incident_id):
    return True


def test_metadata_backup_uploads_all_artifacts_and_verifies(isolated_db, fakes):
    succeeded = metadata.run("action-1", {}, "incident-1", _write_progress, _kill_switch_off)

    assert succeeded is True
    uploaded_names = {key.rsplit("/", 1)[-1] for key in fakes.uploaded}
    assert uploaded_names == {"monmap.bin", "osdmap.bin", "crushmap.bin", "auth_export.txt", "config_dump.json"}

    with db_module.SessionLocal() as session:
        jobs = session.query(BackupJob).filter(BackupJob.job_type == "metadata").all()
    assert len(jobs) == 2  # one per target slot
    assert all(j.status == "SUCCESS" for j in jobs)
    assert all(j.pool is None and j.image is None for j in jobs)


def test_kill_switch_blocks_before_any_command(isolated_db, fakes, monkeypatch):
    calls = []
    monkeypatch.setattr(metadata, "execute_command", lambda host, cmd: calls.append(cmd) or "")

    succeeded = metadata.run("action-1", {}, "incident-1", _write_progress, _kill_switch_on)

    assert succeeded is False
    assert calls == []
    with db_module.SessionLocal() as session:
        assert session.query(BackupJob).count() == 0


def test_command_failure_marks_job_failed(isolated_db, fakes, monkeypatch):
    def fake_execute_command(host, cmd):
        if cmd == "ceph auth export":
            raise RuntimeError("simulated SSH failure")
        return FAKE_OUTPUTS[cmd]

    monkeypatch.setattr(metadata, "execute_command", fake_execute_command)

    succeeded = metadata.run("action-1", {}, "incident-1", _write_progress, _kill_switch_off)

    assert succeeded is False
    with db_module.SessionLocal() as session:
        jobs = session.query(BackupJob).filter(BackupJob.job_type == "metadata").all()
    assert len(jobs) == 1
    assert jobs[0].status == "FAILED"
    assert "simulated SSH failure" in jobs[0].error_message


def test_verify_failure_marks_job_failed(isolated_db, fakes):
    fakes.fail_verify = True

    succeeded = metadata.run("action-1", {}, "incident-1", _write_progress, _kill_switch_off)

    assert succeeded is False
    with db_module.SessionLocal() as session:
        jobs = session.query(BackupJob).filter(BackupJob.job_type == "metadata").all()
    assert len(jobs) == 1
    assert jobs[0].status == "FAILED"


def test_no_backup_targets_configured_fails(isolated_db, fakes, monkeypatch):
    monkeypatch.setattr(metadata, "backup_targets_from_policy", lambda: [])

    succeeded = metadata.run("action-1", {}, "incident-1", _write_progress, _kill_switch_off)

    assert succeeded is False


def test_engine_dispatches_backup_metadata_run_to_metadata_module(monkeypatch):
    calls = []
    monkeypatch.setattr(
        engine.backup_metadata,
        "run",
        lambda action_pk, action_params, incident_id, write_progress, check_kill_switch: calls.append(
            action_pk
        )
        or True,
    )

    succeeded = engine.run("action-1", "backup_metadata_run", {}, "incident-1", _write_progress, _kill_switch_off)

    assert succeeded is True
    assert calls == ["action-1"]
