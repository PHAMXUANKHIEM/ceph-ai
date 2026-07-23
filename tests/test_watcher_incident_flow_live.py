"""Real end-to-end integration test: SSH+docker exec against the live Ceph
lab cluster, real SQLite DB file, real RabbitMQ publish — no mocking.

Marked `live` (see pyproject.toml) — run explicitly with `pytest -m live`.
Skips gracefully if the Watcher's SSH key isn't present.
"""

import asyncio
import os

import pytest
from sqlalchemy import create_engine

from config.settings import settings
from shared import db as db_module
from shared.db import Base
from shared.mq import QUEUE_NAME, get_connection
from shared.models import Incident
from watcher.ceph_client import query_cluster_health
from watcher.main import build_and_publish_incident

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.path.exists(settings.ssh_key_path),
        reason=f"Watcher SSH key not found at {settings.ssh_key_path} — skipping live test",
    ),
]


def test_real_mon_clock_skew_produces_incident_row_and_rabbitmq_message(tmp_path, monkeypatch):
    db_path = tmp_path / "watcher_live_flow.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )

    health = query_cluster_health()
    assert health["status"] == "HEALTH_WARN"
    assert "MON_CLOCK_SKEW" in health["checks"]

    build_and_publish_incident(None, health)

    with db_module.SessionLocal() as session:
        incidents = session.query(Incident).filter_by(ceph_code="MON_CLOCK_SKEW").all()
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.status == "NEW"
        assert incident.log_excerpt  # non-empty — real docker logs content

    async def _check_queue_has_message():
        connection = await get_connection()
        async with connection:
            channel = await connection.channel()
            queue = await channel.declare_queue(QUEUE_NAME, passive=True)
            return queue.declaration_result.message_count

    # Fresh connection to avoid RobustChannel's cached-queue-object staleness
    # (same lesson as Story 1.2's test_mq.py).
    message_count = asyncio.run(_check_queue_has_message())
    assert message_count >= 1
