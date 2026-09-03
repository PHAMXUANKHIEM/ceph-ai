import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.digest as digest
from shared import db as db_module
from shared.db import Base
from shared.models import BackupAnomaly, BackupDigestLog, BackupJob, Cluster


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


def test_gather_stats_counts_success_and_failed_in_period(isolated_db):
    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        session.add(BackupJob(run_id="1", pool="vms", image="web01", job_type="full", status="SUCCESS", created_at=now))
        session.add(BackupJob(run_id="2", pool="vms", image="web01", job_type="full", status="SUCCESS", created_at=now))
        session.add(BackupJob(run_id="3", pool="vms", image="web02", job_type="full", status="FAILED", created_at=now))
        session.commit()

    stats = digest._gather_stats(now - timedelta(hours=1), now + timedelta(hours=1))

    assert stats["succeeded_count"] == 2
    assert stats["failed_count"] == 1


def test_gather_stats_excludes_jobs_outside_period(isolated_db):
    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        session.add(BackupJob(run_id="old", pool="vms", image="web01", job_type="full", status="SUCCESS", created_at=now - timedelta(days=5)))
        session.commit()

    stats = digest._gather_stats(now - timedelta(hours=24), now)

    assert stats["succeeded_count"] == 0
    assert stats["failed_count"] == 0


def test_gather_stats_is_scoped_to_cluster(isolated_db):
    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        cluster_a = Cluster(name="a", ceph_mon_nodes="10.0.0.1", ssh_user="root", ssh_key_path="/tmp/a")
        cluster_b = Cluster(name="b", ceph_mon_nodes="10.0.0.2", ssh_user="root", ssh_key_path="/tmp/b")
        session.add_all([cluster_a, cluster_b])
        session.flush()
        session.add(BackupJob(run_id="a", cluster_id=cluster_a.id, job_type="full", status="SUCCESS", created_at=now))
        session.add(BackupJob(run_id="b", cluster_id=cluster_b.id, job_type="full", status="FAILED", created_at=now))
        cluster_a_id = cluster_a.id
        session.commit()

    stats = digest._gather_stats(now - timedelta(hours=1), now + timedelta(hours=1), cluster_id=cluster_a_id)

    assert stats["succeeded_count"] == 1
    assert stats["failed_count"] == 0


def test_gather_stats_includes_anomaly_count_and_details(isolated_db):
    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        job = BackupJob(run_id="1", pool="vms", image="web01", job_type="full", status="SUCCESS", created_at=now)
        session.add(job)
        session.flush()
        session.add(BackupAnomaly(backup_job_id=job.id, kind="duration", severity="warning", ai_summary="chạy lâu bất thường", created_at=now))
        session.commit()

    stats = digest._gather_stats(now - timedelta(hours=1), now + timedelta(hours=1))

    assert stats["anomaly_count"] == 1
    assert stats["anomaly_details"] == ["chạy lâu bất thường"]


def test_gather_stats_includes_latest_restore_drill(isolated_db):
    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(run_id="drill-1", pool="vms", image="web01", job_type="restore_drill", status="SUCCESS", created_at=now - timedelta(days=1))
        )
        session.add(
            BackupJob(run_id="drill-2", pool="vms", image="web01", job_type="restore_drill", status="FAILED", created_at=now)
        )
        session.commit()

    stats = digest._gather_stats(now - timedelta(hours=1), now + timedelta(hours=1))

    # latest drill by created_at, regardless of period bounds — an
    # operator wants to know the CURRENT restorability state, not just
    # drills that happened to run within this digest's window
    assert stats["latest_restore_drill_status"] == "FAILED"


def test_run_digest_persists_summary_and_sends_alert(isolated_db, monkeypatch):
    monkeypatch.setattr(digest, "load_backup_policy", lambda: {"schedule": {"digest": {"period_hours": 24}}})
    monkeypatch.setattr(digest.ai_analysis, "summarize_digest", lambda stats: _async_return("Mọi thứ ổn trong 24h qua."))
    alerts = []
    monkeypatch.setattr(digest.alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append((severity, message)))

    digest.run_digest()

    with db_module.SessionLocal() as session:
        logs = session.query(BackupDigestLog).all()
    assert len(logs) == 1
    assert logs[0].summary_text == "Mọi thứ ổn trong 24h qua."
    assert len(alerts) == 1
    assert alerts[0] == ("info", "Mọi thứ ổn trong 24h qua.")


def test_run_digest_falls_back_when_ai_fails(isolated_db, monkeypatch):
    monkeypatch.setattr(digest, "load_backup_policy", lambda: {"schedule": {"digest": {"period_hours": 24}}})

    def _boom(stats):
        return _async_raise(digest.ai_analysis.AIAnalysisError("router down"))

    monkeypatch.setattr(digest.ai_analysis, "summarize_digest", _boom)
    alerts = []
    monkeypatch.setattr(digest.alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append((severity, message)))

    digest.run_digest()  # must not raise

    with db_module.SessionLocal() as session:
        logs = session.query(BackupDigestLog).all()
    assert len(logs) == 1
    assert "job backup thành công" in logs[0].summary_text
    assert len(alerts) == 1


async def _async_return(value):
    return value


async def _async_raise(exc):
    raise exc
