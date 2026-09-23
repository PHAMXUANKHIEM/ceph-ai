"""Durable, post-commit RabbitMQ incident delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_

from shared import db
from shared.models import Incident, IncidentOutbox, IncidentOutboxStatus, IncidentStatus
from shared.time import utc_now
from watcher import publisher

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
CLAIM_LEASE_SECONDS = 300
MAX_ERROR_LENGTH = 500
CONSUMER_CLAIM_LEASE_SECONDS = 300
CONSUMER_PENDING = "PENDING"
CONSUMER_PROCESSING = "PROCESSING"
CONSUMER_DONE = "DONE"
_SECRET_RE = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*\S+",
)


def _safe_error(exc: BaseException) -> str:
    message = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", str(exc))
    return f"{type(exc).__name__}: {message[:MAX_ERROR_LENGTH]}"


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def enqueue(
    session,
    *,
    incident_id: str,
    payload: dict[str, Any],
    event_id: str | None = None,
) -> str:
    """Insert one immutable envelope in the caller's open transaction."""
    event_id = event_id or f"incident:{incident_id}:diagnose"
    existing = session.query(IncidentOutbox).filter_by(event_id=event_id).one_or_none()
    if existing is not None:
        return existing.event_id
    session.add(IncidentOutbox(
        event_id=event_id,
        incident_id=incident_id,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        payload_hash=_payload_hash(payload),
        status=IncidentOutboxStatus.PENDING.value,
        next_attempt_at=utc_now(),
    ))
    session.flush()
    return event_id


def _claim_one() -> tuple[str, str, str, dict[str, Any]] | None:
    now = utc_now()
    lease_expired = now - timedelta(seconds=CLAIM_LEASE_SECONDS)
    due = or_(
        and_(IncidentOutbox.status == IncidentOutboxStatus.PENDING.value,
             IncidentOutbox.next_attempt_at <= now),
        and_(IncidentOutbox.status == IncidentOutboxStatus.PROCESSING.value,
             IncidentOutbox.claimed_at <= lease_expired),
    )
    with db.SessionLocal() as session:
        candidate = session.query(IncidentOutbox).filter(due).order_by(
            IncidentOutbox.next_attempt_at, IncidentOutbox.created_at,
        ).first()
        if candidate is None:
            return None
        token = str(uuid.uuid4())
        updated = session.query(IncidentOutbox).filter(
            IncidentOutbox.id == candidate.id,
            IncidentOutbox.status == candidate.status,
        ).update({
            IncidentOutbox.status: IncidentOutboxStatus.PROCESSING.value,
            IncidentOutbox.claimed_at: now,
            IncidentOutbox.claim_token: token,
            IncidentOutbox.attempts: IncidentOutbox.attempts + 1,
            IncidentOutbox.updated_at: now,
        }, synchronize_session=False)
        if updated != 1:
            session.rollback()
            return None
        payload = json.loads(candidate.payload_json)
        session.commit()
        return candidate.id, token, candidate.event_id, payload


def _mark_sent(row_id: str, claim_token: str) -> None:
    with db.SessionLocal() as session:
        row = session.query(IncidentOutbox).filter(
            IncidentOutbox.id == row_id,
            IncidentOutbox.claim_token == claim_token,
            IncidentOutbox.status == IncidentOutboxStatus.PROCESSING.value,
        ).one_or_none()
        if row is None:
            return
        now = utc_now()
        row.status = IncidentOutboxStatus.SENT.value
        row.sent_at = now
        row.updated_at = now
        row.last_error = None
        row.publish_latency_ms = max(0.0, (now - row.claimed_at).total_seconds() * 1000) if row.claimed_at else None
        session.commit()


def mark_published(*, event_id: str) -> None:
    """Mark an immediate post-commit publish as delivered.

    Watcher may use this fast path after the outbox row is committed. If the
    publish fails, this function is never called and the Worker retries the
    still-PENDING row.
    """
    with db.SessionLocal() as session:
        session.query(IncidentOutbox).filter(
            IncidentOutbox.event_id == event_id,
            IncidentOutbox.status == IncidentOutboxStatus.PENDING.value,
        ).update({
            IncidentOutbox.status: IncidentOutboxStatus.SENT.value,
            IncidentOutbox.sent_at: utc_now(),
            IncidentOutbox.updated_at: utc_now(),
        }, synchronize_session=False)
        session.commit()


def claim_consumer(event_id: str) -> tuple[str, str | None]:
    """Claim one message for diagnosis, or return DONE/BUSY/MISSING.

    This is intentionally independent of producer delivery state. A broker
    redelivery must not re-run a completed diagnosis or its mutation.
    """
    now = utc_now()
    lease_expired = now - timedelta(seconds=CONSUMER_CLAIM_LEASE_SECONDS)
    with db.SessionLocal() as session:
        row = session.query(IncidentOutbox).filter_by(event_id=event_id).one_or_none()
        if row is None:
            return "MISSING", None
        if row.consumer_status == CONSUMER_DONE:
            return "DONE", None
        if (
            row.consumer_status == CONSUMER_PROCESSING
            and row.consumer_claimed_at is not None
            and row.consumer_claimed_at > lease_expired
        ):
            return "BUSY", None
        token = str(uuid.uuid4())
        updated = session.query(IncidentOutbox).filter(
            IncidentOutbox.id == row.id,
            IncidentOutbox.consumer_status == row.consumer_status,
        ).update({
            IncidentOutbox.consumer_status: CONSUMER_PROCESSING,
            IncidentOutbox.consumer_claimed_at: now,
            IncidentOutbox.consumer_claim_token: token,
        }, synchronize_session=False)
        if updated != 1:
            session.rollback()
            return "BUSY", None
        session.commit()
        return "CLAIMED", token


