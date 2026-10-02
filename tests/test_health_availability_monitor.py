import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.health_availability_monitor as ham
from config.settings import settings
from shared import db as db_module
from shared.db import Base
from shared.models import Action, ActionStatus, AuditEntry, Incident, IncidentStatus, TelegramOutbox

T0 = datetime(2026, 10, 2, 7, 0, 0)


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


@pytest.fixture()
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ham.telegram_outbox, "dispatch_due",
        lambda **kwargs: calls.append(kwargs["event_ids"]),
    )
    return calls


@pytest.fixture(autouse=True)
def ten_minute_threshold(monkeypatch):
    monkeypatch.setattr(settings, "ceph_health_unavailable_alert_seconds", 600)


def _outage(tracker, minutes, error="All MON nodes failed: health collection deadline exceeded"):
    detail = None
    for minute in range(0, minutes + 1):
        detail = tracker.record_failure(error, now=T0 + timedelta(minutes=minute))
    return detail


def test_tracker_waits_for_the_threshold_then_reports_the_outage():
    tracker = ham.HealthAvailabilityTracker()

    assert _outage(tracker, 9) is None
    detail = tracker.record_failure("still failing", now=T0 + timedelta(minutes=10))

    assert detail["unavailable_seconds"] == 600
    assert detail["consecutive_failures"] == 11
    assert detail["unavailable_since"] == T0.isoformat()
    assert detail["last_error"] == "still failing"


def test_tracker_success_resets_the_outage_clock():
    tracker = ham.HealthAvailabilityTracker()
    _outage(tracker, 9)

    assert tracker.record_success() is True
    assert tracker.record_success() is False
    assert tracker.record_failure("again", now=T0 + timedelta(minutes=30)) is None


def test_tracker_truncates_long_errors():
    tracker = ham.HealthAvailabilityTracker()

    detail = _outage(tracker, 10, error="x" * 5000)

    assert len(detail["last_error"]) == 500


def test_report_opens_one_gated_incident_and_alerts_once(isolated_db, dispatched):
    detail = _outage(ham.HealthAvailabilityTracker(), 10)

    first = ham.report_unavailable(detail, cluster_id="cluster-a")
    detail = {**detail, "unavailable_seconds": 1800, "consecutive_failures": 31}
    second = ham.report_unavailable(detail, cluster_id="cluster-a")

    assert first == second
    with db_module.SessionLocal() as session:
        incidents = session.query(Incident).all()
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.ceph_code == ham.CEPH_HEALTH_UNAVAILABLE_CODE
        assert incident.cluster_id == "cluster-a"
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert json.loads(incident.signal_evidence_json)["consecutive_failures"] == 31
        assert "30 phút" in incident.log_excerpt
        action = session.query(Action).one()
        assert action.action_id == "investigate_manually"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert session.query(AuditEntry).count() == 1
        assert session.query(TelegramOutbox).count() == 1
    assert len(dispatched) == 1


def test_resolve_closes_the_incident_and_cancels_its_action(isolated_db, dispatched):
    ham.report_unavailable(_outage(ham.HealthAvailabilityTracker(), 10), cluster_id="cluster-a")

    assert ham.resolve_unavailable(cluster_id="cluster-a") == 1

    with db_module.SessionLocal() as session:
        assert session.query(Incident).one().status == IncidentStatus.RESOLVED.value
        assert session.query(Action).one().status != ActionStatus.PENDING_APPROVAL.value
    assert ham.resolve_unavailable(cluster_id="cluster-a") == 0


def test_observed_cluster_never_touches_another_clusters_incident(isolated_db, dispatched):
    ham.report_unavailable(_outage(ham.HealthAvailabilityTracker(), 10), cluster_id="cluster-a")

    assert ham.resolve_unavailable(cluster_id="cluster-b", include_legacy_null=False) == 0
    ham.report_unavailable(
        _outage(ham.HealthAvailabilityTracker(), 10), cluster_id="cluster-b", include_legacy_null=False,
    )

    with db_module.SessionLocal() as session:
        rows = session.query(Incident).order_by(Incident.cluster_id).all()
        assert [(row.cluster_id, row.status) for row in rows] == [
            ("cluster-a", IncidentStatus.PENDING_APPROVAL.value),
            ("cluster-b", IncidentStatus.PENDING_APPROVAL.value),
        ]


def test_first_read_clears_an_incident_left_by_a_previous_process(isolated_db, dispatched, monkeypatch):
    ham.report_unavailable(_outage(ham.HealthAvailabilityTracker(), 10), cluster_id="cluster-a")
    resolves = []
    real_resolve = ham.resolve_unavailable
    monkeypatch.setattr(
        ham, "resolve_unavailable",
        lambda **kwargs: resolves.append(kwargs) or real_resolve(**kwargs),
    )
    tracker = ham.HealthAvailabilityTracker()

    ham.on_health_read(tracker, cluster_id="cluster-a")
    ham.on_health_read(tracker, cluster_id="cluster-a")

    assert len(resolves) == 1
    with db_module.SessionLocal() as session:
        assert session.query(Incident).one().status == IncidentStatus.RESOLVED.value


def test_failure_hook_swallows_database_errors(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(ham, "report_unavailable", broken)
    tracker = ham.HealthAvailabilityTracker(unavailable_since=T0 - timedelta(hours=1))

    ham.on_health_failure(tracker, "deadline exceeded", cluster_id="cluster-a")

    assert tracker.consecutive_failures == 1

