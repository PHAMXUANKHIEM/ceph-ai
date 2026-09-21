import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db, telegram_outbox
from shared.db import Base
from shared.models import (
    Incident,
    IncidentStatus,
    TelegramOutbox,
    TelegramOutboxStatus,
)
from shared.time import utc_now


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(
        db,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, autocommit=False),
    )
    yield engine


def _incident(session):
    incident = Incident(
        ceph_code="NODE_RESOURCE_HIGH:node-1",
        status=IncidentStatus.PENDING_APPROVAL.value,
        detected_at=utc_now(),
        log_excerpt="CPU 95%",
    )
    session.add(incident)
    session.flush()
    return incident


def test_enqueue_is_idempotent_and_payload_is_redacted(isolated_db):
    with db.SessionLocal() as session:
        incident = _incident(session)
        first = telegram_outbox.enqueue_node_alert(
            session,
            incident_id=incident.id,
            host="node-1",
            message="CPU 95%",
        )
        second = telegram_outbox.enqueue_node_alert(
            session,
            incident_id=incident.id,
            host="node-1",
            message="CPU 95%",
        )
        session.commit()

        rows = session.query(TelegramOutbox).all()
        assert first == second
        assert len(rows) == 1
        payload = json.loads(rows[0].payload_json)
        assert payload["kind"] == "node_alert"
        assert "token" not in rows[0].payload_json.lower()
        assert rows[0].payload_hash


def test_dispatch_marks_event_sent_after_commit(isolated_db):
    calls = []
    with db.SessionLocal() as session:
        incident = _incident(session)
        event_id = telegram_outbox.enqueue_node_alert(
            session,
            incident_id=incident.id,
            host="node-1",
            message="CPU 95%",
        )
        session.commit()

    processed = telegram_outbox.dispatch_due(
        event_ids=[event_id],
        sender=lambda host, message: calls.append((host, message)),
    )

    with db.SessionLocal() as session:
        row = session.query(TelegramOutbox).one()
        assert processed == 1
        assert calls == [("node-1", "CPU 95%")]
        assert row.status == TelegramOutboxStatus.SENT.value
        assert row.attempts == 1
        assert row.sent_at is not None


def test_dispatch_failure_is_retryable_and_does_not_delete_incident(isolated_db):
    with db.SessionLocal() as session:
        incident = _incident(session)
        event_id = telegram_outbox.enqueue_node_alert(
            session,
            incident_id=incident.id,
            host="node-1",
            message="CPU 95%",
        )
        incident_id = incident.id
        session.commit()

    def fail(_host, _message):
        raise RuntimeError("token=super-secret Telegram unavailable")

    assert telegram_outbox.dispatch_due(event_ids=[event_id], sender=fail) == 1

    with db.SessionLocal() as session:
        row = session.query(TelegramOutbox).one()
        assert row.status == TelegramOutboxStatus.PENDING.value
        assert row.attempts == 1
        assert "super-secret" not in (row.last_error or "")
        assert session.get(Incident, incident_id) is not None


def test_dispatch_marks_dead_after_retry_budget(isolated_db, monkeypatch):
    monkeypatch.setattr(telegram_outbox, "MAX_ATTEMPTS", 1)
    with db.SessionLocal() as session:
        incident = _incident(session)
        event_id = telegram_outbox.enqueue_node_alert(
            session,
            incident_id=incident.id,
            host="node-1",
            message="CPU 95%",
        )
        session.commit()

    assert telegram_outbox.dispatch_due(
        event_ids=[event_id],
        sender=lambda *_: (_ for _ in ()).throw(RuntimeError("provider down")),
    ) == 1

    with db.SessionLocal() as session:
        assert session.query(TelegramOutbox).one().status == TelegramOutboxStatus.DEAD.value


def test_dead_event_can_be_replayed_and_stats_do_not_expose_payload(isolated_db, monkeypatch):
    monkeypatch.setattr(telegram_outbox, "MAX_ATTEMPTS", 1)
    with db.SessionLocal() as session:
        incident = _incident(session)
        event_id = telegram_outbox.enqueue_node_alert(
            session, incident_id=incident.id, host="node-1", message="CPU 95%",
        )
        session.commit()

    telegram_outbox.dispatch_due(
        event_ids=[event_id],
        sender=lambda *_: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    stats = telegram_outbox.delivery_stats()
    assert stats["DEAD"] == 1
    assert "payload" not in stats

    assert telegram_outbox.replay_dead(event_ids=[event_id], limit=20) == 1
    with db.SessionLocal() as session:
        row = session.query(TelegramOutbox).one()
        assert row.status == TelegramOutboxStatus.PENDING.value
        assert row.attempts == 0
        assert row.last_error is None


def test_backup_alert_payload_never_contains_credentials(isolated_db, monkeypatch):
    monkeypatch.setattr(telegram_outbox.settings, "telegram_backup_bot_token", "token-not-persisted")
    monkeypatch.setattr(telegram_outbox.settings, "telegram_backup_chat_id", "-100999")
    monkeypatch.setattr(telegram_outbox.settings, "telegram_backup_enabled", True)
    with db.SessionLocal() as session:
        event_id = telegram_outbox.enqueue_backup_alert(
            session,
            event_id="backup-alert:test-1",
            severity="critical",
            message="disk full",
            backup_job_id="job-12345678",
            cluster_name="CS-LAB",
        )
        session.commit()
        row = session.query(TelegramOutbox).one()
        assert event_id == row.event_id
        assert "token-not-persisted" not in row.payload_json
        assert "-100999" not in row.payload_json
        payload = json.loads(row.payload_json)
        assert payload["kind"] == "backup_alert"
        assert payload["cluster_name"] == "CS-LAB"


def test_backup_alert_dispatch_resolves_credentials_at_delivery(isolated_db, monkeypatch):
    monkeypatch.setattr(telegram_outbox.settings, "telegram_backup_bot_token", "token-at-delivery")
    monkeypatch.setattr(telegram_outbox.settings, "telegram_backup_chat_id", "-100999")
    monkeypatch.setattr(telegram_outbox.settings, "telegram_backup_enabled", True)
    calls = []

    def fake_send(severity, message, backup_job_id, **kwargs):
        calls.append((severity, message, backup_job_id, kwargs))
        return True

    monkeypatch.setattr(telegram_outbox.telegram_alerts, "send_backup_alert", fake_send)
    assert telegram_outbox.enqueue_backup_alert_and_dispatch(
        event_id="backup-alert:test-2",
        severity="warning",
        message="stale backup",
        backup_job_id="job-2",
        cluster_name="CS-LAB",
    )
    assert calls == [(
        "warning",
        "stale backup",
        "job-2",
        {
            "cluster_name": "CS-LAB",
            "bot_token": "token-at-delivery",
            "chat_id": "-100999",
            "enabled": True,
            "managed_channel": True,
        },
    )]
    with db.SessionLocal() as session:
        row = session.query(TelegramOutbox).filter_by(event_id="backup-alert:test-2").one()
        assert row.status == TelegramOutboxStatus.SENT.value
