import json

import aio_pika

from shared.mq import QUEUE_NAME, declare_topology, get_connection

SCHEMA_VERSION = "1.0"


def build_envelope(
    incident_id: str,
    ceph_code: str,
    detected_at: str,
    nodes: list[str],
    log_excerpt: str,
    cluster_snapshot: dict,
) -> dict:
    """Build the Incident message envelope (matches Story 1.4 AC #2 exactly)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "incident_id": incident_id,
        "ceph_code": ceph_code,
        "detected_at": detected_at,
        "nodes": nodes,
        "log_excerpt": log_excerpt,
        "cluster_snapshot": cluster_snapshot,
    }


async def publish_incident(envelope: dict) -> None:
    """Publish an Incident envelope to the `incidents` queue.

    Topology (queue/exchange names, DLX) is owned solely by `shared/mq.py`
    (AD-2) — this function only declares it (idempotent) and publishes,
    never redefines it.
    """
    connection = await get_connection()
    async with connection:
        channel = await connection.channel()
        await declare_topology(channel)
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(envelope).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=QUEUE_NAME,
        )
