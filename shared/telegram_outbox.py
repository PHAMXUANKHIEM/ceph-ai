"""Durable, post-commit Telegram notification delivery.

The producer writes an outbox row in the same SQL transaction as the Incident
or other durable state. The dispatcher claims and sends rows only after that
transaction has committed. Delivery is therefore retryable and cannot make a
successful Incident transaction disappear.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_

from shared import db, telegram_alerts
from shared.models import (
    Cluster,
    Incident,
    TelegramOutbox,
    TelegramOutboxStatus,
)
from shared.time import utc_now

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
CLAIM_LEASE_SECONDS = 300
MAX_ERROR_LENGTH = 500

_ALERT_CALLS = frozenset({
    "send_ai_ops_digest_alert",
    "send_ai_unavailable_alert",
    "send_capacity_recovery_alert",
    "send_code_repair_alert",
    "send_capacity_threshold_alert",
    "send_log_finding_alert",
    "send_log_finding_recovery_pending_alert",
    "send_log_finding_resolved_alert",
    "send_node_forecast_alert",
    "send_performance_rca_alert",
    "send_periodic_health_status",
    "send_trash_capacity_alert",
    "send_vault_alert",
    "send_vitastor_alert",
    "send_volume_forecast_alert",
})
_CLUSTER_CHANNEL_ALERTS = frozenset({
    "send_ai_ops_digest_alert",
    "send_ai_unavailable_alert",
    "send_log_finding_alert",
    "send_log_finding_recovery_pending_alert",
    "send_log_finding_resolved_alert",
    "send_performance_rca_alert",
    "send_periodic_health_status",
    "send_trash_capacity_alert",
})
_SECRET_RE = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*\S+",
)


def _safe_error(exc: BaseException) -> str:
    """Keep provider diagnostics useful without persisting secret material."""
    message = _SECRET_RE.sub(
        lambda match: f"{match.group(1)}=[REDACTED]", str(exc),
    )
    return f"{type(exc).__name__}: {message[:MAX_ERROR_LENGTH]}"


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _enqueue(
    session,
    *,
    event_id: str,
    incident_id: str | None,
    category: str,
    payload: dict[str, Any],
) -> str:
    existing = session.query(TelegramOutbox).filter_by(event_id=event_id).one_or_none()
    if existing is not None:
        return existing.event_id

    row = TelegramOutbox(
        event_id=event_id,
        incident_id=incident_id,
        category=category,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        payload_hash=_payload_hash(payload),
        status=TelegramOutboxStatus.PENDING.value,
        next_attempt_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return row.event_id


def enqueue_node_alert(
    session,
    *,
    incident_id: str,
    host: str,
    message: str,
) -> str:
    """Queue one hardware alert, idempotent for one Incident lifecycle."""
    payload = {
        "kind": "node_alert",
        "incident_id": incident_id,
        "host": str(host),
        "message": str(message)[:4000],
    }
    return _enqueue(
        session,
        event_id=f"incident:{incident_id}:hardware_initial",
        incident_id=incident_id,
        category="hardware",
        payload=payload,
    )



def enqueue_osd_latency_alert(
    session,
    *,
    incident_id: str,
    osd_id: str,
    host: str,
    message: str,
) -> str:
    payload = {
        "kind": "osd_latency_alert",
        "incident_id": incident_id,
        "osd_id": osd_id,
        "host": str(host),
        "message": str(message)[:4000],
    }
    return _enqueue(
        session,
        event_id=f"incident:{incident_id}:hardware_osd_latency",
        incident_id=incident_id,
        category="hardware",
        payload=payload,
    )


def enqueue_crush_skew_alert(
    session,
    *,
    incident_id: str,
    signal: str,
    entity_label: str,
    message: str,
) -> str:
    payload = {
        "kind": "crush_skew_alert",
        "incident_id": incident_id,
        "signal": str(signal),
        "entity_label": str(entity_label),
        "message": str(message)[:4000],
    }
    return _enqueue(
        session,
        event_id=f"incident:{incident_id}:hardware_crush_skew",
        incident_id=incident_id,
        category="hardware",
        payload=payload,
    )


def enqueue_database_size_alert(session, *, incident_id: str, message: str) -> str:
    payload = {
        "kind": "database_size_alert",
        "incident_id": incident_id,
        "message": str(message)[:4000],
    }
    return _enqueue(
        session,
        event_id=f"incident:{incident_id}:database_capacity",
        incident_id=incident_id,
        category="incident",
        payload=payload,
    )

def enqueue_incident_alert(
    session,
    incident: Incident,
    *,
    event_kind: str = "initial",
    rationale: str | None = None,
) -> str:
    """Queue an immutable cluster Incident alert after the caller's commit.

    Only Incident evidence and presentation text are persisted. Telegram
    credentials are resolved by the dispatcher and never enter the payload.
    """
    cluster_name = None
    if incident.cluster_id:
        cluster = session.get(Cluster, incident.cluster_id)
        cluster_name = cluster.name if cluster else None
    is_reminder = event_kind.startswith("reminder")
    payload = {
        "kind": "incident_alert",
        "incident_id": incident.id,
        "ceph_code": incident.ceph_code,
        "severity": incident.severity,
        "log_excerpt": incident.log_excerpt,
        "cluster_id": incident.cluster_id,
        "cluster_name": cluster_name,
        "event_kind": event_kind,
        "diagnosis_text": incident.diagnosis_text if is_reminder else None,
        "rationale": rationale if is_reminder else None,
        "reminder": is_reminder,
    }
    return _enqueue(
        session,
        event_id=f"incident:{incident.id}:incident_{event_kind}",
        incident_id=incident.id,
        category="incident",
        payload=payload,
    )


def enqueue_alert_call(
    session,
    *,
    event_id: str,
    category: str,
    function: str,
    args: Iterable[Any] = (),
    kwargs: dict[str, Any] | None = None,
    incident_id: str | None = None,
    cluster_id: str | None = None,
    cluster_name: str | None = None,
) -> str:
    """Queue one whitelisted alert call without persisting credentials."""
    if function not in _ALERT_CALLS:
        raise ValueError(f"unsupported Telegram alert function: {function!r}")
    payload = {
        "kind": "alert_call",
        "function": function,
        "args": list(args),
        "kwargs": kwargs or {},
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
    }
    return _enqueue(
        session,
        event_id=event_id,
        incident_id=incident_id,
        category=category,
        payload=payload,
    )


def enqueue_alert_call_and_dispatch(
    *,
    event_id: str,
    category: str,
    function: str,
    args: Iterable[Any] = (),
    kwargs: dict[str, Any] | None = None,
    incident_id: str | None = None,
    cluster_id: str | None = None,
    cluster_name: str | None = None,
    sender: Callable[..., Any] | None = None,
) -> bool:
    """Commit an alert event first, then attempt bounded immediate delivery."""
    with db.SessionLocal() as session:
        event_id = enqueue_alert_call(
            session,
            event_id=event_id,
            category=category,
            function=function,
            args=args,
            kwargs=kwargs,
            incident_id=incident_id,
            cluster_id=cluster_id,
            cluster_name=cluster_name,
        )
        session.commit()
    dispatch_due(event_ids=[event_id], sender=sender)
    with db.SessionLocal() as session:
        row = session.query(TelegramOutbox.status).filter(
            TelegramOutbox.event_id == event_id,
        ).one_or_none()
        return bool(row and row[0] == TelegramOutboxStatus.SENT.value)


def enqueue_incident_verified_alert(
    session,
    *,
    incident_id: str,
    ceph_code: str,
    attempted_command: str | None = None,
    display_name: str | None = None,
) -> str:
    """Queue the post-remediation verification notification."""
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise ValueError(f"incident {incident_id!r} does not exist")
    payload = {
        "kind": "incident_verified_alert",
        "incident_id": incident_id,
        "cluster_id": incident.cluster_id,
        "cluster_name": None,
        "ceph_code": ceph_code,
        "attempted_command": attempted_command,
        "display_name": display_name,
    }
    if incident.cluster_id:
        cluster = session.get(Cluster, incident.cluster_id)
        payload["cluster_name"] = cluster.name if cluster else None
    return _enqueue(
        session,
        event_id=f"incident:{incident_id}:verified",
        incident_id=incident_id,
        category="incident",
        payload=payload,
    )


def enqueue_incident_verify_exhausted_alert(
    session,
    *,
    incident_id: str,
    ceph_code: str,
    attempts: int,
) -> str:
    """Queue the terminal post-remediation failure notification."""
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise ValueError(f"incident {incident_id!r} does not exist")
    payload = {
        "kind": "incident_verify_exhausted_alert",
        "incident_id": incident_id,
        "cluster_id": incident.cluster_id,
        "cluster_name": None,
        "ceph_code": ceph_code,
        "attempts": int(attempts),
    }
    if incident.cluster_id:
        cluster = session.get(Cluster, incident.cluster_id)
        payload["cluster_name"] = cluster.name if cluster else None
    return _enqueue(
        session,
        event_id=f"incident:{incident_id}:verify_exhausted",
        incident_id=incident_id,
        category="incident",
        payload=payload,
    )


def _claim_one(
    *,
    event_ids: set[str] | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    now = utc_now()
    lease_expired = now - timedelta(seconds=CLAIM_LEASE_SECONDS)
    due = or_(
        and_(
            TelegramOutbox.status == TelegramOutboxStatus.PENDING.value,
            TelegramOutbox.next_attempt_at <= now,
        ),
        and_(
            TelegramOutbox.status == TelegramOutboxStatus.PROCESSING.value,
            TelegramOutbox.claimed_at <= lease_expired,
        ),
    )
    with db.SessionLocal() as session:
        query = session.query(TelegramOutbox).filter(due)
        if event_ids is not None:
            query = query.filter(TelegramOutbox.event_id.in_(event_ids))
        candidate = query.order_by(
            TelegramOutbox.next_attempt_at,
            TelegramOutbox.created_at,
        ).first()
        if candidate is None:
            return None

        token = str(uuid.uuid4())
        updated = (
            session.query(TelegramOutbox)
            .filter(TelegramOutbox.id == candidate.id)
            .filter(TelegramOutbox.status == candidate.status)
            .filter(
                or_(
                    candidate.status != TelegramOutboxStatus.PROCESSING.value,
                    TelegramOutbox.claimed_at <= lease_expired,
                )
            )
            .update(
                {
                    TelegramOutbox.status: TelegramOutboxStatus.PROCESSING.value,
                    TelegramOutbox.claimed_at: now,
                    TelegramOutbox.claim_token: token,
                    TelegramOutbox.attempts: TelegramOutbox.attempts + 1,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            session.rollback()
            return None
        payload = json.loads(candidate.payload_json)
        session.commit()
        return candidate.id, token, payload


def _mark_sent(row_id: str, claim_token: str) -> None:
    with db.SessionLocal() as session:
        session.query(TelegramOutbox).filter(
            TelegramOutbox.id == row_id,
            TelegramOutbox.claim_token == claim_token,
            TelegramOutbox.status == TelegramOutboxStatus.PROCESSING.value,
        ).update(
            {
                TelegramOutbox.status: TelegramOutboxStatus.SENT.value,
                TelegramOutbox.sent_at: utc_now(),
                TelegramOutbox.updated_at: utc_now(),
                TelegramOutbox.last_error: None,
            },
            synchronize_session=False,
        )
        session.commit()


def _mark_failed(row_id: str, claim_token: str, exc: BaseException) -> None:
    now = utc_now()
    with db.SessionLocal() as session:
        row = session.query(TelegramOutbox).filter(
            TelegramOutbox.id == row_id,
            TelegramOutbox.claim_token == claim_token,
            TelegramOutbox.status == TelegramOutboxStatus.PROCESSING.value,
        ).one_or_none()
        if row is None:
            return
        terminal = row.attempts >= MAX_ATTEMPTS
        row.status = (
            TelegramOutboxStatus.DEAD.value
            if terminal
            else TelegramOutboxStatus.PENDING.value
        )
        row.next_attempt_at = now + timedelta(
            seconds=min(3600, 5 * (2 ** max(0, row.attempts - 1))),
        )
        row.last_error = _safe_error(exc)
        row.updated_at = now
        event_id = row.event_id
        attempts = row.attempts
        session.commit()
    logger.warning(
        "telegram outbox delivery failed event=%s attempts=%s terminal=%s",
        event_id,
        attempts,
        terminal,
        exc_info=True,
    )




def _cluster_channel_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve cluster credentials only at delivery time; never store secrets."""
    cluster = None
    cluster_id = payload.get("cluster_id")
    if cluster_id:
        with db.SessionLocal() as session:
            cluster = session.get(Cluster, cluster_id)
    has_cluster_channel = bool(
        cluster and cluster.telegram_bot_token and cluster.telegram_chat_id
    )
    return {
        "cluster_name": payload.get("cluster_name"),
        "bot_token": cluster.telegram_bot_token if has_cluster_channel else None,
        "chat_id": cluster.telegram_chat_id if has_cluster_channel else None,
        "enabled": cluster.telegram_enabled if has_cluster_channel else None,
    }


