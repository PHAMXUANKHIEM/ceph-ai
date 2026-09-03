import asyncio
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.scheduler as scheduler
from shared import db as db_module
from shared.db import Base
from shared.models import Action, ActionClassification, ActionStatus, Cluster, Incident


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


def test_create_backup_action_creates_incident_and_approved_safe_action(isolated_db):
    action_pk = scheduler._create_scheduled_action(
        "rbd_backup_run", {"pool": "vms", "image": "web01"}, "Backup RBD theo lịch cho vms/web01"
    )

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action is not None
        assert action.action_id == "rbd_backup_run"
        assert action.classification == ActionClassification.SAFE.value
        assert action.status == ActionStatus.APPROVED.value
        assert json.loads(action.action_params) == {"pool": "vms", "image": "web01"}
        # Real, non-empty single-host list — required by
        # _execute_approved_action's own validation gate (see this
        # function's docstring / dashboard/routes/volumes.py's 2026-07-28
        # fix comment).
        nodes = json.loads(action.target_nodes)
        assert isinstance(nodes, list) and len(nodes) == 1 and nodes[0]

        incident = session.get(Incident, action.incident_id)
        assert incident is not None
        assert incident.ceph_code == scheduler.BACKUP_SCHEDULED_CEPH_CODE


def test_trigger_backup_calls_execute_approved_action_with_new_action_pk(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "worker.llm.router_client._execute_approved_action", lambda action_pk: calls.append(action_pk)
    )

    asyncio.run(scheduler.trigger_backup("vms", "web01"))

    assert len(calls) == 1
    with db_module.SessionLocal() as session:
        action = session.get(Action, calls[0])
        assert action is not None
        assert action.action_id == "rbd_backup_run"


def test_trigger_backup_swallows_execution_errors(isolated_db, monkeypatch):
    """A scheduled tick failing must not crash the Scheduler coroutine
    (and therefore the whole asyncio.gather in worker/main.py) — same
    "never take the shared event loop down" posture as
    poll_approved_actions()'s own try/except."""

    def _boom(action_pk):
        raise RuntimeError("simulated dispatch failure")

    monkeypatch.setattr("worker.llm.router_client._execute_approved_action", _boom)

    asyncio.run(scheduler.trigger_backup("vms", "web01"))  # must not raise


def test_build_scheduler_registers_one_job_per_tracked_image(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [{"slot": "a", "immutable": False}],
        "tracked_images": [{"pool": "vms", "image": "web01"}, {"pool": "vms", "image": "web02"}],
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {"cron": {"hour": 3, "minute": 30}},
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)
    monkeypatch.setattr(scheduler, "_default_backup_target_ready", lambda _policy: True)

    built = scheduler.build_scheduler()

    job_ids = {job.id for job in built.get_jobs()}
    # backup_alert_check (Story 9.4) is always registered regardless of
    # policy content — assert the tracked-image jobs are a SUBSET, not
    # equality, so this test doesn't churn every time Story 9.4+ adds
    # another always-on job.
    assert {"rbd_backup_vms_web01", "rbd_backup_vms_web02"} <= job_ids


def test_create_metadata_backup_action_has_no_pool_image(isolated_db):
    action_pk = scheduler._create_scheduled_action(
        "backup_metadata_run", {}, "Backup metadata cụm theo lịch"
    )

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.action_id == "backup_metadata_run"
        assert action.classification == ActionClassification.SAFE.value
        assert action.status == ActionStatus.APPROVED.value
        assert json.loads(action.action_params) == {}


def test_trigger_metadata_backup_calls_execute_approved_action(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "worker.llm.router_client._execute_approved_action", lambda action_pk: calls.append(action_pk)
    )

    asyncio.run(scheduler.trigger_metadata_backup())

    assert len(calls) == 1
    with db_module.SessionLocal() as session:
        action = session.get(Action, calls[0])
        assert action.action_id == "backup_metadata_run"


def test_build_scheduler_registers_metadata_job_when_configured(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [{"slot": "a", "immutable": False}],
        "tracked_images": [],
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {"cron": {"hour": 2, "minute": 0}, "metadata_cron": {"hour": "*/6", "minute": 0}},
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)
    monkeypatch.setattr(scheduler, "_default_backup_target_ready", lambda _policy: True)

    built = scheduler.build_scheduler()

    job_ids = {job.id for job in built.get_jobs()}
    assert "backup_metadata_run" in job_ids


