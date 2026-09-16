import json

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractConnection

from config.settings import settings
from shared.request_context import REQUEST_ID_HEADER, get_request_id

QUEUE_NAME = "incidents"
DLX_NAME = "incidents.dlx"
DLQ_NAME = "incidents.dlq"
DELEGATED_QUEUE_NAME = "ai.delegated.tasks"
DELEGATED_EXCHANGE_NAME = "ai.delegated.tasks.exchange"
DELEGATED_DLX_NAME = "ai.delegated.tasks.dlx"
DELEGATED_DLQ_NAME = "ai.delegated.tasks.dlq"


CONNECT_TIMEOUT_SECONDS = 10


def request_headers() -> dict[str, str]:
    """Return safe AMQP headers for the current request context."""
    request_id = get_request_id()
    return {REQUEST_ID_HEADER: request_id} if request_id else {}


async def get_connection() -> AbstractConnection:
    return await aio_pika.connect_robust(
        settings.rabbitmq_url, timeout=CONNECT_TIMEOUT_SECONDS
    )


async def declare_topology(channel: AbstractChannel):
    """Declare the incidents queue and its dead-letter exchange/queue (AD-2).

    Safe to call repeatedly with the SAME topology definition (e.g. on every
    service startup) — redeclaring with identical arguments is a no-op. If the
    topology definition changes (e.g. a new queue argument) while the old
    queue still exists, the broker raises PRECONDITION_FAILED and closes the
    channel instead of reconciling — this function does not handle that case.
    """
    dlx = await channel.declare_exchange(
        DLX_NAME, aio_pika.ExchangeType.DIRECT, durable=True
    )
    dlq = await channel.declare_queue(DLQ_NAME, durable=True)
    await dlq.bind(dlx, routing_key=QUEUE_NAME)

    queue = await channel.declare_queue(
        QUEUE_NAME,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DLX_NAME,
            "x-dead-letter-routing-key": QUEUE_NAME,
        },
    )

    return queue, dlx, dlq


async def declare_delegated_topology(channel: AbstractChannel):
    """Declare the durable supervisor queue and its dead-letter queue."""
    exchange = await channel.declare_exchange(
        DELEGATED_EXCHANGE_NAME, aio_pika.ExchangeType.DIRECT, durable=True
    )
    dlx = await channel.declare_exchange(
        DELEGATED_DLX_NAME, aio_pika.ExchangeType.DIRECT, durable=True
    )
    dlq = await channel.declare_queue(DELEGATED_DLQ_NAME, durable=True)
    await dlq.bind(dlx, routing_key=DELEGATED_QUEUE_NAME)
    queue = await channel.declare_queue(
        DELEGATED_QUEUE_NAME,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DELEGATED_DLX_NAME,
            "x-dead-letter-routing-key": DELEGATED_QUEUE_NAME,
        },
    )
    await queue.bind(exchange, routing_key=DELEGATED_QUEUE_NAME)
    return queue


async def publish_delegated_task(task_id: str) -> None:
    """Publish one parent task after its DB rows are committed."""
    connection = await get_connection()
    try:
        async with connection:
            channel = await connection.channel()
            queue = await declare_delegated_topology(channel)
            exchange = await channel.get_exchange(DELEGATED_EXCHANGE_NAME)
            await exchange.publish(
                aio_pika.Message(
                    body=json.dumps({"task_id": task_id}).encode(),
                    headers=request_headers(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=queue.name,
            )
    finally:
        if not connection.is_closed:
            await connection.close()
