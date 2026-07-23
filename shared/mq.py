import aio_pika
from aio_pika.abc import AbstractChannel, AbstractConnection

from config.settings import settings

QUEUE_NAME = "incidents"
DLX_NAME = "incidents.dlx"
DLQ_NAME = "incidents.dlq"


CONNECT_TIMEOUT_SECONDS = 10


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
