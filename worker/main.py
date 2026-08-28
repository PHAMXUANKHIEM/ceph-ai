import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from config.settings import settings
from shared import db, service_health
from shared.mq import QUEUE_NAME, declare_topology, get_connection
from shared.models import Cluster, Incident, IncidentStatus
from shared.telegram_alerts import send_ai_unavailable_alert
from watcher import incident_grouping

logger = logging.getLogger(__name__)

RETRY_HEADER = "x-retry-count"

ProcessIncident = Callable[[str, dict], Awaitable[None]]


async def default_process_incident(incident_id: str, envelope: dict) -> None:
    """v1 injection-point placeholder (no-op success) — Story 2.3 will replace
    this with a real Claude diagnosis call, the same way Story 1.4's
    `build_and_publish_incident` replaced watcher/main.py's default callback
    without touching the surrounding loop."""
    logger.info("default_process_incident: received incident %s (diagnosis not yet implemented)", incident_id)


def _safe_retry_count(headers: Optional[dict]) -> int:
    """Read `x-retry-count` defensively — a non-int, missing, or negative
    header (corrupted, forged, or stripped by an intermediary) must never
    crash the consumer or silently extend the retry budget past
    `max_retries`; treat anything untrustworthy as attempt zero."""
    raw = (headers or {}).get(RETRY_HEADER, 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


async def _set_incident_status(incident_id: str, status: IncidentStatus) -> None:
    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        if incident is None:
            logger.warning(
                "_set_incident_status: no Incident row for id=%s — cannot set status=%s",
                incident_id,
                status.value,
            )
            return
        incident.status = status.value
        session.commit()


def _notify_ai_diagnosis_failed(incident_id: str) -> None:
    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        if incident is None:
            return
        cluster_name = None
        bot_token = chat_id = None
        enabled = None
        if incident.cluster_id is not None:
            cluster = session.get(Cluster, incident.cluster_id)
            if cluster is not None:
                cluster_name = cluster.name
                if cluster.telegram_bot_token and cluster.telegram_chat_id:
                    bot_token, chat_id = cluster.telegram_bot_token, cluster.telegram_chat_id
                    enabled = cluster.telegram_enabled
        ceph_code, severity = incident.ceph_code, incident.severity
    send_ai_unavailable_alert(
        ceph_code,
        severity,
        cluster_name=cluster_name,
        bot_token=bot_token,
        chat_id=chat_id,
        enabled=enabled,
    )


async def _republish_with_incremented_retry(
    channel: Any, message: AbstractIncomingMessage, next_retry_count: int
) -> None:
    """Publish a NEW message with an incremented `x-retry-count` header.

    RabbitMQ's `nack(requeue=True)` redelivers the original message unchanged
    — there is nowhere to persist an incrementing attempt counter on it. A
    fresh message with a bumped header is the simplest portable way to track
    attempts across redeliveries.
    """
    headers = dict(message.headers or {})
    headers[RETRY_HEADER] = next_retry_count
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=message.body,
            headers=headers,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=QUEUE_NAME,
    )


