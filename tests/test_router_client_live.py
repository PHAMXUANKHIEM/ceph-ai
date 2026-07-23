"""Real end-to-end integration test: real 9router call (OpenAI-compatible
function-calling), real SQLite DB file (isolated tmp file, not the shared
prod DB).

Marked `live` (see pyproject.toml) — run explicitly with `pytest -m live`.
Skips gracefully if 9router isn't configured.
"""

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import worker.llm.router_client as router_client
from config.settings import settings
from shared import db as db_module
from shared.db import Base
from shared.models import Incident, IncidentStatus

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (settings.router_enabled and settings.router_api_key and settings.router_base_url),
        reason="9router not configured — skipping live router test",
    ),
]


def test_real_mon_clock_skew_produces_valid_diagnosis_and_saves_to_db(tmp_path, monkeypatch):
    db_path = tmp_path / "router_client_live.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )

    incident_id = "router-live-1"
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code="MON_CLOCK_SKEW",
                status=IncidentStatus.DIAGNOSING.value,
                detected_at=datetime.utcnow(),
            )
        )
        session.commit()

    envelope = {
        "schema_version": "1.0",
        "incident_id": incident_id,
        "ceph_code": "MON_CLOCK_SKEW",
        "detected_at": datetime.utcnow().isoformat(),
        "nodes": ["10.20.1.249", "10.20.1.253"],
        "log_excerpt": (
            "2026-07-16 10:00:01 mon.khiempx-mon2 clock skew 0.854861s > max 0.05s "
            "(mon.khiempx-mon2 is behind mon.khiempx-mon1 by 0.85s)"
        ),
        "cluster_snapshot": {
            "status": "HEALTH_WARN",
            "checks": {"MON_CLOCK_SKEW": {"severity": "HEALTH_WARN"}},
        },
    }

    asyncio.run(router_client.diagnose_incident(incident_id, envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        assert incident.diagnosis_text
        assert len(incident.diagnosis_text) > 0
