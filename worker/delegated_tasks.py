"""RabbitMQ consumer for supervisor-created Ceph investigation tasks."""

from __future__ import annotations

import asyncio
import json
import logging

from shared.mq import declare_delegated_topology, get_connection, publish_delegated_task
from shared.ai_delegation import claim_tasks_for_dispatch, execute_task

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


async def run(max_messages: int | None = None) -> None:
    if max_messages == 0:
        return
    connection = await get_connection()
    watchdog_task = asyncio.create_task(_dispatch_watchdog())
    try:
        async with connection:
            channel = await connection.channel()
            # Parent execution is intentionally serialized per Worker. This
            # keeps the broker from buffering several expensive delegated
            # jobs in one process while the task-level guard enforces the
            # fan-out limit inside the active job.
            await channel.set_qos(prefetch_count=1)
            queue = await declare_delegated_topology(channel)
            processed = 0
            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    try:
                        payload = json.loads(message.body)
                        task_id = str(payload["task_id"])
                        await execute_task(task_id)
                    except Exception:
                        logger.exception("delegated task message failed")
                        await message.reject(requeue=False)
                    else:
                        await message.ack()
                    processed += 1
                    if max_messages is not None and processed >= max_messages:
                        break
    finally:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass
        if not connection.is_closed:
            await connection.close()
