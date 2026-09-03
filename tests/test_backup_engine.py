import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.cluster_scope as cluster_scope
import worker.backup.engine as engine
import worker.backup.restore as restore
from shared import db as db_module
from shared.db import Base
from shared.models import Action, ActionStatus, BackupJob, Incident, IncidentStatus


@pytest.fixture()
def isolated_db(monkeypatch):
    """Same pattern as tests/test_router_client.py::isolated_db — a fresh
    in-memory DB per test, monkeypatched onto shared.db's module attributes
    so worker/backup/engine.py's `db.SessionLocal()` calls pick it up."""
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


def _make_incident_and_action(action_id: str = "rbd_backup_run", cluster_id: str | None = None) -> tuple[str, str]:
    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=cluster_id,
            ceph_code="BACKUP_SCHEDULED", status=IncidentStatus.EXECUTING.value, detected_at=datetime.utcnow()
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification="SAFE",
            status=ActionStatus.APPROVED.value,
            target_nodes=json.dumps(["10.20.1.112"]),
        )
        session.add(action)
        session.commit()
        return incident.id, action.id


def _make_additional_cluster(**overrides) -> str:
    from shared.models import Cluster

    defaults = dict(
        name="cluster-b",
        ceph_mon_nodes="10.20.2.10",
        ssh_user="root",
        ssh_key_path="/root/.ssh/id_rsa",
        is_default=False,
        is_active=True,
        backup_enabled=True,
        backup_tracked_images="vms/web01",
        backup_transport="s3",
        backup_s3_endpoint="https://s3.example.test",
        backup_s3_bucket="cluster-b-backups",
    )
    defaults.update(overrides)
    with db_module.SessionLocal() as session:
        cluster = Cluster(**defaults)
        session.add(cluster)
        session.commit()
        return cluster.id


class _FakeStdout:
    def __init__(self, payload: bytes, exit_status: int):
        self._data = payload
        self._pos = 0
        self.channel = SimpleNamespace(recv_exit_status=lambda: exit_status)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._pos :]
        else:
            chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


class _FakeStderr:
    def __init__(self, text: bytes = b""):
        self._text = text

    def read(self):
        return self._text


class _FakeStdin:
    def __init__(self, sink: bytearray):
        self._sink = sink

    def write(self, data):
        self._sink.extend(data)

    def close(self):
        pass


class FakeSSHClient:
    export_payload = b""
    exit_status = 0
    last_cmd = None
    # Story 9.7: per-command exit status/stderr overrides for `rbd
    # import`/`import-diff`-style commands (restore.py's _stream_file_to_rbd
    # writes to stdin instead of reading stdout) — keyed by exact command.
    exit_status_by_cmd: dict = {}
    stderr_by_cmd: dict = {}
    imported_calls: list = []  # [(cmd, bytes_written), ...] in call order

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
        FakeSSHClient.last_cmd = cmd
        sink = bytearray()
        FakeSSHClient.imported_calls.append([cmd, sink])
        stdin = _FakeStdin(sink)
        exit_status = FakeSSHClient.exit_status_by_cmd.get(cmd, FakeSSHClient.exit_status)
        stdout = _FakeStdout(FakeSSHClient.export_payload, exit_status)
        stderr = _FakeStderr(FakeSSHClient.stderr_by_cmd.get(cmd, b""))
        return stdin, stdout, stderr

    def close(self):
        pass