def _deliver(payload: dict[str, Any], sender: Callable[..., Any] | None = None) -> None:
    kind = payload.get("kind")
    if kind == "alert_call":
        function = payload.get("function")
        if function not in _ALERT_CALLS:
            raise ValueError(f"unsupported Telegram alert function: {function!r}")
        callbacks = {
            "send_ai_ops_digest_alert": telegram_alerts.send_ai_ops_digest_alert,
            "send_ai_unavailable_alert": telegram_alerts.send_ai_unavailable_alert,
            "send_capacity_recovery_alert": telegram_alerts.send_capacity_recovery_alert,
            "send_code_repair_alert": telegram_alerts.send_code_repair_alert,
            "send_capacity_threshold_alert": telegram_alerts.send_capacity_threshold_alert,
            "send_log_finding_alert": telegram_alerts.send_log_finding_alert,
            "send_log_finding_recovery_pending_alert": telegram_alerts.send_log_finding_recovery_pending_alert,
            "send_log_finding_resolved_alert": telegram_alerts.send_log_finding_resolved_alert,
            "send_node_forecast_alert": telegram_alerts.send_node_forecast_alert,
            "send_performance_rca_alert": telegram_alerts.send_performance_rca_alert,
            "send_periodic_health_status": telegram_alerts.send_periodic_health_status,
            "send_trash_capacity_alert": telegram_alerts.send_trash_capacity_alert,
            "send_vitastor_alert": telegram_alerts.send_vitastor_alert,
            "send_volume_forecast_alert": telegram_alerts.send_volume_forecast_alert,
        }
        if function == "send_vault_alert":
            from shared.vault_alerts import send_vault_alert
            callbacks[function] = send_vault_alert
        callback = sender or callbacks[function]
        kwargs = dict(payload.get("kwargs") or {})
        if function == "send_volume_forecast_alert" and isinstance(
            kwargs.get("target_at"), str
        ):
            kwargs["target_at"] = datetime.fromisoformat(kwargs["target_at"])
        if function in _CLUSTER_CHANNEL_ALERTS:
            kwargs.update(_cluster_channel_kwargs(payload))
        elif payload.get("cluster_name") is not None:
            kwargs.setdefault("cluster_name", payload["cluster_name"])
        delivered = callback(*payload.get("args", []), **kwargs)
        if delivered is False:
            raise RuntimeError(f"Telegram alert delivery returned false for {function}")
        return

    if kind == "node_alert":
        callback = sender or telegram_alerts.send_node_alert
        callback(payload["host"], payload["message"])
        return

    if kind == "osd_latency_alert":
        callback = sender or telegram_alerts.send_osd_latency_alert
        callback(payload["osd_id"], payload["host"], payload["message"])
        return
    if kind == "crush_skew_alert":
        callback = sender or telegram_alerts.send_crush_skew_alert
        callback(payload["signal"], payload["entity_label"], payload["message"])
        return
    if kind == "database_size_alert":
        callback = sender or telegram_alerts.send_database_size_alert
        callback(payload["message"])
        return
    if kind == "incident_verified_alert":
        callback = sender or telegram_alerts.send_incident_verified_alert
        callback_kwargs = _cluster_channel_kwargs(payload)
        if payload.get("attempted_command") is not None:
            callback_kwargs["attempted_command"] = payload["attempted_command"]
        if payload.get("display_name") is not None:
            callback_kwargs["display_name"] = payload["display_name"]
        delivered = callback(payload["ceph_code"], **callback_kwargs)
        if delivered is False:
            raise RuntimeError("Telegram verified alert delivery returned false")
        return
    if kind == "incident_verify_exhausted_alert":
        callback = sender or telegram_alerts.send_incident_verify_exhausted_alert
        delivered = callback(
            payload["ceph_code"],
            payload["attempts"],
            **_cluster_channel_kwargs(payload),
        )
        if delivered is False:
            raise RuntimeError("Telegram exhausted alert delivery returned false")
        return

    if kind != "incident_alert":
        raise ValueError(f"unsupported telegram outbox payload kind: {kind!r}")

    cluster = None
    cluster_id = payload.get("cluster_id")
    if cluster_id:
        with db.SessionLocal() as session:
            cluster = session.get(Cluster, cluster_id)
    has_cluster_channel = bool(
        cluster and cluster.telegram_bot_token and cluster.telegram_chat_id
    )
    kwargs = {"background": False}
    if cluster_id:
        kwargs.update(
            cluster_name=payload.get("cluster_name"),
            bot_token=cluster.telegram_bot_token if has_cluster_channel else None,
            chat_id=cluster.telegram_chat_id if has_cluster_channel else None,
            enabled=cluster.telegram_enabled if has_cluster_channel else None,
        )
    if payload.get("reminder"):
        kwargs.update(
            reminder=True,
            diagnosis_text=payload.get("diagnosis_text"),
            rationale=payload.get("rationale"),
        )
    delivered = telegram_alerts.send_incident_alert(
        payload["ceph_code"],
        payload.get("severity"),
        payload.get("log_excerpt"),
        **kwargs,
    )
    if delivered is False:
        raise RuntimeError("Telegram alert delivery returned false")


