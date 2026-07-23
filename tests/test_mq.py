import asyncio

from aiormq.exceptions import ChannelNotFoundEntity

from shared.mq import DLQ_NAME, DLX_NAME, QUEUE_NAME, declare_topology, get_connection

# 2026-07-23: bumped from 20 (2s total) after a real CI failure elsewhere
# in this file (see test_rejected_message_lands_in_dead_letter_queue's
# comment) — GitHub Actions' freshly-started RabbitMQ service container
# can be slower than this always-warm local broker; more attempts costs
# nothing in the common case, this poll returns as soon as its predicate
# is truthy.
POLL_ATTEMPTS = 50
POLL_INTERVAL_SECONDS = 0.1


async def _reset_topology(connection):
    # Start each test from a clean slate so tests don't depend on run order.
    # A missing queue closes the AMQP channel (ChannelNotFoundEntity), so a
    # fresh channel is opened per name rather than reusing one across a loop.
    for name in (QUEUE_NAME, DLQ_NAME):
        channel = await connection.channel()
        try:
            queue = await channel.get_queue(name)
            await queue.purge()
        except ChannelNotFoundEntity:
            pass  # queue doesn't exist yet — nothing to reset


async def _poll_until(predicate, attempts=POLL_ATTEMPTS, interval=POLL_INTERVAL_SECONDS):
    """Poll an async predicate until it returns truthy, or give up after `attempts`."""
    for _ in range(attempts):
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    return await predicate()


def test_declare_topology_is_idempotent():
    async def scenario():
        connection = await get_connection()
        async with connection:
            await _reset_topology(connection)
            channel = await connection.channel()

            queue1, dlx1, dlq1 = await declare_topology(channel)
            queue2, dlx2, dlq2 = await declare_topology(channel)

            assert queue1.name == queue2.name == QUEUE_NAME
            assert dlx1.name == dlx2.name == DLX_NAME
            assert dlq1.name == dlq2.name == DLQ_NAME

    asyncio.run(scenario())


def test_published_message_stays_in_queue_without_consumer():
    async def scenario():
        connection = await get_connection()
        async with connection:
            await _reset_topology(connection)
            channel = await connection.channel()
            await declare_topology(channel)

            await channel.default_exchange.publish(
                _make_message(b"test incident payload"),
                routing_key=QUEUE_NAME,
            )

            # Check from a fresh channel — a RobustChannel caches queue objects
            # by name, so re-checking on the SAME channel would return the
            # stale declaration_result from before the publish.
            async def _message_landed():
                check_channel = await connection.channel()
                passive_queue = await check_channel.declare_queue(QUEUE_NAME, passive=True)
                count = passive_queue.declaration_result.message_count
                return count if count >= 1 else None

            assert await _poll_until(_message_landed) is not None

    asyncio.run(scenario())


def test_rejected_message_lands_in_dead_letter_queue():
    async def scenario():
        connection = await get_connection()
        async with connection:
            await _reset_topology(connection)
            channel = await connection.channel()
            queue, _dlx, dlq = await declare_topology(channel)

            await channel.default_exchange.publish(
                _make_message(b"simulated failed incident"),
                routing_key=QUEUE_NAME,
            )

            # queue.get(timeout=...) already waits for the message to become
            # available, so no fixed sleep is needed before either get() call.
            # 2026-07-23: bumped from 5s after a real CI failure (GitHub
            # Actions' freshly-started RabbitMQ service container) —
            # QueueEmpty on the DLQ get specifically, meaning the DLX
            # reject->requeue=False->dead-letter hop occasionally takes
            # longer than 5s on a colder/more loaded broker than this
            # always-warm local one. The main queue get is bumped too for
            # the same reason, defensively — either can in principle be the
            # slow one, and a longer timeout costs nothing in the common
            # case (get() returns as soon as the message IS available).
            incoming = await queue.get(timeout=15)
            await incoming.reject(requeue=False)

            dlq_message = await dlq.get(timeout=15)
            assert dlq_message.body == b"simulated failed incident"
            await dlq_message.ack()

    asyncio.run(scenario())


def _make_message(body: bytes):
    import aio_pika

    return aio_pika.Message(body=body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT)
