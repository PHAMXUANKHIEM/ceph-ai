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
    assert payload == {
        "severity": "critical",
        "message": "something broke",
        "backup_job_id": "job-1",
        "cluster_id": None,
    }


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
    monkeypatch.setattr(alerting.settings, "telegram_backup_bot_token", token, raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_backup_chat_id", chat_id, raising=False)


def test_send_alert_sends_telegram_when_configured(monkeypatch):
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


def test_send_alert_skips_telegram_when_not_configured(monkeypatch):
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_backup_bot_token", "", raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_backup_chat_id", "", raising=False)
    calls = []
    monkeypatch.setattr(alerting, "send_telegram_message", lambda *a: calls.append(a))

    alerting.send_alert("warning", "minor issue")

    assert calls == []


def test_send_alert_skips_telegram_when_disabled(monkeypatch):
    # 2026-08-07: `telegram_backup_enabled` is a SEPARATE toggle from
    # token+chat_id being set (Alert Telegram page's "Tắt kênh này" button).
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    _enable_telegram(monkeypatch)
    monkeypatch.setattr(alerting.settings, "telegram_backup_enabled", False, raising=False)
    calls = []
    monkeypatch.setattr(alerting, "send_telegram_message", lambda *a: calls.append(a))

    alerting.send_alert("warning", "minor issue")

    assert calls == []


def test_send_alert_skips_telegram_when_only_chat_id_configured(monkeypatch):
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_backup_bot_token", "", raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_backup_chat_id", "-100999", raising=False)
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


def test_check_overdue_and_failed_backups_does_not_repeat_ai_analyzed_failure(isolated_db, monkeypatch):
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
    monkeypatch.setattr(alerting, "send_alert", lambda severity, message, backup_job_id=None, cluster=None: alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert alerts == []


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
    monkeypatch.setattr(alerting, "send_alert", lambda severity, message, backup_job_id=None, cluster=None: alerts.append((severity, message)))

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
    monkeypatch.setattr(alerting, "send_alert", lambda severity, message, backup_job_id=None, cluster=None: alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert alerts == []


def test_check_overdue_and_failed_backups_uses_policy_rpo_hours(isolated_db, monkeypatch):
    monkeypatch.setattr(
        alerting,
        "load_backup_policy",
        lambda: {
            "tracked_images": [{"pool": "vms", "image": "web01"}],
            "rpo_hours": 48,
        },
    )
    backup_time = datetime.utcnow() - timedelta(hours=25)
    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="r1", pool="vms", image="web01", job_type="full", status="SUCCESS",
                created_at=backup_time, finished_at=backup_time,
            )
        )
        _add_fresh_metadata_success(session)
        session.commit()

    alerts = []
    monkeypatch.setattr(
        alerting,
        "send_alert",
        lambda severity, message, backup_job_id=None, cluster=None: alerts.append((severity, message)),
    )

    alerting.check_overdue_and_failed_backups()

    assert alerts == []