def test_build_scheduler_always_registers_alert_check(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [],
        "tracked_images": [],
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {},
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)

    built = scheduler.build_scheduler()

    job_ids = {job.id for job in built.get_jobs()}
    assert "backup_alert_check" in job_ids


def test_build_scheduler_skips_restore_drill_job_when_not_configured(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [],
        "tracked_images": [],
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {},
        "restore_drill": {"pool": "", "image": "", "scratch_pool": "", "scratch_image": ""},
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)

    built = scheduler.build_scheduler()

    job_ids = {job.id for job in built.get_jobs()}
    assert "restore_drill_execute" not in job_ids


def test_build_scheduler_registers_restore_drill_job_when_configured(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [],
        "tracked_images": [],
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {"restore_drill_cron": {"day_of_week": "mon", "hour": 3, "minute": 0}},
        "restore_drill": {
            "pool": "vms",
            "image": "web01",
            "scratch_pool": "scratch",
            "scratch_image": "drill01",
        },
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)
    monkeypatch.setattr(scheduler, "_default_backup_target_ready", lambda _policy: True)

    built = scheduler.build_scheduler()

    job_ids = {job.id for job in built.get_jobs()}
    assert "restore_drill_execute" in job_ids


def test_trigger_restore_drill_calls_execute_approved_action(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "worker.llm.router_client._execute_approved_action", lambda action_pk: calls.append(action_pk)
    )

    asyncio.run(scheduler.trigger_restore_drill())

    assert len(calls) == 1
    with db_module.SessionLocal() as session:
        action = session.get(Action, calls[0])
        assert action.action_id == "restore_drill_execute"


def _make_additional_cluster(**overrides) -> str:
    defaults = dict(
        name="cluster-b",
        ceph_mon_nodes="10.20.2.10",
        ssh_user="root",
        ssh_key_path="/root/.ssh/id_rsa",
        is_default=False,
        is_active=True,
        backup_enabled=True,
        backup_tracked_images="rbd/vm1,rbd/vm2",
        backup_transport="s3",
        backup_s3_endpoint="https://s3.example.test",
        backup_s3_access_key="access",
        backup_s3_secret_key="secret",
        backup_s3_bucket="cluster-b-backups",
    )
    defaults.update(overrides)
    with db_module.SessionLocal() as session:
        cluster = Cluster(**defaults)
        session.add(cluster)
        session.commit()
        return cluster.id


def test_build_scheduler_registers_jobs_for_backup_enabled_additional_cluster(isolated_db, monkeypatch):
    """Multi-tenant remediation Phase 3 — an ADDITIONAL cluster with
    backup_enabled=True gets its own rbd_backup_run/backup_metadata_run
    jobs registered ALONGSIDE (never replacing) the default cluster's
    own YAML-driven jobs, one per pool/image in its own
    backup_tracked_images."""
    policy = {
        "backup_targets": [{"slot": "a", "immutable": False}],
        "tracked_images": [{"pool": "vms", "image": "web01"}],
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {"cron": {"hour": 2, "minute": 0}, "metadata_cron": {"hour": "*/6", "minute": 0}},
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)
    monkeypatch.setattr(scheduler, "_default_backup_target_ready", lambda _policy: True)
    cluster_id = _make_additional_cluster()

    built = scheduler.build_scheduler()

    job_ids = {job.id for job in built.get_jobs()}
    assert "rbd_backup_vms_web01" in job_ids  # default cluster's own job, untouched
    assert f"rbd_backup_{cluster_id}_rbd_vm1" in job_ids
    assert f"rbd_backup_{cluster_id}_rbd_vm2" in job_ids
    assert f"backup_metadata_run_{cluster_id}" in job_ids
    assert f"backup_digest_run_{cluster_id}" in job_ids

    job = built.get_job(f"rbd_backup_{cluster_id}_rbd_vm1")
    assert tuple(job.args) == ("rbd", "vm1", cluster_id)


def test_build_scheduler_skips_disabled_additional_cluster(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [],
        "tracked_images": [],
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {},
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)
    cluster_id = _make_additional_cluster(backup_enabled=False)

    built = scheduler.build_scheduler()

    job_ids = {job.id for job in built.get_jobs()}
    assert not any(cluster_id in job_id for job_id in job_ids)


