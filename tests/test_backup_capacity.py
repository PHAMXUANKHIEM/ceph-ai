from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db as db_module
from shared.db import Base
from shared.models import BackupJob
from worker.backup import capacity


class _Backend:
    def probe_metadata(self):
        return {"capacity_bytes": 100_000, "free_bytes": 50_000, "endpoint": "test"}


def test_capacity_reports_growth_and_unknown_backend(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=test_engine, autoflush=False, autocommit=False))
    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        session.add_all([
            BackupJob(run_id="run-1", pool="rbd", image="web", job_type="full", status="SUCCESS",
                      backup_target_slot="a", size_bytes=10_000, created_at=now - timedelta(days=1)),
            BackupJob(run_id="run-2", pool="rbd", image="web", job_type="incremental", status="SUCCESS",
                      backup_target_slot="a", size_bytes=20_000, created_at=now),
        ])
        session.commit()
    monkeypatch.setattr(capacity, "resolve_targets", lambda _cluster: [("a", _Backend())])

    result = capacity.overview(None, now=now, window_days=10)
    target = result["targets"][0]
    assert target["recorded_bytes"] == 30_000
    assert target["daily_growth_bytes"] == 3_000
    assert target["days_until_full"] == 16.7
    assert target["status"] == "healthy"
