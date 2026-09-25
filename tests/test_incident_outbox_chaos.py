"""Deterministic fault-injection tests for the Incident Outbox.

These tests deliberately use an isolated in-memory database and fake AMQP
objects. They exercise failure windows that cannot safely be created against
the Ceph production cluster. A separate marked integration test remains the
place for a real RabbitMQ/container restart rehearsal.
"""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db, incident_outbox
from shared.db import Base
from shared.models import Incident, IncidentOutbox, IncidentOutboxStatus, IncidentStatus
from shared.time import utc_now
from worker import main as worker_main


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


def _make_due(event_id):
    with db.SessionLocal() as session:
        row = session.query(IncidentOutbox).filter_by(event_id=event_id).one()
        row.next_attempt_at = utc_now() - timedelta(seconds=1)
        session.commit()


class _Message:
    def __init__(self, body, headers):
        self.body = body
        self.headers = headers
        self.acks = 0
        self.rejects = []

    async def ack(self):
        self.acks += 1

    async def reject(self, *, requeue):
        self.rejects.append(requeue)


class _Exchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, *, routing_key):
        self.published.append((message, routing_key))


class _Channel:
    def __init__(self):
        self.default_exchange = _Exchange()


def test_broker_failure_after_commit_retries_with_backoff_and_preserves_incident(monkeypatch):
    _isolated_db(monkeypatch)
    incident_id, event_id = _enqueue_one()
    calls = []

    async def publish(payload, *, event_id=None):
        calls.append(event_id)
        if len(calls) == 1:
            raise ConnectionError("broker unavailable")

    monkeypatch.setattr(incident_outbox.publisher, "publish_incident", publish)

    assert incident_outbox.dispatch_due(limit=1) == 1
    with db.SessionLocal() as session:
        failed_attempt = session.query(IncidentOutbox).one()
        assert failed_attempt.status == IncidentOutboxStatus.PENDING.value
        assert failed_attempt.attempts == 1
        assert failed_attempt.next_attempt_at > utc_now()
        assert session.get(Incident, incident_id) is not None

    _make_due(event_id)
    assert incident_outbox.dispatch_due(limit=1) == 1
    with db.SessionLocal() as session:
        row = session.query(IncidentOutbox).one()
        assert row.status == IncidentOutboxStatus.SENT.value
        assert row.attempts == 2
    assert calls == [event_id, event_id]


def test_publisher_crash_after_confirmation_reclaims_without_losing_event(monkeypatch):
    _isolated_db(monkeypatch)
    _, event_id = _enqueue_one()
    first = incident_outbox._claim_one()
    assert first is not None
    row_id, first_token, claimed_event_id, payload = first
    assert claimed_event_id == event_id

    published = []

    async def confirmed_publish(envelope, *, event_id=None):
        published.append((event_id, envelope))
        # Simulate process death after RabbitMQ confirms the persistent
        # message, before _mark_sent() can commit local state.

    monkeypatch.setattr(incident_outbox.publisher, "publish_incident", confirmed_publish)
    import asyncio

    asyncio.run(incident_outbox._publish(payload, event_id))
    with db.SessionLocal() as session:
        row = session.get(IncidentOutbox, row_id)
        row.claimed_at = utc_now() - timedelta(seconds=incident_outbox.CLAIM_LEASE_SECONDS + 1)
        session.commit()

    assert incident_outbox.dispatch_due(limit=1) == 1
    with db.SessionLocal() as session:
        row = session.get(IncidentOutbox, row_id)
        assert row.status == IncidentOutboxStatus.SENT.value
        assert row.attempts == 2
    assert first_token
    assert [event for event, _ in published] == [event_id, event_id]


def test_consumer_crash_after_side_effect_replays_once_with_event_idempotency(monkeypatch):
    _isolated_db(monkeypatch)
    _, event_id = _enqueue_one()

    state, first_token = incident_outbox.claim_consumer(event_id)
    assert state == "CLAIMED"

    side_effects = set()
    mutation_count = 0

    def apply_once(key):
        nonlocal mutation_count
        if key in side_effects:
            return
        side_effects.add(key)
        mutation_count += 1

    apply_once(event_id)
    with db.SessionLocal() as session:
        row = session.query(IncidentOutbox).filter_by(event_id=event_id).one()
        row.consumer_claimed_at = utc_now() - timedelta(
            seconds=incident_outbox.CONSUMER_CLAIM_LEASE_SECONDS + 1
        )
        session.commit()

    state, second_token = incident_outbox.claim_consumer(event_id)
    assert state == "CLAIMED"
    apply_once(event_id)
    incident_outbox.mark_consumer_done(event_id, second_token)
    incident_outbox.mark_consumer_done(event_id, first_token)

    final_state, no_token = incident_outbox.claim_consumer(event_id)
    assert final_state == incident_outbox.CONSUMER_DONE
    assert no_token is None
    assert mutation_count == 1


def test_consumer_terminal_failure_dead_letters_and_notifies_once(monkeypatch):
    import asyncio
    _isolated_db(monkeypatch)
    incident_id, event_id = _enqueue_one()
    notified = []
    monkeypatch.setattr(worker_main, "_notify_ai_diagnosis_failed", notified.append)

    async def process(_incident_id, _envelope):
        raise RuntimeError("simulated diagnosis failure")

    message = _Message(
        b'{"incident_id": "' + incident_id.encode() + b'"}',
        {"x-incident-event-id": event_id},
    )
    asyncio.run(worker_main._handle_message_without_context(
        message,
        _Channel(),
        process,
        max_retries=1,
        event_id=event_id,
    ))

    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        row = session.query(IncidentOutbox).filter_by(event_id=event_id).one()
        assert incident.status == IncidentStatus.FAILED.value
        assert row.consumer_status == incident_outbox.CONSUMER_PENDING
    assert message.acks == 0
    assert message.rejects == [False]
    assert notified == [incident_id]