class FakeBackend:
    """Records every upload keyed by remote_key; verify() always succeeds
    unless `fail_verify` is set. Shared across both configured slots in
    these tests (same object returned for slot "a" and "b")."""

    def __init__(self):
        self.uploaded: dict[str, bytes] = {}
        self.fail_verify = False
        self.deleted: list[str] = []

    def upload(self, stream, remote_key):
        data = stream.read()
        self.uploaded[remote_key] = data
        sha256 = hashlib.sha256(data).hexdigest()
        return SimpleNamespace(key=remote_key, size=len(data), sha256=sha256)

    def verify(self, remote_key, expected_size, expected_sha256):
        if self.fail_verify:
            return False
        data = self.uploaded.get(remote_key)
        return data is not None and len(data) == expected_size

    def download(self, remote_key, dest):
        dest.write(self.uploaded[remote_key])

    def list(self, prefix):
        return [
            SimpleNamespace(key=k, size=len(v), created_at=datetime.utcnow())
            for k, v in self.uploaded.items()
            if k.startswith(prefix)
        ]

    def delete(self, remote_key):
        self.deleted.append(remote_key)
        self.uploaded.pop(remote_key, None)

    def apply_retention(self, prefix, policy):
        objects = sorted(self.list(prefix), key=lambda o: o.created_at, reverse=True)
        deletable = [o for o in objects if o.key not in policy.protected_keys]
        keep = policy.keep_full_count + policy.keep_incremental_count
        to_delete = deletable[keep:]
        for obj in to_delete:
            self.delete(obj.key)
        return [o.key for o in to_delete]


DEFAULT_POLICY = {
    "backup_targets": [{"slot": "a", "immutable": False}, {"slot": "b", "immutable": True}],
    "tracked_images": [{"pool": "vms", "image": "web01"}],
    "retention": {"keep_full_count": 2, "keep_incremental_count": 3},
}


@pytest.fixture(autouse=True)
def fake_backend_and_ssh(monkeypatch):
    FakeSSHClient.export_payload = b"fake rbd export bytes"
    FakeSSHClient.exit_status = 0
    FakeSSHClient.exit_status_by_cmd = {}
    FakeSSHClient.stderr_by_cmd = {}
    FakeSSHClient.imported_calls = []
    monkeypatch.setattr(engine.paramiko, "SSHClient", FakeSSHClient)
    monkeypatch.setattr(restore.paramiko, "SSHClient", FakeSSHClient)

    backend = FakeBackend()
    monkeypatch.setattr(engine, "get_backend", lambda slot, settings, immutable_enabled=False: backend)
    # worker/backup/cluster_scope.py::resolve_targets() is where engine.py's
    # upload/retention loops actually resolve backends now (multi-tenant
    # remediation Phase 3 consolidation) — patch it there too, not just on
    # `engine` itself.
    monkeypatch.setattr(cluster_scope, "get_backend", lambda slot, settings, immutable_enabled=False: backend)
    # Same reasoning, for an ADDITIONAL cluster's own single-target path
    # (worker/backup/storage/factory.py::get_backend_for_cluster).
    monkeypatch.setattr(cluster_scope, "get_backend_for_cluster", lambda cluster: backend)
    monkeypatch.setattr(engine, "load_backup_policy", lambda: DEFAULT_POLICY)
    monkeypatch.setattr(
        engine.settings, "ceph_mon_nodes", "10.20.1.112,10.20.1.95,10.20.1.21", raising=False
    )

    def fake_execute_command(host, cmd):
        if "rbd info" in cmd:
            return json.dumps({"size": len(FakeSSHClient.export_payload)})
        return ""

    monkeypatch.setattr(engine, "execute_command", fake_execute_command)
    yield backend


def _write_progress(action_pk, progress):
    pass


def _allow_execution(incident_id):
    return False


def _deny_execution(incident_id):
    return True


def test_first_backup_is_full_export(isolated_db):
    incident_id, action_pk = _make_incident_and_action()

    succeeded = engine.run(
        action_pk, "rbd_backup_run", {"pool": "vms", "image": "web01"}, incident_id, None, _write_progress, _allow_execution
    )

    assert succeeded is True
    with db_module.SessionLocal() as session:
        jobs = session.query(BackupJob).filter(BackupJob.pool == "vms", BackupJob.image == "web01").all()
    # one row per configured target slot (a, b)
    assert len(jobs) == 2
    assert all(j.job_type == "full" for j in jobs)
    assert all(j.status == "SUCCESS" for j in jobs)
    assert all(j.base_job_id is None for j in jobs)


