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
    cluster_id: str | None = None,
) -> dict:
    """Build the Incident message envelope (matches Story 1.4 AC #2 exactly).

    `cluster_id` (multi-cluster observability Phase 1, default None):
    worker/main.py's message handler uses this to skip AI diagnosis/
    remediation entirely for any cluster other than the default one — see
    that module's cluster-scope guard. None means "the default cluster",
    same COALESCE convention as Incident.cluster_id/WatcherHeartbeat.cluster_id."""
    return {
        "schema_version": SCHEMA_VERSION,
        "incident_id": incident_id,
        "ceph_code": ceph_code,
        "detected_at": detected_at,
        "nodes": nodes,
        "log_excerpt": log_excerpt,
        "cluster_snapshot": cluster_snapshot,
        "cluster_id": cluster_id,
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