def mark_consumer_done(event_id: str, claim_token: str) -> None:
    with db.SessionLocal() as session:
        session.query(IncidentOutbox).filter(
            IncidentOutbox.event_id == event_id,
            IncidentOutbox.consumer_claim_token == claim_token,
            IncidentOutbox.consumer_status == CONSUMER_PROCESSING,
        ).update({
            IncidentOutbox.consumer_status: CONSUMER_DONE,
            IncidentOutbox.consumer_finished_at: utc_now(),
            IncidentOutbox.consumer_claimed_at: None,
            IncidentOutbox.consumer_claim_token: None,
        }, synchronize_session=False)
        session.commit()


def release_consumer(event_id: str, claim_token: str) -> None:
    """Return a failed delivery to PENDING so RabbitMQ retry can reclaim it."""
    with db.SessionLocal() as session:
        session.query(IncidentOutbox).filter(
            IncidentOutbox.event_id == event_id,
            IncidentOutbox.consumer_claim_token == claim_token,
            IncidentOutbox.consumer_status == CONSUMER_PROCESSING,
        ).update({
            IncidentOutbox.consumer_status: CONSUMER_PENDING,
            IncidentOutbox.consumer_claimed_at: None,
            IncidentOutbox.consumer_claim_token: None,
        }, synchronize_session=False)
        session.commit()


async def _publish(payload: dict[str, Any], event_id: str) -> None:
    try:
        await publisher.publish_incident(payload, event_id=event_id)
    except TypeError as exc:
        # Keep injected legacy publishers usable in deterministic tests.
        if "unexpected keyword argument 'event_id'" not in str(exc):
            raise
        await publisher.publish_incident(payload)


def _mark_failed(row_id: str, claim_token: str, exc: BaseException) -> None:
    now = utc_now()
    with db.SessionLocal() as session:
        row = session.query(IncidentOutbox).filter(
            IncidentOutbox.id == row_id,
            IncidentOutbox.claim_token == claim_token,
            IncidentOutbox.status == IncidentOutboxStatus.PROCESSING.value,
        ).one_or_none()
        if row is None:
            return
        terminal = row.attempts >= MAX_ATTEMPTS
        row.status = IncidentOutboxStatus.DEAD.value if terminal else IncidentOutboxStatus.PENDING.value
        row.next_attempt_at = now + timedelta(seconds=min(3600, 5 * (2 ** max(0, row.attempts - 1))))
        row.last_error = _safe_error(exc)
        row.updated_at = now
        event_id, attempts = row.event_id, row.attempts
        session.commit()
    logger.warning("incident outbox delivery failed event=%s attempts=%s terminal=%s", event_id, attempts, terminal, exc_info=True)


def dispatch_due(*, limit: int = 20) -> int:
    """Publish due rows with bounded retry and lease recovery."""
    processed = 0
    for _ in range(max(0, int(limit))):
        claimed = _claim_one()
        if claimed is None:
            break
        row_id, claim_token, event_id, payload = claimed
        try:
            asyncio.run(_publish(payload, event_id))
        except Exception as exc:
            _mark_failed(row_id, claim_token, exc)
        else:
            _mark_sent(row_id, claim_token)
        processed += 1
    return processed


def delivery_stats() -> dict[str, int]:
    with db.SessionLocal() as session:
        counts = {status.value: 0 for status in IncidentOutboxStatus}
        rows = session.query(IncidentOutbox.status, IncidentOutbox.attempts, IncidentOutbox.created_at, IncidentOutbox.publish_latency_ms).all()
        for status, _attempts, _created_at, _latency in rows:
            counts[str(status)] = counts.get(str(status), 0) + 1
        pending = [row for row in session.query(IncidentOutbox).filter(
            IncidentOutbox.status == IncidentOutboxStatus.PENDING.value,
        ).all()]
        counts["due"] = session.query(IncidentOutbox).filter(
            IncidentOutbox.status == IncidentOutboxStatus.PENDING.value,
            IncidentOutbox.next_attempt_at <= utc_now(),
        ).count()
        counts["oldest_pending_age_seconds"] = int(max(
            [max(0.0, (utc_now() - row.created_at).total_seconds()) for row in pending] or [0]
        ))
        counts["max_attempts"] = max([row.attempts for row in session.query(IncidentOutbox).all()] or [0])
        latencies = [row.publish_latency_ms for row in session.query(IncidentOutbox).filter(IncidentOutbox.publish_latency_ms.isnot(None)).all()]
        counts["publish_latency_ms_avg"] = int(sum(latencies) / len(latencies)) if latencies else 0
        return counts


def reconcile_stale(*, max_age_seconds: int = 300) -> dict[str, int]:
    """Find committed incidents that have no usable RabbitMQ delivery row."""
    cutoff = utc_now() - timedelta(seconds=max(1, int(max_age_seconds)))
    missing_delivery = 0
    stale_pending = 0
    with db.SessionLocal() as session:
        incidents = session.query(Incident).filter(
            Incident.status == IncidentStatus.NEW.value,
            Incident.created_at <= cutoff,
        ).all()
        for incident in incidents:
            row = session.query(IncidentOutbox).filter_by(incident_id=incident.id).one_or_none()
            if row is None:
                missing_delivery += 1
                logger.error(
                    "incident outbox reconciler: Incident %s has no delivery row",
                    incident.id,
                )
            elif row.status in {
                IncidentOutboxStatus.PENDING.value,
                IncidentOutboxStatus.PROCESSING.value,
            } and row.created_at <= cutoff:
                stale_pending += 1
        session.rollback()
    result = {"missing_delivery": missing_delivery, "stale_pending": stale_pending}
    if any(result.values()):
        logger.warning("incident outbox reconciler found stale delivery: %s", result)
    return result