def test_second_backup_is_incremental_and_links_base_job(isolated_db):
    incident_id, action_pk = _make_incident_and_action()
    engine.run(action_pk, "rbd_backup_run", {"pool": "vms", "image": "web01"}, incident_id, None, _write_progress, _allow_execution)

    with db_module.SessionLocal() as session:
        first_full_by_slot = {
            j.backup_target_slot: j.id
            for j in session.query(BackupJob)
            .filter(BackupJob.pool == "vms", BackupJob.job_type == "full")
            .all()
        }

    incident_id2, action_pk2 = _make_incident_and_action()
    succeeded = engine.run(
        action_pk2, "rbd_backup_run", {"pool": "vms", "image": "web01"}, incident_id2, None, _write_progress, _allow_execution
    )

    assert succeeded is True
    assert "export-diff" in FakeSSHClient.last_cmd
    assert "--from-snap" in FakeSSHClient.last_cmd
    with db_module.SessionLocal() as session:
        incrementals = (
            session.query(BackupJob)
            .filter(BackupJob.pool == "vms", BackupJob.job_type == "incremental")
            .all()
        )
    assert len(incrementals) == 2  # one per target slot
    # Each target's incremental must point to that target's full. A single
    # export-diff stream is valid for both only because both full rows refer
    # to the same Ceph snapshot, but the restore chain is target-specific.
    assert {j.backup_target_slot for j in incrementals} == {"a", "b"}
    assert {
        j.base_job_id == first_full_by_slot[j.backup_target_slot]
        for j in incrementals
    } == {True}


def test_idempotent_skip_when_fresh_running_job_exists(isolated_db):
    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="prior-run",
                pool="vms",
                image="web01",
                job_type="full",
                status="RUNNING",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    incident_id, action_pk = _make_incident_and_action()
    FakeSSHClient.last_cmd = None

    succeeded = engine.run(
        action_pk, "rbd_backup_run", {"pool": "vms", "image": "web01"}, incident_id, None, _write_progress, _allow_execution
    )

    assert succeeded is True  # benign skip, not a failure
    assert FakeSSHClient.last_cmd is None  # never attempted a new export
    with db_module.SessionLocal() as session:
        assert session.query(BackupJob).count() == 1  # no new row added


def test_stale_running_job_is_marked_failed_and_new_job_proceeds(isolated_db):
    stale_time = datetime.utcnow() - timedelta(seconds=engine.STALE_RUNNING_TIMEOUT_SECONDS + 60)
    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="stale-run", pool="vms", image="web01", job_type="full", status="RUNNING", created_at=stale_time
            )
        )
        session.commit()

    incident_id, action_pk = _make_incident_and_action()

    succeeded = engine.run(
        action_pk, "rbd_backup_run", {"pool": "vms", "image": "web01"}, incident_id, None, _write_progress, _allow_execution
    )

    assert succeeded is True
    with db_module.SessionLocal() as session:
        rows = session.query(BackupJob).filter(BackupJob.run_id == "stale-run").all()
        assert len(rows) == 1
        assert rows[0].status == "FAILED"
        new_rows = session.query(BackupJob).filter(BackupJob.status == "SUCCESS").all()
        assert len(new_rows) == 2  # one per target slot


def test_verify_failure_marks_job_failed(isolated_db, fake_backend_and_ssh):
    fake_backend_and_ssh.fail_verify = True
    incident_id, action_pk = _make_incident_and_action()

    succeeded = engine.run(
        action_pk, "rbd_backup_run", {"pool": "vms", "image": "web01"}, incident_id, None, _write_progress, _allow_execution
    )

    assert succeeded is False
    with db_module.SessionLocal() as session:
        jobs = session.query(BackupJob).filter(BackupJob.pool == "vms", BackupJob.image == "web01").all()
    assert len(jobs) == 1
    assert jobs[0].status == "FAILED"


def test_invalid_rbd_names_are_rejected_before_remote_commands(isolated_db, fake_backend_and_ssh):
    incident_id, action_pk = _make_incident_and_action()
    FakeSSHClient.last_cmd = None

    succeeded = engine.run(
        action_pk,
        "rbd_backup_run",
        {"pool": "vms; touch /tmp/pwned", "image": "web01"},
        incident_id,
        None,
        _write_progress,
        _allow_execution,
    )

    assert succeeded is False
    assert FakeSSHClient.last_cmd is None


