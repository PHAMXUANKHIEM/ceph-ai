from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.alerting as alerting
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


class FakeHttpxResponse:
    def raise_for_status(self):
        pass


def test_send_alert_posts_webhook_when_url_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "http://example.test/hook", raising=False)
    monkeypatch.setattr(
        alerting.httpx, "post", lambda url, json, timeout: calls.append((url, json, timeout)) or FakeHttpxResponse()
    )

    alerting.send_alert("critical", "something broke", backup_job_id="job-1")

    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "http://example.test/hook"
    assert payload == {"severity": "critical", "message": "something broke", "backup_job_id": "job-1"}


def test_send_alert_does_not_call_http_when_url_blank(monkeypatch):
    calls = []
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    monkeypatch.setattr(alerting.httpx, "post", lambda *a, **kw: calls.append((a, kw)))

    alerting.send_alert("warning", "minor issue")

    assert calls == []


def test_send_alert_swallows_webhook_failure(monkeypatch):
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "http://example.test/hook", raising=False)

    def _boom(url, json, timeout):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(alerting.httpx, "post", _boom)

    alerting.send_alert("critical", "should not raise")  # must not propagate


def _enable_telegram(monkeypatch, token="123:ABC", chat_id="-100999"):
    monkeypatch.setattr(alerting.settings, "telegram_alerts_enabled", True, raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_bot_token", token, raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_chat_id", chat_id, raising=False)


def test_send_alert_sends_telegram_when_enabled_and_configured(monkeypatch):
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    _enable_telegram(monkeypatch)
    calls = []
    monkeypatch.setattr(
        alerting, "send_telegram_message", lambda token, chat_id, text: calls.append((token, chat_id, text))
    )

    alerting.send_alert("critical", "disk full", backup_job_id="job-1")

    assert len(calls) == 1
    token, chat_id, text = calls[0]
    assert token == "123:ABC"
    assert chat_id == "-100999"
    assert "disk full" in text
    assert "job-1" in text
    assert "CRITICAL" in text


def test_send_alert_skips_telegram_when_disabled(monkeypatch):
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_alerts_enabled", False, raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_bot_token", "123:ABC", raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_chat_id", "-100999", raising=False)
    calls = []
    monkeypatch.setattr(alerting, "send_telegram_message", lambda *a: calls.append(a))

    alerting.send_alert("warning", "minor issue")

    assert calls == []


def test_send_alert_skips_telegram_when_enabled_but_not_configured(monkeypatch):
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_alerts_enabled", True, raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_bot_token", "", raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_chat_id", "", raising=False)
    calls = []
    monkeypatch.setattr(alerting, "send_telegram_message", lambda *a: calls.append(a))

    alerting.send_alert("warning", "minor issue")

    assert calls == []


def test_send_alert_swallows_telegram_failure(monkeypatch):
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    _enable_telegram(monkeypatch)

    def _boom(token, chat_id, text):
        raise alerting.TelegramSendError("bad token")

    monkeypatch.setattr(alerting, "send_telegram_message", _boom)

    alerting.send_alert("critical", "should not raise")  # must not propagate


def test_send_alert_delivers_to_both_webhook_and_telegram_independently(monkeypatch):
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "http://example.test/hook", raising=False)
    _enable_telegram(monkeypatch)
    webhook_calls = []
    telegram_calls = []
    monkeypatch.setattr(
        alerting.httpx, "post", lambda url, json, timeout: webhook_calls.append((url, json)) or FakeHttpxResponse()
    )
    monkeypatch.setattr(
        alerting, "send_telegram_message", lambda token, chat_id, text: telegram_calls.append(text)
    )

    alerting.send_alert("warning", "both channels")

    assert len(webhook_calls) == 1
    assert len(telegram_calls) == 1


def _add_fresh_metadata_success(session):
    """Keeps the always-checked metadata target quiet in tests that only
    care about the RBD (pool, image) alert."""
    session.add(BackupJob(run_id="meta", job_type="metadata", status="SUCCESS", created_at=datetime.utcnow()))


def test_check_overdue_and_failed_backups_alerts_on_failure(isolated_db, monkeypatch):
    monkeypatch.setattr(
        alerting,
        "load_backup_policy",
        lambda: {"tracked_images": [{"pool": "vms", "image": "web01"}]},
    )
    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="r1", pool="vms", image="web01", job_type="full", status="FAILED",
                error_message="disk full", created_at=datetime.utcnow(),
            )
        )
        _add_fresh_metadata_success(session)
        session.commit()

    alerts = []
    monkeypatch.setattr(alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert len(alerts) == 1
    assert alerts[0][0] == "warning"
    assert "disk full" in alerts[0][1]


def test_check_overdue_and_failed_backups_alerts_when_stale(isolated_db, monkeypatch):
    monkeypatch.setattr(
        alerting,
        "load_backup_policy",
        lambda: {"tracked_images": [{"pool": "vms", "image": "web01"}]},
    )
    stale_time = datetime.utcnow() - timedelta(hours=alerting.RPO_HOURS + 1)
    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="r1", pool="vms", image="web01", job_type="full", status="SUCCESS",
                created_at=stale_time, finished_at=stale_time,
            )
        )
        _add_fresh_metadata_success(session)
        session.commit()

    alerts = []
    monkeypatch.setattr(alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert len(alerts) == 1
    assert "quá hạn RPO" in alerts[0][1]


def test_check_overdue_and_failed_backups_silent_when_fresh_and_successful(isolated_db, monkeypatch):
    monkeypatch.setattr(
        alerting,
        "load_backup_policy",
        lambda: {"tracked_images": [{"pool": "vms", "image": "web01"}]},
    )
    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="r1", pool="vms", image="web01", job_type="full", status="SUCCESS",
                created_at=datetime.utcnow(), finished_at=datetime.utcnow(),
            )
        )
        _add_fresh_metadata_success(session)
        session.commit()

    alerts = []
    monkeypatch.setattr(alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert alerts == []


def test_check_overdue_and_failed_backups_alerts_for_metadata_never_run(isolated_db, monkeypatch):
    monkeypatch.setattr(alerting, "load_backup_policy", lambda: {"tracked_images": []})

    alerts = []
    monkeypatch.setattr(alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert len(alerts) == 1
    assert "metadata" in alerts[0][1]
