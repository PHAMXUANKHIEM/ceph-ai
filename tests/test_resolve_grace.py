from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.main as watcher_main
from config.settings import settings
from shared import audit
from shared import db as db_module
from shared.db import Base
from shared.models import AuditEntry, Incident, IncidentStatus
from watcher import resolve_grace

T0 = datetime(2026, 10, 3, 1, 46, 0)


@pytest.fixture(autouse=True)
def thirty_minute_grace(monkeypatch):
    monkeypatch.setattr(settings, "incident_resolve_grace_seconds", 1800)
    resolve_grace.reset()
    yield
    resolve_grace.reset()


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
    monkeypatch.setattr(watcher_main.settings, "telegram_ai_humanize_enabled", False)
    yield engine


def test_absent_check_is_held_until_the_grace_elapses():
    assert resolve_grace.decide("c1", "MON_DOWN", False, now=T0) == resolve_grace.HOLD
    assert resolve_grace.decide("c1", "MON_DOWN", False, now=T0 + timedelta(minutes=29)) == resolve_grace.HOLD
    assert resolve_grace.decide("c1", "MON_DOWN", False, now=T0 + timedelta(minutes=30)) == resolve_grace.RESOLVE


def test_check_returning_inside_the_grace_recurs_once_and_restarts_the_clock():
    resolve_grace.decide("c1", "MON_DOWN", False, now=T0)

    assert resolve_grace.decide("c1", "MON_DOWN", True, now=T0 + timedelta(minutes=10)) == resolve_grace.RECURRED
    assert resolve_grace.decide("c1", "MON_DOWN", True, now=T0 + timedelta(minutes=11)) == resolve_grace.PRESENT
    assert resolve_grace.decide("c1", "MON_DOWN", False, now=T0 + timedelta(minutes=12)) == resolve_grace.HOLD
    assert resolve_grace.decide("c1", "MON_DOWN", False, now=T0 + timedelta(minutes=41)) == resolve_grace.HOLD
    assert resolve_grace.decide("c1", "MON_DOWN", False, now=T0 + timedelta(minutes=42)) == resolve_grace.RESOLVE


def test_clusters_and_codes_keep_separate_clocks():
    resolve_grace.decide("c1", "MON_DOWN", False, now=T0)

    assert resolve_grace.decide("c2", "MON_DOWN", False, now=T0 + timedelta(minutes=30)) == resolve_grace.HOLD
    assert resolve_grace.decide("c1", "OSD_DOWN", False, now=T0 + timedelta(minutes=30)) == resolve_grace.HOLD
    assert resolve_grace.decide("c2", "MON_DOWN", True, now=T0 + timedelta(minutes=31)) == resolve_grace.RECURRED
    assert resolve_grace.decide("c1", "MON_DOWN", False, now=T0 + timedelta(minutes=31)) == resolve_grace.RESOLVE


def test_zero_grace_resolves_at_once(monkeypatch):
    monkeypatch.setattr(settings, "incident_resolve_grace_seconds", 0)

    assert resolve_grace.decide("c1", "MON_DOWN", False, now=T0) == resolve_grace.RESOLVE


def _open(code, cluster_id="c1"):
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=code, status=IncidentStatus.PENDING_APPROVAL.value,
            detected_at=T0, cluster_id=cluster_id,
        )
        session.add(incident)
        session.commit()
        return incident.id


def _status(incident_id):
    with db_module.SessionLocal() as session:
        return session.get(Incident, incident_id).status


def test_flapping_check_keeps_one_incident_and_audits_the_recurrence(isolated_db, monkeypatch):
    clock = {"now": T0}
    real_decide = resolve_grace.decide
    monkeypatch.setattr(
        resolve_grace, "decide",
        lambda cluster_id, code, present, now=None: real_decide(cluster_id, code, present, now=clock["now"]),
    )
    incident_id = _open("MON_DOWN")

    watcher_main._resolve_recovered_incidents(set(), cluster_id="c1", include_legacy_null=False)
    assert _status(incident_id) == IncidentStatus.PENDING_APPROVAL.value

    clock["now"] = T0 + timedelta(minutes=5)
    watcher_main._resolve_recovered_incidents({"MON_DOWN"}, cluster_id="c1", include_legacy_null=False)
    with db_module.SessionLocal() as session:
        recurred = session.query(AuditEntry).filter_by(event_type=audit.EVENT_INCIDENT_RECURRED).count()
    assert recurred == 1

    clock["now"] = T0 + timedelta(minutes=10)
    watcher_main._resolve_recovered_incidents(set(), cluster_id="c1", include_legacy_null=False)
    clock["now"] = T0 + timedelta(minutes=39)
    watcher_main._resolve_recovered_incidents(set(), cluster_id="c1", include_legacy_null=False)
    assert _status(incident_id) == IncidentStatus.PENDING_APPROVAL.value
    clock["now"] = T0 + timedelta(minutes=40)
    watcher_main._resolve_recovered_incidents(set(), cluster_id="c1", include_legacy_null=False)

    assert _status(incident_id) == IncidentStatus.RESOLVED.value


def test_other_clusters_incidents_are_untouched(isolated_db):
    other = _open("MON_DOWN", cluster_id="c2")

    for _ in range(3):
        watcher_main._resolve_recovered_incidents(set(), cluster_id="c1", include_legacy_null=False)

    assert _status(other) == IncidentStatus.PENDING_APPROVAL.value