def test_build_scheduler_skips_backup_jobs_when_default_target_is_unconfigured(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [{"slot": "a", "immutable": False}],
        "tracked_images": [{"pool": "vms", "image": "web01"}],
        "schedule": {"metadata_cron": {"hour": "*/6", "minute": 0}},
        "restore_drill": {
            "pool": "vms", "image": "web01", "scratch_pool": "scratch", "scratch_image": "drill01"
        },
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)
    monkeypatch.setattr(scheduler, "_default_backup_target_ready", lambda _policy: False)

    job_ids = {job.id for job in scheduler.build_scheduler().get_jobs()}

    assert "rbd_backup_vms_web01" not in job_ids
    assert "backup_metadata_run" not in job_ids
    assert "restore_drill_execute" not in job_ids


def test_default_target_readiness_requires_policy_copy_count(monkeypatch):
    policy = {
        "backup_targets": [{"slot": "a", "immutable": False}, {"slot": "b", "immutable": True}],
        "required_copy_count": 2,
    }
    fields = {
        "backup_target_a_transport": "s3",
        "backup_target_a_s3_endpoint": "endpoint",
        "backup_target_a_s3_access_key": "access",
        "backup_target_a_s3_secret_key": "secret",
        "backup_target_a_s3_bucket": "bucket",
        "backup_target_b_transport": "",
    }
    for name, value in fields.items():
        monkeypatch.setattr(scheduler.settings, name, value, raising=False)

    assert scheduler._default_backup_target_ready(policy) is False

    monkeypatch.setattr(scheduler.settings, "backup_target_b_transport", "s3", raising=False)
    for suffix in ("endpoint", "access_key", "secret_key", "bucket"):
        monkeypatch.setattr(scheduler.settings, f"backup_target_b_s3_{suffix}", suffix, raising=False)
    assert scheduler._default_backup_target_ready(policy) is True


def test_build_scheduler_removes_stale_persisted_backup_jobs(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [{"slot": "a", "immutable": False}],
        "tracked_images": [],
        "required_copy_count": 1,
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {"metadata_cron": {"hour": "*/6", "minute": 0}},
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)
    monkeypatch.setattr(scheduler, "_default_backup_target_ready", lambda _policy: True)
    first = scheduler.build_scheduler()
    assert first.get_job("backup_metadata_run") is not None

    monkeypatch.setattr(scheduler, "_default_backup_target_ready", lambda _policy: False)
    second = scheduler.build_scheduler()

    assert second.get_job("backup_metadata_run") is None


def test_running_scheduler_removes_stale_persisted_backup_jobs(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [{"slot": "a", "immutable": False}],
        "tracked_images": [],
        "required_copy_count": 1,
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {"metadata_cron": {"hour": "*/6", "minute": 0}},
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)
    monkeypatch.setattr(scheduler, "_default_backup_target_ready", lambda _policy: True)
    async def _exercise():
        first = scheduler.build_scheduler()
        first.start()
        assert first.get_job("backup_metadata_run") is not None
        first.shutdown(wait=False)

        monkeypatch.setattr(scheduler, "_default_backup_target_ready", lambda _policy: False)
        second = scheduler.build_scheduler()
        second.start()
        try:
            scheduler._reconcile_backup_jobs(second, second._desired_backup_job_ids)
            assert second.get_job("backup_metadata_run") is None
        finally:
            second.shutdown(wait=False)

    asyncio.run(_exercise())


def test_create_scheduled_action_stamps_cluster_id(isolated_db):
    cluster_id = _make_additional_cluster()

    action_pk = scheduler._create_scheduled_action(
        "rbd_backup_run", {"pool": "rbd", "image": "vm1"}, "Backup RBD theo lịch cho rbd/vm1", cluster_id=cluster_id
    )

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, action.incident_id)
        assert incident.cluster_id == cluster_id


def test_trigger_backup_passes_cluster_id_through(isolated_db, monkeypatch):
    cluster_id = _make_additional_cluster()
    calls = []
    monkeypatch.setattr(
        "worker.llm.router_client._execute_approved_action", lambda action_pk: calls.append(action_pk)
    )

    asyncio.run(scheduler.trigger_backup("rbd", "vm1", cluster_id))

    with db_module.SessionLocal() as session:
        action = session.get(Action, calls[0])
        incident = session.get(Incident, action.incident_id)
        assert incident.cluster_id == cluster_id


def test_build_scheduler_always_registers_digest_job(isolated_db, monkeypatch):
    policy = {
        "backup_targets": [],
        "tracked_images": [],
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
        "schedule": {},
    }
    monkeypatch.setattr(scheduler, "load_backup_policy", lambda: policy)

    built = scheduler.build_scheduler()

    job_ids = {job.id for job in built.get_jobs()}
    assert "backup_digest_run" in job_ids
