from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import incident_outbox, reliability
from shared.db import Base
from shared.models import Incident, IncidentOutbox, IncidentStatus
from shared.time import utc_now


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _row(session, index, *, status, attempts=0, created_ago=0, next_in=0, claimed_ago=None):
    now = utc_now()
    incident = Incident(ceph_code=f"TEST_CODE_{index}", status=IncidentStatus.NEW.value, detected_at=now)
    session.add(incident)
    session.flush()
    session.add(IncidentOutbox(
        event_id=f"incident:{index}",
        incident_id=incident.id,
        payload_json="{}",
        payload_hash="0" * 64,
        status=status,
        attempts=attempts,
        created_at=now - timedelta(seconds=created_ago),
        next_attempt_at=now + timedelta(seconds=next_in),
        claimed_at=None if claimed_ago is None else now - timedelta(seconds=claimed_ago),
    ))
    session.flush()


def _outbox(session):
    return reliability._outbox_row(
        session, IncidentOutbox, name="incident_outbox",
        attempt_limit=incident_outbox.MAX_ATTEMPTS, lease_seconds=incident_outbox.CLAIM_LEASE_SECONDS,
    )


def _codes(queue):
    return {alert["code"] for alert in reliability._alerts([], {}, [queue], {}, [], {})}


def test_healthy_outbox_raises_no_alert():
    session = _session()
    _row(session, 1, status="PENDING")
    _row(session, 2, status="SENT", attempts=1, created_ago=10_000)
    queue = _outbox(session)
    assert queue["pending_total"] == 1
    assert queue["dead_total"] == 0
    assert queue["max_attempts"] == 0
    assert queue["attempt_limit"] == incident_outbox.MAX_ATTEMPTS
    assert _codes(queue) == set()


def test_dead_letters_alert_without_poisoning_live_backlog_age():
    session = _session()
    _row(session, 1, status="DEAD", attempts=incident_outbox.MAX_ATTEMPTS, created_ago=86_400)
    _row(session, 2, status="PENDING", created_ago=5)
    queue = _outbox(session)
    assert queue["dead_total"] == 1
    assert queue["pending_total"] == 1
    assert queue["oldest_age_seconds"] < 60
    assert queue["backlog_alert"] is False
    assert _codes(queue) == {"outbox_dead_letters"}


def test_retry_exhaustion_overdue_retry_and_stuck_claim_are_reported():
    session = _session()
    _row(session, 1, status="PENDING", attempts=incident_outbox.MAX_ATTEMPTS - 1, next_in=-600)
    _row(session, 2, status="PROCESSING", attempts=1, claimed_ago=incident_outbox.CLAIM_LEASE_SECONDS + 60)
    _row(session, 3, status="PROCESSING", attempts=1, claimed_ago=10)
    queue = _outbox(session)
    assert queue["max_attempts"] == incident_outbox.MAX_ATTEMPTS - 1
    assert queue["retry_exhaustion"] is True
    assert queue["overdue_retries"] == 1
    assert queue["stuck_claims"] == 1
    assert _codes(queue) == {"outbox_retry_exhaustion", "outbox_overdue_retry", "outbox_stuck_claim"}


def test_first_attempt_waiting_for_publisher_is_not_an_overdue_retry():
    session = _session()
    _row(session, 1, status="PENDING", attempts=0, next_in=-600)
    assert _outbox(session)["overdue_retries"] == 0