def test_source_checksum_mismatch_marks_job_failed(isolated_db, fake_backend_and_ssh, monkeypatch):
    backend = fake_backend_and_ssh
    original_upload = backend.upload

    def upload_with_wrong_checksum(stream, remote_key):
        result = original_upload(stream, remote_key)
        result.sha256 = "0" * 64
        return result

    monkeypatch.setattr(backend, "upload", upload_with_wrong_checksum)
    incident_id, action_pk = _make_incident_and_action()

    succeeded = engine.run(
        action_pk,
        "rbd_backup_run",
        {"pool": "vms", "image": "web01"},
        incident_id,
        None,
        _write_progress,
        _allow_execution,
    )

    assert succeeded is False
    with db_module.SessionLocal() as session:
        jobs = session.query(BackupJob).filter(BackupJob.pool == "vms", BackupJob.image == "web01").all()
    assert len(jobs) == 1
    assert jobs[0].status == "FAILED"


def test_incremental_snapshot_is_removed_after_verified_backup(isolated_db, monkeypatch):
    commands = []

    def record_command(host, cmd):
        commands.append(cmd)
        if "rbd info" in cmd:
            return json.dumps({"size": len(FakeSSHClient.export_payload)})
        return ""

    monkeypatch.setattr(engine, "execute_command", record_command)
    incident_id, action_pk = _make_incident_and_action()
    assert engine.run(
        action_pk, "rbd_backup_run", {"pool": "vms", "image": "web01"},
        incident_id, None, _write_progress, _allow_execution,
    ) is True

    commands.clear()
    incident_id, action_pk = _make_incident_and_action()
    assert engine.run(
        action_pk, "rbd_backup_run", {"pool": "vms", "image": "web01"},
        incident_id, None, _write_progress, _allow_execution,
    ) is True

    assert any(cmd.startswith("rbd snap rm vms/web01@backup-") for cmd in commands)


def test_failed_backup_snapshot_is_removed(isolated_db, fake_backend_and_ssh, monkeypatch):
    commands = []

    def record_command(host, cmd):
        commands.append(cmd)
        if "rbd info" in cmd:
            return json.dumps({"size": len(FakeSSHClient.export_payload)})
        return ""

    monkeypatch.setattr(engine, "execute_command", record_command)
    fake_backend_and_ssh.fail_verify = True
    incident_id, action_pk = _make_incident_and_action()

    assert engine.run(
        action_pk, "rbd_backup_run", {"pool": "vms", "image": "web01"},
        incident_id, None, _write_progress, _allow_execution,
    ) is False

    assert any(cmd.startswith("rbd snap rm vms/web01@backup-") for cmd in commands)


def test_retention_never_deletes_full_still_depended_by_kept_incremental(isolated_db, fake_backend_and_ssh):
    """keep_full_count=2 in DEFAULT_POLICY — create 3 fulls (oldest would
    normally be pruned) but make the incremental chain depend on the
    OLDEST one, and confirm it survives retention."""
    backend = fake_backend_and_ssh
    now = datetime.utcnow()
    full_ids = []
    with db_module.SessionLocal() as session:
        for i in range(3):
            key = f"full/vms/web01/snap{i}.bin"
            backend.uploaded[key] = b"x"
            job = BackupJob(
                run_id=f"run-{i}",
                pool="vms",
                image="web01",
                job_type="full",
                status="SUCCESS",
                backup_target_slot="a",
                remote_key=key,
                created_at=now - timedelta(days=3 - i),
                finished_at=now - timedelta(days=3 - i),
            )
            session.add(job)
            session.flush()
            full_ids.append(job.id)
        # incremental depends on the OLDEST full (full_ids[0]), which would
        # otherwise fall outside keep_full_count=2 by recency alone
        session.add(
            BackupJob(
                run_id="run-inc",
                pool="vms",
                image="web01",
                job_type="incremental",
                status="SUCCESS",
                base_job_id=full_ids[0],
                backup_target_slot="a",
                remote_key="incremental/vms/web01/snap_inc.bin",
                created_at=now,
                finished_at=now,
            )
        )
        session.commit()

    incident_id, action_pk = _make_incident_and_action()
    engine._sweep_retention_after_success("vms", "web01", incident_id, action_pk)

    assert "full/vms/web01/snap0.bin" in backend.uploaded  # protected, survived


