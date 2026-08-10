import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.database_capacity_monitor as dbcm
from config.settings import settings
from shared import db as db_module
from shared.db import Base
from shared.models import Action, Incident, IncidentStatus


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    yield engine


@pytest.fixture(autouse=True)
def clear_module_state():
    # dbcm._consecutive_high_scans is process-lifetime module state (by
    # design, same as osd_latency_monitor.py's/node_health_monitor.py's own
    # streak state) — would otherwise leak across unrelated test functions.
    dbcm._consecutive_high_scans = 0
    yield
    dbcm._consecutive_high_scans = 0


# --- _sqlite_file_path ---------------------------------------------------


def test_sqlite_file_path_relative():
    assert dbcm._sqlite_file_path("sqlite:///./ceph_aiops.db") == "./ceph_aiops.db"


def test_sqlite_file_path_absolute():
    assert dbcm._sqlite_file_path("sqlite:////var/lib/ceph_aiops.db") == "/var/lib/ceph_aiops.db"


def test_sqlite_file_path_memory_returns_none():
    assert dbcm._sqlite_file_path("sqlite:///:memory:") is None


def test_sqlite_file_path_non_sqlite_url_returns_none():
    assert dbcm._sqlite_file_path("postgresql://user:pw@host/db") is None


# --- get_database_size_bytes ----------------------------------------------


def test_get_database_size_bytes_sqlite_real_file(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_file.write_bytes(b"x" * 4096)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_file}")

    assert dbcm.get_database_size_bytes() == 4096


def test_get_database_size_bytes_sqlite_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/missing.db")

    assert dbcm.get_database_size_bytes() is None


def test_get_database_size_bytes_sqlite_memory_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite:///:memory:")

    assert dbcm.get_database_size_bytes() is None


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeSession:
    def __init__(self, value):
        self._value = value

    def execute(self, *_args, **_kwargs):
        return _FakeScalarResult(self._value)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_get_database_size_bytes_postgres_runs_pg_database_size(monkeypatch):
    # This is the OpenEverest/Postgres-on-Kubernetes path this feature was
    # actually built for — a real Postgres isn't available in this test
    # environment, so the session is faked; the important thing this
    # asserts is that the module runs `pg_database_size()` over the
    # EXISTING shared.db.SessionLocal connection rather than opening a new
    # one, and returns the scalar it gets back.
    monkeypatch.setattr(settings, "database_url", "postgresql://fake-host/ceph_aiops")
    monkeypatch.setattr(db_module, "SessionLocal", lambda: _FakeSession(123456789))

    assert dbcm.get_database_size_bytes() == 123456789


def test_get_database_size_bytes_postgres_query_failure_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://fake-host/ceph_aiops")

    def _raise(*_args, **_kwargs):
        raise SQLAlchemyError("connection to OpenEverest cluster failed")

    monkeypatch.setattr(db_module, "SessionLocal", _raise)

    assert dbcm.get_database_size_bytes() is None


# --- check_database_size (streak logic) -----------------------------------


def test_check_database_size_returns_empty_when_under_threshold(monkeypatch):
    monkeypatch.setattr(dbcm, "get_database_size_bytes", lambda: 1024)

    assert dbcm.check_database_size() == {}


def test_check_database_size_requires_consecutive_scans(monkeypatch):
    over_threshold = dbcm.DATABASE_SIZE_THRESHOLD_BYTES + 1
    monkeypatch.setattr(dbcm, "get_database_size_bytes", lambda: over_threshold)

    first = dbcm.check_database_size()
    assert first == {}  # 1st scan alone isn't enough (CONSECUTIVE_SCANS_REQUIRED=2)

    second = dbcm.check_database_size()
    assert dbcm.DATABASE_SIZE_HIGH_PREFIX in second
    assert second[dbcm.DATABASE_SIZE_HIGH_PREFIX]["consecutive_scans"] == 2
    assert second[dbcm.DATABASE_SIZE_HIGH_PREFIX]["size_bytes"] == over_threshold


