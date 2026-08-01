from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.anomaly as anomaly
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


def _seed_history(pool, image, job_type, count, duration_seconds, size_bytes):
    with db_module.SessionLocal() as session:
        for i in range(count):
            session.add(
                BackupJob(
                    run_id=f"hist-{i}",
                    pool=pool,
                    image=image,
                    job_type=job_type,
                    status="SUCCESS",
                    duration_seconds=duration_seconds,
                    size_bytes=size_bytes,
                    created_at=datetime.utcnow() - timedelta(days=count - i),
                    finished_at=datetime.utcnow() - timedelta(days=count - i),
                )
            )
        session.commit()


def _make_job(pool, image, job_type, status, duration_seconds, size_bytes) -> BackupJob:
    with db_module.SessionLocal() as session:
        job = BackupJob(
            run_id="current",
            pool=pool,
            image=image,
            job_type=job_type,
            status=status,
            duration_seconds=duration_seconds,
            size_bytes=size_bytes,
            created_at=datetime.utcnow(),
        )
        session.add(job)
        session.commit()
        job_id = job.id
    with db_module.SessionLocal() as session:
        return session.get(BackupJob, job_id)


@pytest.fixture(autouse=True)
def fake_policy(monkeypatch):
    monkeypatch.setattr(anomaly, "load_backup_policy", lambda: {"anomaly_threshold_stddev": 3})


def test_no_anomaly_when_insufficient_history(isolated_db):
    _seed_history("vms", "web01", "full", 10, duration_seconds=100.0, size_bytes=1_000_000)
    job = _make_job("vms", "web01", "full", "SUCCESS", duration_seconds=100.0, size_bytes=1_000_000)

    assert anomaly.check_anomaly(job) is None


def test_no_anomaly_when_within_threshold(isolated_db):
    # Slight, realistic variance around a stable baseline — every history
    # value close to but not exactly 100.0, so pstdev is non-zero.
    with db_module.SessionLocal() as session:
        for i in range(30):
            session.add(
                BackupJob(
                    run_id=f"hist-{i}",
                    pool="vms",
                    image="web01",
                    job_type="full",
                    status="SUCCESS",
                    duration_seconds=98.0 + (i % 5),
                    size_bytes=1_000_000 + (i % 5) * 1000,
                    created_at=datetime.utcnow() - timedelta(days=30 - i),
                )
            )
        session.commit()
    job = _make_job("vms", "web01", "full", "SUCCESS", duration_seconds=101.0, size_bytes=1_002_000)

    assert anomaly.check_anomaly(job) is None


def test_detects_duration_anomaly(isolated_db):
    with db_module.SessionLocal() as session:
        for i in range(30):
            session.add(
                BackupJob(
                    run_id=f"hist-{i}",
                    pool="vms",
                    image="web01",
                    job_type="full",
                    status="SUCCESS",
                    duration_seconds=100.0 + (i % 3),  # low variance baseline
                    size_bytes=1_000_000,
                    created_at=datetime.utcnow() - timedelta(days=30 - i),
                )
            )
        session.commit()
    # wildly longer than baseline
    job = _make_job("vms", "web01", "full", "SUCCESS", duration_seconds=5000.0, size_bytes=1_000_000)

    result = anomaly.check_anomaly(job)

    assert result is not None
    assert result["kind"] == "duration"


def test_detects_size_anomaly(isolated_db):
    with db_module.SessionLocal() as session:
        for i in range(30):
            session.add(
                BackupJob(
                    run_id=f"hist-{i}",
                    pool="vms",
                    image="web01",
                    job_type="full",
                    status="SUCCESS",
                    duration_seconds=100.0,
                    size_bytes=1_000_000 + (i % 3) * 100,
                    created_at=datetime.utcnow() - timedelta(days=30 - i),
                )
            )
        session.commit()
    # size collapsed to almost nothing — classic "source data lost" signal
    job = _make_job("vms", "web01", "full", "SUCCESS", duration_seconds=100.0, size_bytes=10)

    result = anomaly.check_anomaly(job)

    assert result is not None
    assert result["kind"] == "size"


def test_no_anomaly_for_metadata_job(isolated_db):
    job = _make_job(None, None, "metadata", "SUCCESS", duration_seconds=5.0, size_bytes=1000)

    assert anomaly.check_anomaly(job) is None


def test_no_anomaly_for_failed_job(isolated_db):
    _seed_history("vms", "web01", "full", 30, duration_seconds=100.0, size_bytes=1_000_000)
    job = _make_job("vms", "web01", "full", "FAILED", duration_seconds=5000.0, size_bytes=10)

    assert anomaly.check_anomaly(job) is None


def test_threshold_is_configurable_not_hardcoded(isolated_db, monkeypatch):
    with db_module.SessionLocal() as session:
        for i in range(30):
            session.add(
                BackupJob(
                    run_id=f"hist-{i}",
                    pool="vms",
                    image="web01",
                    job_type="full",
                    status="SUCCESS",
                    duration_seconds=100.0 + (i % 3),
                    size_bytes=1_000_000,
                    created_at=datetime.utcnow() - timedelta(days=30 - i),
                )
            )
        session.commit()
    # A moderate deviation — not flagged with a loose threshold, flagged
    # with a strict one, proving the threshold is read from policy.
    job = _make_job("vms", "web01", "full", "SUCCESS", duration_seconds=110.0, size_bytes=1_000_000)

    monkeypatch.setattr(anomaly, "load_backup_policy", lambda: {"anomaly_threshold_stddev": 100})
    assert anomaly.check_anomaly(job) is None

    monkeypatch.setattr(anomaly, "load_backup_policy", lambda: {"anomaly_threshold_stddev": 0.001})
    assert anomaly.check_anomaly(job) is not None