async def _handle_message(
    message: AbstractIncomingMessage,
    channel: Any,
    process_incident: ProcessIncident,
    max_retries: int,
) -> None:
    """Consume one Incident message: mark DIAGNOSING, run `process_incident`,
    and on failure either retry (fresh message, incremented header) or —
    once `max_retries` total attempts have been made — mark the Incident
    FAILED and dead-letter the message (AC #2/#3, FR4/AD-2).

    Every stage (parsing, the DIAGNOSING write, the retry/failure recovery
    itself) is individually guarded: an unparseable body, a transient DB
    error, or a broker hiccup must never propagate out of this function and
    kill the whole `run()` consumer loop — that would crash-loop the entire
    Worker on redelivery of the same poison message instead of dead-lettering
    just that one (AD-2: never drop silently, but never take the loop down
    either).
    """
    try:
        envelope = json.loads(message.body)
        incident_id = envelope["incident_id"]
    except Exception:
        logger.exception("_handle_message: unparseable message body — dead-lettering without retry")
        await message.reject(requeue=False)
        return

    # 2026-08-10 (multi-tenant remediation Phase 1): the "default cluster
    # only" guard that used to sit here is GONE — Worker's SSH credentials/
    # command-building now come from THIS envelope's own ssh_user/
    # ssh_key_path/ceph_exec_mode/ceph_container_name fields
    # (watcher/publisher.py::build_envelope), populated from the
    # ORIGINATING cluster (watcher/main.py::run_observed_cluster_loop for a
    # non-default one), never implicitly from the global `settings`
    # singleton. worker/llm/router_client.py's `_maybe_execute_safe_action`/
    # `_execute_approved_action` both resolve creds from the Incident's own
    # cluster_id now, not the process-wide default. See this session's plan
    # (multi-tenant ceph-aiops) for the full audit of what was made
    # cluster-aware to make this safe: shared/cluster_nodes.py,
    # watcher/collector.py, watcher/ceph_client.py's `_with`-suffixed
    # functions, worker/executor/ssh_executor.py::execute_command().

    retry_count = _safe_retry_count(message.headers)
    envelope = dict(envelope)

    try:
        with db.SessionLocal() as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                logger.warning("_handle_message: no Incident row for id=%s — acking, discarding", incident_id)
                await message.ack()
                return
            try:
                incident_grouping.assign_incident_group(session, incident)
                envelope["incident_group"] = incident_grouping.build_group_context(
                    session, incident_id
                )
            except Exception:
                # Grouping is enrichment, not a reason to lose a diagnosis.
                # A missing/unmigrated grouping column must not dead-letter a
                # valid Incident; the AI call can still run with its original
                # envelope while the error remains visible in the Worker log.
                session.rollback()
                logger.exception(
                    "_handle_message: incident grouping failed for %s; continuing without group context",
                    incident_id,
                )
            incident.status = IncidentStatus.DIAGNOSING.value
            session.commit()
    except Exception:
        logger.exception(
            "_handle_message: failed to set DIAGNOSING for incident %s — dead-lettering", incident_id
        )
        await message.reject(requeue=False)
        return

    try:
        await process_incident(incident_id, envelope)
    except Exception:
        logger.exception(
            "_handle_message: process_incident failed for incident %s (attempt %d)",
            incident_id,
            retry_count + 1,
        )
        try:
            if retry_count + 1 >= max_retries:
                await _set_incident_status(incident_id, IncidentStatus.FAILED)
                _notify_ai_diagnosis_failed(incident_id)
                # Dead-lettered via the queue's x-dead-letter-exchange (declared
                # once, in shared/mq.py::declare_topology — AD-2).
                await message.reject(requeue=False)
            else:
                await _republish_with_incremented_retry(channel, message, retry_count + 1)
                await message.ack()
        except Exception:
            logger.exception(
                "_handle_message: failure-recovery path itself failed for incident %s — "
                "dead-lettering as last resort",
                incident_id,
            )
            await message.reject(requeue=False)
        return

    # Deliberately outside the try/except above: if ack() itself raises
    # (e.g. channel closed) it must not be misread as a process_incident
    # failure — that would republish a duplicate for work already done.
    try:
        await message.ack()
    except Exception:
        logger.exception("_handle_message: ack() failed for incident %s after successful processing", incident_id)


def _effective_max_retries(configured: int) -> int:
    if configured < 1:
        logger.warning(
            "worker_max_retries=%d is invalid (must be >= 1) — clamping to 1", configured
        )
        return 1
    return configured


async def run(
    process_incident: ProcessIncident = default_process_incident,
    max_messages: Optional[int] = None,
) -> None:
    """Consume Incidents from the `incidents` queue forever (`max_messages=None`,
    real usage) or a bounded number of messages (tests). `max_messages=0`
    processes nothing.

    Topology (queue/DLX names) is owned solely by `shared/mq.py` — this
    function only declares it (idempotent) and consumes, never redefines it
    (AD-2).
    """
    if max_messages == 0:
        return

    max_retries = _effective_max_retries(settings.worker_max_retries)

    connection = await get_connection()
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue, _dlx, _dlq = await declare_topology(channel)

        processed = 0
        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                await _handle_message(message, channel, process_incident, max_retries)
                processed += 1
                if max_messages is not None and processed >= max_messages:
                    break


async def _main() -> None:
    from worker.backup import scheduler as backup_scheduler
    from worker import bucket_logging, rgw_access_audit
    from worker.llm.router_client import diagnose_incident, poll_approved_actions

    # Story 4.3: the approved-RISKY-action poller runs alongside the
    # RabbitMQ consumer in the same process/event loop — only the Worker
    # holds SSH executor credentials (AD-3), and an approval isn't a queue
    # message that run() would ever see on its own.
    # Epic 9, Story 9.1: the Backup Scheduler is a THIRD coroutine here,
    # same reasoning — it creates its own synthetic Incident/Action and
    # calls straight into _execute_approved_action(), no separate queue or
    # poll loop of its own (see worker/backup/scheduler.py's docstring).
    async def service_heartbeat() -> None:
        while True:
            service_health.record("worker")
            await asyncio.sleep(15)

    await asyncio.gather(
        run(process_incident=diagnose_incident),
        poll_approved_actions(),
        backup_scheduler.run(),
        bucket_logging.run(),
        rgw_access_audit.run(),
        service_heartbeat(),
    )


if __name__ == "__main__":
    # See watcher/main.py's identical change — "%(asctime)s" is what lets
    # the Settings page's log-cleanup feature filter this file by date.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    asyncio.run(_main())