def delivery_stats() -> dict[str, int]:
    """Return bounded operator metrics without exposing payloads."""
    with db.SessionLocal() as session:
        rows = (
            session.query(TelegramOutbox.status)
            .all()
        )
        counts = {status.value: 0 for status in TelegramOutboxStatus}
        for (status,) in rows:
            counts[str(status)] = counts.get(str(status), 0) + 1
        now = utc_now()
        counts["due"] = (
            session.query(TelegramOutbox)
            .filter(
                TelegramOutbox.status == TelegramOutboxStatus.PENDING.value,
                TelegramOutbox.next_attempt_at <= now,
            )
            .count()
        )
        return counts


def replay_dead(*, event_ids: Iterable[str] | None = None, limit: int = 20) -> int:
    """Move only DEAD rows back to PENDING for an explicit operator replay."""
    requested = set(event_ids) if event_ids is not None else None
    now = utc_now()
    with db.SessionLocal() as session:
        query = session.query(TelegramOutbox).filter(
            TelegramOutbox.status == TelegramOutboxStatus.DEAD.value,
        )
        if requested is not None:
            query = query.filter(TelegramOutbox.event_id.in_(requested))
        rows = query.order_by(TelegramOutbox.created_at).limit(max(0, int(limit))).all()
        for row in rows:
            row.status = TelegramOutboxStatus.PENDING.value
            row.attempts = 0
            row.next_attempt_at = now
            row.claimed_at = None
            row.claim_token = None
            row.sent_at = None
            row.last_error = None
            row.updated_at = now
        session.commit()
        return len(rows)


def dispatch_due(
    *,
    limit: int = 20,
    event_ids: Iterable[str] | None = None,
    sender: Callable[..., Any] | None = None,
) -> int:
    """Claim and process due rows; return the number attempted.

    The optional sender is available for deterministic tests and the
    node-health compatibility path. Production calls use the shared sender.
    """
    requested = set(event_ids) if event_ids is not None else None
    processed = 0
    for _ in range(max(0, int(limit))):
        claimed = _claim_one(event_ids=requested)
        if claimed is None:
            break
        row_id, claim_token, payload = claimed
        try:
            _deliver(payload, sender=sender)
        except Exception as exc:
            _mark_failed(row_id, claim_token, exc)
            logger.exception("telegram outbox delivery exception for row %s", row_id)
        else:
            _mark_sent(row_id, claim_token)
        processed += 1
    return processed