def test_check_overdue_and_failed_backups_uses_workload_rpo_override(isolated_db, monkeypatch):
    monkeypatch.setattr(alerting, "load_backup_policy", lambda: {
        "tracked_images": [{"pool": "vms", "image": "web01", "rpo_hours": 48}],
        "rpo_hours": 24,
    })
    backup_time = datetime.utcnow() - timedelta(hours=25)
    with db_module.SessionLocal() as session:
        session.add(BackupJob(run_id="r1", pool="vms", image="web01", job_type="full",
                              status="SUCCESS", created_at=backup_time, finished_at=backup_time))
        _add_fresh_metadata_success(session)
        session.commit()
    alerts = []
    monkeypatch.setattr(alerting, "send_alert",
                        lambda severity, message, backup_job_id=None, cluster=None:
                        alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert alerts == []


def test_check_overdue_and_failed_backups_alerts_for_metadata_never_run(isolated_db, monkeypatch):
    monkeypatch.setattr(alerting, "load_backup_policy", lambda: {"tracked_images": []})

    alerts = []
    monkeypatch.setattr(alerting, "send_alert", lambda severity, message, backup_job_id=None, cluster=None: alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert len(alerts) == 1
    assert "metadata" in alerts[0][1]


def test_check_overdue_and_failed_backups_uses_metadata_threshold(isolated_db, monkeypatch):
    monkeypatch.setattr(alerting, "load_backup_policy",
                        lambda: {"tracked_images": [], "metadata_rpo_hours": 12})
    stale_time = datetime.utcnow() - timedelta(hours=13)
    with db_module.SessionLocal() as session:
        session.add(BackupJob(run_id="meta", job_type="metadata", status="SUCCESS",
                              created_at=stale_time, finished_at=stale_time))
        session.commit()
    alerts = []
    monkeypatch.setattr(alerting, "send_alert",
                        lambda severity, message, backup_job_id=None, cluster=None:
                        alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert len(alerts) == 1
    assert "metadata" in alerts[0][1]
    assert "12h" in alerts[0][1]


def test_check_overdue_and_failed_backups_checks_configured_restore_drill(isolated_db, monkeypatch):
    monkeypatch.setattr(alerting, "load_backup_policy", lambda: {
        "tracked_images": [], "restore_drill_rpo_hours": 192,
        "restore_drill": {"pool": "vms", "image": "web01",
                          "scratch_pool": "scratch", "scratch_image": "drill01"},
    })
    with db_module.SessionLocal() as session:
        _add_fresh_metadata_success(session)
        session.commit()
    alerts = []
    monkeypatch.setattr(alerting, "send_alert",
                        lambda severity, message, backup_job_id=None, cluster=None:
                        alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert len(alerts) == 1
    assert "RestoreDrill" in alerts[0][1]


def _make_additional_cluster(session, **overrides):
    from shared.models import Cluster

    defaults = dict(
        name="cluster-b",
        ceph_mon_nodes="10.20.2.10",
        ssh_user="root",
        ssh_key_path="/root/.ssh/id_rsa",
        is_default=False,
        is_active=True,
        backup_enabled=True,
        backup_tracked_images="rbd/vm1",
        telegram_bot_token="123:CLUSTERB",
        telegram_chat_id="-100222",
        telegram_enabled=True,
    )
    defaults.update(overrides)
    cluster = Cluster(**defaults)
    session.add(cluster)
    session.commit()
    return cluster


def test_check_overdue_and_failed_backups_covers_additional_cluster_and_routes_its_own_telegram(
    isolated_db, monkeypatch
):
    """Multi-tenant remediation Phase 3 — an additional cluster's overdue/
    never-run backup is checked too, and its Telegram alert goes to ITS
    OWN channel (Phase 2 fields), never the global backup_telegram_* one."""
    monkeypatch.setattr(alerting, "load_backup_policy", lambda: {"tracked_images": []})
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    # Global backup Telegram channel deliberately left UNCONFIGURED here —
    # if the additional cluster's alert fell back to it, this would be a
    # cross-cluster channel leak (exactly what this test guards against).
    monkeypatch.setattr(alerting.settings, "telegram_backup_bot_token", "", raising=False)
    monkeypatch.setattr(alerting.settings, "telegram_backup_chat_id", "", raising=False)

    with db_module.SessionLocal() as session:
        cluster = _make_additional_cluster(session)
        cluster_id = cluster.id

    telegram_calls = []
    monkeypatch.setattr(
        alerting, "send_telegram_message", lambda token, chat_id, text: telegram_calls.append((token, chat_id, text))
    )

    alerting.check_overdue_and_failed_backups()

    # One alert for the never-run rbd/vm1 image, one for never-run metadata.
    assert len(telegram_calls) == 2
    for token, chat_id, _text in telegram_calls:
        assert token == "123:CLUSTERB"
        assert chat_id == "-100222"
    with db_module.SessionLocal() as session:
        # Confirms _check_target() looked up THIS cluster's own BackupJob
        # rows (none exist), not silently querying cluster_id=None.
        assert session.query(BackupJob).filter(BackupJob.cluster_id == cluster_id).count() == 0


def test_check_overdue_and_failed_backups_additional_cluster_without_telegram_is_skipped_not_fallback(
    isolated_db, monkeypatch
):
    """An additional cluster with NO Telegram channel configured must not
    fall back to the global Backup channel — same narrowing Phase 2
    established for regular Incidents."""
    monkeypatch.setattr(alerting, "load_backup_policy", lambda: {"tracked_images": []})
    monkeypatch.setattr(alerting.settings, "backup_alert_webhook_url", "", raising=False)
    _enable_telegram(monkeypatch)  # GLOBAL backup channel IS configured here

    with db_module.SessionLocal() as session:
        _make_additional_cluster(session, telegram_bot_token="", telegram_chat_id="")

    telegram_calls = []
    monkeypatch.setattr(alerting, "send_telegram_message", lambda *a: telegram_calls.append(a))

    alerting.check_overdue_and_failed_backups()

    # Only the default cluster's own 2 alerts (empty tracked_images + never-
    # run metadata) went out over the global channel; the additional
    # cluster's own 2 alerts (also never-run image + metadata) must be
    # silently skipped, not delivered over the global channel instead.
    assert len(telegram_calls) == 1  # default cluster's never-run metadata alert only


def test_check_overdue_and_failed_backups_uses_additional_cluster_rpo(isolated_db, monkeypatch):
    monkeypatch.setattr(alerting, "load_backup_policy", lambda: {"tracked_images": [], "rpo_hours": 24})
    backup_time = datetime.utcnow() - timedelta(hours=25)
    with db_module.SessionLocal() as session:
        cluster = _make_additional_cluster(session, backup_rpo_hours=48)
        session.add_all([
            BackupJob(run_id="cluster-rbd", cluster_id=cluster.id, pool="rbd", image="vm1",
                      job_type="full", status="SUCCESS", created_at=backup_time),
            BackupJob(run_id="cluster-meta", cluster_id=cluster.id, job_type="metadata",
                      status="SUCCESS", created_at=datetime.utcnow()),
        ])
        _add_fresh_metadata_success(session)
        session.commit()
    alerts = []
    monkeypatch.setattr(alerting, "send_alert",
                        lambda severity, message, backup_job_id=None, cluster=None:
                        alerts.append((severity, message)))

    alerting.check_overdue_and_failed_backups()

    assert alerts == []
