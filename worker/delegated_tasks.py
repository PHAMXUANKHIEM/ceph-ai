"""RabbitMQ consumer for supervisor-created Ceph investigation tasks."""

from __future__ import annotations

import asyncio
import json
import logging

from shared.mq import declare_delegated_topology, get_connection, publish_delegated_task
from shared.ai_delegation import claim_tasks_for_dispatch, execute_task
from config.settings import settings

logger = logging.getLogger(__name__)
WATCHDOG_INTERVAL_SECONDS = 30


async def _dispatch_watchdog() -> None:
    """Recover tasks lost between DB commit, publish, and Worker crash."""
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
        try:
            task_ids = await asyncio.to_thread(claim_tasks_for_dispatch)
            for task_id in task_ids:
                try:
                    await publish_delegated_task(task_id)
                except Exception:
                    # The DB dispatch claim expires and will be retried on a
                    # later pass; do not bring down the AMQP consumer.
                    logger.exception("could not republish delegated task %s", task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("delegated task watchdog failed")


async def _process_message(message) -> None:
    """Execute and acknowledge one delivery without blocking other deliveries."""
    try:
        payload = json.loads(message.body)
        task_id = str(payload["task_id"]).strip()
        if not task_id:
            raise ValueError("delegated task message has an empty task_id")
        await execute_task(task_id)
    except Exception:
        logger.exception("delegated task message failed")
        await message.reject(requeue=False)
    else:
        await message.ack()


async def run(max_messages: int | None = None) -> None:
    if max_messages == 0:
        return
    connection = await get_connection()
    watchdog_task = asyncio.create_task(_dispatch_watchdog())
    active_tasks: set[asyncio.Task] = set()
    try:
        async with connection:
            channel = await connection.channel()
            active_limit = max(1, settings.delegated_ai_max_active_tasks)
            # Bound parent-task concurrency at the broker and let each task's
            # own semaphore bound its sub-agent fan-out independently.
            await channel.set_qos(prefetch_count=active_limit)
            queue = await declare_delegated_topology(channel)
            processed = 0
            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    task = asyncio.create_task(_process_message(message))
                    active_tasks.add(task)
                    task.add_done_callback(active_tasks.discard)
                    processed += 1
                    if max_messages is not None and processed >= max_messages:
                        break
                if active_tasks:
                    await asyncio.gather(*tuple(active_tasks))
    finally:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass
        for task in tuple(active_tasks):
            if not task.done():
                task.cancel()
        remaining_tasks = tuple(active_tasks)
        if remaining_tasks:
            await asyncio.gather(*remaining_tasks, return_exceptions=True)
        if not connection.is_closed:
            await connection.close()