def _make_success_full_backup_job(backend, content: bytes = b"full backup content"):
    key = "full/vms/web01/backup-1.bin"
    backend.uploaded[key] = content
    with db_module.SessionLocal() as session:
        job = BackupJob(
            run_id="run-1",
            pool="vms",
            image="web01",
            job_type="full",
            status="SUCCESS",
            backup_target_slot="a",
            remote_key=key,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        session.add(job)
        session.commit()
        return job.id


def test_restore_to_production_succeeds_when_full_backup_exists(isolated_db, fake_backend_and_ssh):
    full_id = _make_success_full_backup_job(fake_backend_and_ssh)
    incident_id, action_pk = _make_incident_and_action(action_id="restore_rbd_image_to_production")

    succeeded = engine.run(
        action_pk,
        "restore_rbd_image_to_production",
        {"pool": "vms", "image": "web01", "recovery_point_job_id": full_id},
        incident_id,
        None,
        _write_progress,
        _allow_execution,
    )

    assert succeeded is True
    assert FakeSSHClient.imported_calls[0][0] == "rbd import - vms/web01"
    assert bytes(FakeSSHClient.imported_calls[0][1]) == b"full backup content"


def test_restore_as_new_uses_distinct_destination_and_verifies_it(isolated_db, fake_backend_and_ssh):
    full_id = _make_success_full_backup_job(fake_backend_and_ssh)
    incident_id, action_pk = _make_incident_and_action(action_id="restore_rbd_image_as_new")

    succeeded = engine.run(
        action_pk,
        "restore_rbd_image_as_new",
        {"pool": "vms", "image": "web01", "dest_pool": "recovery", "dest_image": "web01-restored",
         "recovery_point_job_id": full_id},
        incident_id,
        None,
        _write_progress,
        _allow_execution,
    )

    assert succeeded is True
    commands = [row[0] for row in FakeSSHClient.imported_calls]
    assert "rbd import - recovery/web01-restored" in commands
    assert "rbd info recovery/web01-restored --format json" in commands


def test_restore_as_new_rechecks_destination_and_fails_if_it_appeared(
    isolated_db, fake_backend_and_ssh, monkeypatch
):
    full_id = _make_success_full_backup_job(fake_backend_and_ssh)
    monkeypatch.setattr(engine.ceph_client, "query_rbd_inventory",
                        lambda pool: [{"name": "web01-restored"}])
    monkeypatch.setattr(engine.ceph_client, "query_rbd_pool_overview",
                        lambda pool: {"max_available": 10_000, "near_full": False})
    incident_id, action_pk = _make_incident_and_action(action_id="restore_rbd_image_as_new")
    writes = []

    succeeded = engine.run(action_pk, "restore_rbd_image_as_new", {
        "pool": "vms", "image": "web01", "dest_pool": "recovery",
        "dest_image": "web01-restored", "recovery_point_job_id": full_id,
        "preflight": {"required_bytes": 10},
    }, incident_id, None, lambda _pk, progress: writes.append(json.loads(json.dumps(progress))),
       _allow_execution)

    assert succeeded is False
    assert FakeSSHClient.imported_calls == []
    assert writes[-1][0]["status"] == "failed"
    assert "destination_exists" in writes[-1][0]["message"]


def test_restore_to_production_fails_when_no_full_backup_exists(isolated_db, fake_backend_and_ssh):
    incident_id, action_pk = _make_incident_and_action(action_id="restore_rbd_image_to_production")

    succeeded = engine.run(
        action_pk,
        "restore_rbd_image_to_production",
        {"pool": "vms", "image": "web01"},
        incident_id,
        None,
        _write_progress,
        _allow_execution,
    )

    assert succeeded is False
    assert FakeSSHClient.imported_calls == []


def test_restore_to_production_requires_approved_recovery_point(isolated_db, fake_backend_and_ssh):
    _make_success_full_backup_job(fake_backend_and_ssh)
    incident_id, action_pk = _make_incident_and_action(action_id="restore_rbd_image_to_production")

    succeeded = engine.run(
        action_pk,
        "restore_rbd_image_to_production",
        {"pool": "vms", "image": "web01"},
        incident_id,
        None,
        _write_progress,
        _allow_execution,
    )

    assert succeeded is False
    assert FakeSSHClient.imported_calls == []


def test_restore_to_production_fails_when_rbd_import_exits_nonzero(isolated_db, fake_backend_and_ssh):
    full_id = _make_success_full_backup_job(fake_backend_and_ssh)
    FakeSSHClient.exit_status_by_cmd["rbd import - vms/web01"] = 1
    FakeSSHClient.stderr_by_cmd["rbd import - vms/web01"] = b"no space left on device"
    incident_id, action_pk = _make_incident_and_action(action_id="restore_rbd_image_to_production")

    succeeded = engine.run(
        action_pk,
        "restore_rbd_image_to_production",
        {"pool": "vms", "image": "web01", "recovery_point_job_id": full_id},
        incident_id,
        None,
        _write_progress,
        _allow_execution,
    )

    assert succeeded is False


def test_restore_to_production_missing_pool_image_fails(isolated_db, fake_backend_and_ssh):
    incident_id, action_pk = _make_incident_and_action(action_id="restore_rbd_image_to_production")

    succeeded = engine.run(
        action_pk, "restore_rbd_image_to_production", {}, incident_id, None, _write_progress, _allow_execution
    )

    assert succeeded is False


def test_second_cluster_with_same_pool_image_gets_its_own_full_backup_not_an_incremental(
    isolated_db, fake_backend_and_ssh
):
    """Multi-tenant remediation Phase 3's core correctness requirement:
    two clusters backing up a SAME-NAMED (pool, image) must never see each
    other's BackupJob history. If cluster_id were missing from any of
    engine.py's queries, cluster B's first-ever backup here would be
    wrongly classified "incremental" against cluster A's full export."""
    cluster_b_id = _make_additional_cluster()

    incident_a, action_a = _make_incident_and_action(cluster_id=None)
    succeeded_a = engine.run(
        action_a, "rbd_backup_run", {"pool": "vms", "image": "web01"}, incident_a, None, _write_progress, _allow_execution
    )
    assert succeeded_a is True

    incident_b, action_b = _make_incident_and_action(cluster_id=cluster_b_id)
    succeeded_b = engine.run(
        action_b,
        "rbd_backup_run",
        {"pool": "vms", "image": "web01"},
        incident_b,
        cluster_b_id,
        _write_progress,
        _allow_execution,
    )
    assert succeeded_b is True
    # Not "export-diff" -- cluster B's own history for (vms, web01) is
    # empty, so this MUST be a full export, not an incremental against
    # cluster A's full backup.
    assert "rbd export vms/web01" in FakeSSHClient.last_cmd
    assert "export-diff" not in FakeSSHClient.last_cmd

    with db_module.SessionLocal() as session:
        cluster_a_jobs = (
            session.query(BackupJob)
            .filter(BackupJob.pool == "vms", BackupJob.image == "web01", BackupJob.cluster_id.is_(None))
            .all()
        )
        cluster_b_jobs = (
            session.query(BackupJob)
            .filter(BackupJob.pool == "vms", BackupJob.image == "web01", BackupJob.cluster_id == cluster_b_id)
            .all()
        )
    assert len(cluster_a_jobs) == 2  # DEFAULT_POLICY configures 2 slots (a, b) for the default cluster
    assert len(cluster_b_jobs) == 1  # an additional cluster gets exactly ONE target, no a/b pair
    assert all(j.job_type == "full" for j in cluster_a_jobs)
    assert all(j.job_type == "full" for j in cluster_b_jobs)
    assert cluster_b_jobs[0].backup_target_slot == "cluster"
