from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import forecast_canary
from shared.db import Base
from shared.models import (
    NodeResourceForecastAlert,
    NodeResourceForecastAlertEvent,
    NodeResourceForecastTransition,
)


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _alert(session):
    alert = NodeResourceForecastAlert(
        cluster_name="CS-LAB",
        host="node-1",
        metric="cpu",
        status="OPEN",
        first_detected_at=datetime(2026, 8, 24, 10),
        last_detected_at=datetime(2026, 8, 24, 10),
        current_percent=80,
        predicted_percent=95,
        hours_to_90=4,
        confidence=0.9,
        samples=30,
        window_hours=24,
    )
    session.add(alert)
    session.flush()
    return alert


def _report(session):
    return forecast_canary.build_canary_report(
        session,
        cluster_id="cluster-id",
        cluster_name="CS-LAB",
        host="node-1",
        metric="cpu",
        now=datetime(2026, 8, 24, 12),
    )


def test_canary_uses_new_alert_event_table_when_migrated():
    engine, session = _session()
    try:
        alert = _alert(session)
        session.add(NodeResourceForecastAlertEvent(
            alert_id=alert.id,
            cluster_name="CS-LAB",
            host="node-1",
            metric="cpu",
            from_state="NORMAL",
            to_state="WARNING",
            reason="forecast breach",
            occurred_at=datetime(2026, 8, 24, 11),
        ))
        session.commit()

        report = _report(session)

        assert report["alert_lifecycle"]["event_count"] == 1
        assert report["alert_lifecycle"]["warning_events"] == 1
    finally:
        session.close()
        engine.dispose()


def test_canary_falls_back_to_legacy_transition_table_before_migration():
    engine, session = _session()
    try:
        NodeResourceForecastAlertEvent.__table__.drop(engine)
        alert = _alert(session)
        session.add(NodeResourceForecastTransition(
            alert_id=alert.id,
            previous_state="NORMAL",
            new_state="WARNING",
            reason="legacy forecast breach",
            changed_at=datetime(2026, 8, 24, 11),
        ))
        session.commit()

        report = _report(session)

        assert report["alert_lifecycle"]["event_count"] == 1
        assert report["alert_lifecycle"]["warning_events"] == 1
    finally:
        session.close()
        engine.dispose()
