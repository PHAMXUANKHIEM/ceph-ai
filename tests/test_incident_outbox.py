import json
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db, incident_outbox
from shared.db import Base
from shared.models import Incident, IncidentOutbox, IncidentOutboxStatus, IncidentStatus
from shared.time import utc_now


def _isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False))


def _enqueue_one():
    with db.SessionLocal() as session:
        incident = Incident(
            ceph_code="MON_CLOCK_SKEW",
            status=IncidentStatus.NEW.value,
            detected_at=utc_now(),
        )
        session.add(incident)
        session.flush()
        event_id = incident_outbox.enqueue(
            session,
            incident_id=incident.id,
            payload={"incident_id": incident.id, "ceph_code": incident.ceph_code},
        )
        session.commit()
        return incident.id, event_id


def test_incident_and_outbox_are_durable_together(monkeypatch):
    _isolated_db(monkeypatch)
    incident_id, event_id = _enqueue_one()

    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        row = session.query(IncidentOutbox).one()
        assert row.event_id == event_id
        assert row.incident_id == incident.id
        assert json.loads(row.payload_json)["incident_id"] == incident_id
        assert row.status == IncidentOutboxStatus.PENDING.value


def test_dispatch_marks_sent_after_publish(monkeypatch):
    _isolated_db(monkeypatch)
    _enqueue_one()

    async def publish(payload):
        assert payload["ceph_code"] == "MON_CLOCK_SKEW"

    monkeypatch.setattr(incident_outbox.publisher, "publish_incident", publish)
    assert incident_outbox.dispatch_due(limit=1) == 1

    with db.SessionLocal() as session:
        assert session.query(IncidentOutbox.status).scalar() == IncidentOutboxStatus.SENT.value


def test_publish_failure_returns_row_to_retry_without_losing_incident(monkeypatch):
    _isolated_db(monkeypatch)
    incident_id, _ = _enqueue_one()

    async def publish(_payload):
        raise RuntimeError("rabbit password=secret unavailable")

    monkeypatch.setattr(incident_outbox.publisher, "publish_incident", publish)
    assert incident_outbox.dispatch_due(limit=1) == 1

    with db.SessionLocal() as session:
        row = session.query(IncidentOutbox).one()
        assert session.get(Incident, incident_id) is not None
        assert row.status == IncidentOutboxStatus.PENDING.value
        assert row.attempts == 1
        assert "secret" not in (row.last_error or "").lower()


def test_consumer_claim_is_idempotent(monkeypatch):
    _isolated_db(monkeypatch)
    _, event_id = _enqueue_one()

    state, token = incident_outbox.claim_consumer(event_id)
    assert state == "CLAIMED"
    assert token
    busy, no_token = incident_outbox.claim_consumer(event_id)
    assert busy == "BUSY"
    assert no_token is None

    incident_outbox.mark_consumer_done(event_id, token)
    done, no_token = incident_outbox.claim_consumer(event_id)
    assert done == incident_outbox.CONSUMER_DONE
    assert no_token is None


def test_reconciler_reports_new_incident_without_delivery(monkeypatch):
    _isolated_db(monkeypatch)
    with db.SessionLocal() as session:
        incident = Incident(
            ceph_code="OSD_DOWN",
            status=IncidentStatus.NEW.value,
            detected_at=utc_now() - timedelta(minutes=10),
            created_at=utc_now() - timedelta(minutes=10),
        )
        session.add(incident)
        session.commit()

    result = incident_outbox.reconcile_stale(max_age_seconds=60)
    assert result == {"missing_delivery": 1, "stale_pending": 0}
