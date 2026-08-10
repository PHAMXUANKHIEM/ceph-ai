import asyncio
import json

import pytest

from shared.mq import QUEUE_NAME, declare_topology, get_connection
from watcher.publisher import SCHEMA_VERSION, build_envelope, publish_incident


async def _reset_queue(connection):
    channel = await connection.channel()
    try:
        queue = await channel.get_queue(QUEUE_NAME)
        await queue.purge()
    except Exception:
        pass  # queue doesn't exist yet — nothing to reset


def test_build_envelope_has_exact_ac2_fields():
    envelope = build_envelope(
        incident_id="abc-123",
        ceph_code="MON_CLOCK_SKEW",
        detected_at="2026-07-16T10:00:00",
        nodes=["10.20.1.249"],
        log_excerpt="mon2 clock skew log",
        cluster_snapshot={"status": "HEALTH_WARN", "checks": {}},
    )

    assert set(envelope.keys()) == {
        "schema_version",
        "incident_id",
        "ceph_code",
        "detected_at",
        "nodes",
        "log_excerpt",
        "cluster_snapshot",
        "cluster_id",
        "ssh_user",
        "ssh_key_path",
        "ceph_exec_mode",
        "ceph_container_name",
    }
    assert envelope["schema_version"] == SCHEMA_VERSION
    assert envelope["incident_id"] == "abc-123"
    assert envelope["nodes"] == ["10.20.1.249"]
    # Multi-cluster observability Phase 1: omitted cluster_id means "the
    # default cluster" (see build_envelope's own docstring), not an error.
    assert envelope["cluster_id"] is None


@pytest.mark.live
def test_publish_incident_delivers_real_message_to_rabbitmq():
    async def scenario():
        connection = await get_connection()
        async with connection:
            await _reset_queue(connection)
            channel = await connection.channel()
            await declare_topology(channel)

            envelope = build_envelope(
                incident_id="test-incident-1",
                ceph_code="MON_CLOCK_SKEW",
                detected_at="2026-07-16T10:00:00",
                nodes=["10.20.1.249"],
                log_excerpt="mon2 clock skew log",
                cluster_snapshot={"status": "HEALTH_WARN"},
            )

            await publish_incident(envelope)

            queue = await channel.declare_queue(QUEUE_NAME, passive=True)
            message = await queue.get(timeout=5)
            body = json.loads(message.body)
            await message.ack()

            assert body == envelope

    asyncio.run(scenario())
