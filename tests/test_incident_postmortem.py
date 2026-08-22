import json
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db
from shared.db import Base
from shared.incident_postmortem import PostmortemError, build_timeline, generate, validate_postmortem
from shared.models import Action, AuditEntry, Incident


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    value = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(db, "SessionLocal", value)
    return value


def _seed(factory):
    now = datetime(2026, 8, 22, 10, 0)
    with factory() as session:
        incident = Incident(
            id="inc-1", ceph_code="OSD_DOWN", status="RESOLVED", severity="HEALTH_ERR",
            detected_at=now, diagnosis_text="OSD stopped",
            signal_evidence_json='{"osd_id": 3, "api_token": "must-not-leak"}',
        )
        action = Action(
            id="act-1", incident_id="inc-1", action_id="restart_osd_daemon",
            classification="RISKY", status="EXECUTED", rationale="Restart OSD",
            created_at=now + timedelta(minutes=1),
        )
        audit = AuditEntry(
            id="audit-1", incident_id="inc-1", action_id="act-1",
            event_type="incident_fix_verified", actor="system",
            created_at=now + timedelta(minutes=2),
        )
        session.add_all([incident, action, audit])
        session.commit()
    return now


def test_build_timeline_uses_real_timestamped_rows_and_redacts(factory):
    _seed(factory)
    with factory() as session:
        timeline = build_timeline(session, "inc-1")
    assert [event["id"] for event in timeline["events"]] == [
        "incident:inc-1:detected", "action:act-1:created", "audit:audit-1"
    ]
    assert timeline["diagnosis_context"] == "OSD stopped"
    assert timeline["events"][0]["evidence"]["api_token"] == "[REDACTED]"


def test_validation_rejects_invented_citation(factory):
    _seed(factory)
    with factory() as session:
        timeline = build_timeline(session, "inc-1")
    result = {key: "x" for key in (
        "root_cause", "impact", "actions_taken", "verification", "prevention", "limitations"
    )} | {"citations": ["audit:not-real"]}
    with pytest.raises(PostmortemError, match="citation"):
        validate_postmortem(result, timeline)


def test_generate_persists_validated_postmortem_without_holding_session(factory, monkeypatch):
    _seed(factory)
    citation = "audit:audit-1"
    result = {key: key for key in (
        "root_cause", "impact", "actions_taken", "verification", "prevention", "limitations"
    )} | {"citations": [citation]}
    monkeypatch.setattr("shared.incident_postmortem._call_model", lambda payload: _async_value(result))
    assert asyncio.run(generate("inc-1"))["citations"] == [citation]
    with factory() as session:
        incident = session.get(Incident, "inc-1")
        assert json.loads(incident.postmortem_json)["root_cause"] == "root_cause"
        assert incident.postmortem_prompt_version == "v1"


async def _async_value(value):
    return value