def test_check_database_size_resets_streak_on_intervening_low_scan(monkeypatch):
    over_threshold = dbcm.DATABASE_SIZE_THRESHOLD_BYTES + 1
    under_threshold = 1024
    values = iter([over_threshold, under_threshold, over_threshold])
    monkeypatch.setattr(dbcm, "get_database_size_bytes", lambda: next(values))

    dbcm.check_database_size()  # streak -> 1
    dbcm.check_database_size()  # under threshold -> streak resets to 0
    result = dbcm.check_database_size()  # over threshold again -> streak 1, not yet 2

    assert result == {}


def test_check_database_size_unknown_scan_does_not_advance_or_reset_streak(monkeypatch):
    over_threshold = dbcm.DATABASE_SIZE_THRESHOLD_BYTES + 1
    monkeypatch.setattr(dbcm, "get_database_size_bytes", lambda: over_threshold)
    dbcm.check_database_size()  # streak -> 1

    monkeypatch.setattr(dbcm, "get_database_size_bytes", lambda: None)
    assert dbcm.check_database_size() == {}  # unknown this scan, streak untouched

    monkeypatch.setattr(dbcm, "get_database_size_bytes", lambda: over_threshold)
    result = dbcm.check_database_size()  # resumes at streak 2, not reset to 1

    assert dbcm.DATABASE_SIZE_HIGH_PREFIX in result


# --- create_or_resolve_database_size_incident -----------------------------


def _flagged_detail(size_gb=6):
    return {
        dbcm.DATABASE_SIZE_HIGH_PREFIX: {
            "size_bytes": size_gb * 1024**3,
            "threshold_bytes": dbcm.DATABASE_SIZE_THRESHOLD_BYTES,
            "consecutive_scans": 2,
        }
    }


def test_creates_incident_and_action_and_sends_alert_on_first_flag(isolated_db, monkeypatch):
    sent = []
    monkeypatch.setattr(dbcm, "send_database_size_alert", lambda message: sent.append(message))

    dbcm.create_or_resolve_database_size_incident(_flagged_detail())

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter(Incident.ceph_code == dbcm.DATABASE_SIZE_HIGH_PREFIX).one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        action = session.query(Action).filter(Action.incident_id == incident.id).one()
        assert action.action_id == "investigate_manually"
    assert len(sent) == 1


def test_does_not_duplicate_incident_or_resend_alert_while_still_flagged(isolated_db, monkeypatch):
    sent = []
    monkeypatch.setattr(dbcm, "send_database_size_alert", lambda message: sent.append(message))

    dbcm.create_or_resolve_database_size_incident(_flagged_detail())
    dbcm.create_or_resolve_database_size_incident(_flagged_detail())

    with db_module.SessionLocal() as session:
        count = session.query(Incident).filter(Incident.ceph_code == dbcm.DATABASE_SIZE_HIGH_PREFIX).count()
        assert count == 1
    assert len(sent) == 1


def test_resolves_without_alert_when_size_drops_out_of_current(isolated_db, monkeypatch):
    monkeypatch.setattr(dbcm, "send_database_size_alert", lambda message: None)

    dbcm.create_or_resolve_database_size_incident(_flagged_detail())
    dbcm.create_or_resolve_database_size_incident({})

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter(Incident.ceph_code == dbcm.DATABASE_SIZE_HIGH_PREFIX).one()
        assert incident.status == IncidentStatus.RESOLVED.value


def test_recreates_incident_if_still_flagged_after_admin_handled_it_manually(isolated_db, monkeypatch):
    sent = []
    monkeypatch.setattr(dbcm, "send_database_size_alert", lambda message: sent.append(message))

    dbcm.create_or_resolve_database_size_incident(_flagged_detail())
    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter(Incident.ceph_code == dbcm.DATABASE_SIZE_HIGH_PREFIX).one()
        incident.status = IncidentStatus.REJECTED.value
        session.commit()

    dbcm.create_or_resolve_database_size_incident(_flagged_detail())

    with db_module.SessionLocal() as session:
        count = session.query(Incident).filter(Incident.ceph_code == dbcm.DATABASE_SIZE_HIGH_PREFIX).count()
        assert count == 2
    assert len(sent) == 2
