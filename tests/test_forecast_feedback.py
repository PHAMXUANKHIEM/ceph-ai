from datetime import datetime

import pytest

from shared import forecast_feedback
from shared.models import NodeResourceForecastAlert


def _alert():
    now = datetime(2026, 8, 25, 3, 0)
    return NodeResourceForecastAlert(
        cluster_name="CS-LAB", host="node-1", metric="cpu", status="OPEN",
        first_detected_at=now, last_detected_at=now,
        current_percent=80, predicted_percent=92, hours_to_90=2,
        confidence=.9, samples=50, window_hours=24,
    )


def test_feedback_is_append_only_and_validates_verdict(db_session):
    alert = _alert()
    db_session.add(alert)
    db_session.flush()

    feedback = forecast_feedback.add_feedback(
        db_session,
        alert_id=alert.id,
        verdict="true_positive",
        submitted_by="admin",
        note="RAM tăng đúng như dự báo",
        impact_percent=12.5,
    )
    db_session.commit()

    assert feedback.verdict == "TRUE_POSITIVE"
    assert db_session.query(type(feedback)).count() == 1

    with pytest.raises(ValueError):
        forecast_feedback.add_feedback(
            db_session,
            alert_id=alert.id,
            verdict="AUTO_EXECUTE",
            submitted_by="admin",
        )

    with pytest.raises(LookupError):
        forecast_feedback.add_feedback(
            db_session,
            alert_id=alert.id,
            verdict="TRUE_POSITIVE",
            submitted_by="admin",
            incident_id="missing-incident",
        )
