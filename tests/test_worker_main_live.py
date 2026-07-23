"""Real end-to-end integration test: real RabbitMQ (publish + consume + DLX),
real SQLite DB file (isolated tmp file, not the shared prod DB) — no mocking
of the broker or the ORM.

Marked `live` (see pyproject.toml) — run explicitly with `pytest -m live`.
"""

import asyncio
from datetime import datetime

import pytest
from aiormq.exceptions import ChannelNotFoundEntity
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import worker.main as worker_main
from config.settings import settings
from shared import db as db_module
from shared.db import Base
from shared.mq import DLQ_NAME, QUEUE_NAME, get_connection
from shared.models import Incident, IncidentStatus
from watcher.publisher import build_envelope, publish_incident

pytestmark = pytest.mark.live


async def _reset_topology(connection):
    for name in (QUEUE_NAME, DLQ_NAME):
        channel = await connection.channel()
        try:
            queue = await channel.get_queue(name)
            await queue.purge()
        except ChannelNotFoundEntity:
            pass  # queue doesn't exist yet — nothing to reset


@pytest.fixture()
def isolated_real_db(tmp_path, monkeypatch):
    db_path = tmp_path / "worker_live.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    yield engine


async def _seed_incident_and_publish(incident_id: str, ceph_code: str) -> None:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code=ceph_code,
                status=IncidentStatus.NEW.value,
                detected_at=datetime.utcnow(),
            )
        )
        session.commit()

    envelope = build_envelope(
        incident_id=incident_id,
        ceph_code=ceph_code,
        detected_at=datetime.utcnow().isoformat(),
        nodes=["10.20.1.249"],
        log_excerpt="mon2 clock skew log",
        cluster_snapshot={"status": "HEALTH_WARN"},
    )
    await publish_incident(envelope)


def test_exhausted_retries_marks_incident_failed_and_dead_letters_real_message(isolated_real_db):
    incident_id = "worker-live-failed-1"

    async def scenario():
        connection = await get_connection()
        async with connection:
            await _reset_topology(connection)

        await _seed_incident_and_publish(incident_id, "MON_CLOCK_SKEW")

        async def always_fails(incident_id, envelope):
            raise RuntimeError("simulated Claude API timeout")

        # One real message; each failed attempt republishes a fresh message,
        # so max_messages = worker_max_retries consumes exactly the chain of
        # attempts down to the final one that dead-letters instead of retrying.
        await worker_main.run(
            process_incident=always_fails, max_messages=settings.worker_max_retries
        )

    asyncio.run(scenario())

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        assert incident.status == IncidentStatus.FAILED.value

    async def _check_dlq():
        connection = await get_connection()
        async with connection:
            channel = await connection.channel()
            dlq = await channel.declare_queue(DLQ_NAME, passive=True)
            return dlq.declaration_result.message_count

    assert asyncio.run(_check_dlq()) >= 1


def test_successful_processing_acks_and_sets_diagnosing_real_message(isolated_real_db):
    incident_id = "worker-live-ok-1"

    async def scenario():
        connection = await get_connection()
        async with connection:
            await _reset_topology(connection)

        await _seed_incident_and_publish(incident_id, "MON_CLOCK_SKEW")

        await worker_main.run(process_incident=worker_main.default_process_incident, max_messages=1)

    asyncio.run(scenario())

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        assert incident.status == IncidentStatus.DIAGNOSING.value

    async def _check_main_queue_empty():
        connection = await get_connection()
        async with connection:
            channel = await connection.channel()
            queue = await channel.declare_queue(QUEUE_NAME, passive=True)
            return queue.declaration_result.message_count

    assert asyncio.run(_check_main_queue_empty()) == 0
