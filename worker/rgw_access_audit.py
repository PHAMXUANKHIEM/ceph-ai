"""Durable per-request RGW audit delivery to the dedicated Telegram channel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from config.settings import settings
from shared import db
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import Cluster, RgwAccessAuditEvent
from shared.telegram_client import TelegramSendError, send_telegram_message
from watcher.rgw_access_log import fetch_rgw_audit_log, fetch_rgw_audit_log_with

logger = logging.getLogger(__name__)
_MAX_OBJECT_CHARS = 500
_MAX_ERROR_CHARS = 1000
_VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _fingerprint(cluster_id: str, host: str, row: dict) -> str:
    # `path` participates only in the digest so presigned query strings are
    # never persisted or sent to Telegram, while distinct requests remain
    # distinguishable.
    payload = [cluster_id, host] + [row.get(key) for key in (
        "timestamp_raw", "method", "path", "requester", "remote_addr",
        "status", "bytes_sent", "latency_ms", "transaction_id",
    )]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _naive_utc(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    return datetime.utcnow()


def _event_name(event: RgwAccessAuditEvent) -> str:
    has_object = bool(event.object_key)
    if event.method == "PUT":
        return "ObjectCreated:Put" if has_object else "BucketCreated:Put"
    if event.method == "POST":
        return "ObjectCreated:Post"
    if event.method == "DELETE":
        return "ObjectRemoved:Delete" if has_object else "BucketRemoved:Delete"
    if event.method == "HEAD":
        return "ObjectAccessed:Head" if has_object else "BucketAccessed:Head"
    if event.method == "GET":
        return "ObjectAccessed:Get" if has_object else "BucketAccessed:List"
    return f"RgwRequest:{event.method.title()}"


def _human_size(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def _message(event: RgwAccessAuditEvent, cluster_name: str) -> str:
    obj = event.object_key or "-"
    if len(obj) > _MAX_OBJECT_CHARS:
        obj = obj[: _MAX_OBJECT_CHARS - 1] + "…"
    local_time = event.event_at.replace(tzinfo=timezone.utc).astimezone(_VIETNAM_TZ)
    status_line = () if event.http_status < 400 else (f"❗ Kết quả: HTTP {event.http_status}",)
    return "\n".join((
        "🔔 THÔNG BÁO CEPH S3",
        "━━━━━━━━━━━━━━━━━━",
        f"📝 Hành động: {_event_name(event)}",
        f"📁 Bucket: {event.bucket or '-'}",
        f"📄 File: {obj}",
        f"⚖️ Size: {_human_size(event.bytes_sent)}",
        "🛡️ Mã hóa: ❔ Không xác định từ RGW Ops Log",
        f"👤 User: {event.requester or '-'}",
        *status_line,
        f"⏰ Giờ VN: {local_time:%H:%M:%S - %d/%m/%Y}",
        "━━━━━━━━━━━━━━━━━━",
    ))


def _fetch(cluster: Cluster, host: str) -> list[dict]:
    if cluster.is_default:
        return fetch_rgw_audit_log(host)
    ssh_user, ssh_key, _mode, _container = resolve_ssh_creds(cluster)
    return fetch_rgw_audit_log_with(host, ssh_user, ssh_key)


def _ingest_host(session, cluster: Cluster, host: str) -> None:
    rows = _fetch(cluster, host)
    initialized = session.query(RgwAccessAuditEvent.id).filter_by(
        cluster_id=cluster.id, rgw_host=host
    ).first() is not None
    # Parser returns newest first; insert oldest first for a natural audit order.
    for row in reversed(rows):
        fingerprint = _fingerprint(cluster.id, host, row)
        exists = session.query(RgwAccessAuditEvent.id).filter_by(fingerprint=fingerprint).first()
        if exists:
            continue
        session.add(RgwAccessAuditEvent(
            cluster_id=cluster.id,
            rgw_host=host,
            fingerprint=fingerprint,
            method=str(row.get("method") or "UNKNOWN")[:16],
            action=str(row.get("action") or row.get("method") or "UNKNOWN")[:64],
            bucket=(str(row["bucket"])[:255] if row.get("bucket") else None),
            object_key=(str(row["object"]) if row.get("object") else None),
            requester=(str(row["requester"])[:255] if row.get("requester") else None),
            remote_addr=(str(row["remote_addr"])[:255] if row.get("remote_addr") else None),
            http_status=int(row.get("status") or 0),
            bytes_sent=row.get("bytes_sent"),
            latency_ms=row.get("latency_ms"),
            event_at=_naive_utc(row.get("timestamp")),
            # On the first scan, establish a baseline without flooding the
            # chat with old requests already present in the daemon tail.
            telegram_sent=not initialized,
        ))
    try:
        session.commit()
    except IntegrityError:
        # Another collector instance won the idempotency race.
        session.rollback()


def _deliver_pending(session) -> None:
    if not (settings.telegram_rgw_enabled and settings.telegram_rgw_bot_token and settings.telegram_rgw_chat_id):
        return
    pending = session.query(RgwAccessAuditEvent).filter_by(telegram_sent=False).order_by(
        RgwAccessAuditEvent.event_at, RgwAccessAuditEvent.created_at
    ).limit(500).all()
    cluster_names = {row.id: row.name for row in session.query(Cluster).all()}
    for event in pending:
        event.telegram_attempts += 1
        try:
            send_telegram_message(
                settings.telegram_rgw_bot_token,
                settings.telegram_rgw_chat_id,
                _message(event, cluster_names.get(event.cluster_id, event.cluster_id)),
            )
        except TelegramSendError as exc:
            event.telegram_error = str(exc)[:_MAX_ERROR_CHARS]
            session.commit()
            logger.warning("RGW audit Telegram delivery failed for %s: %s", event.id, exc)
            break
        event.telegram_sent = True
        event.telegram_sent_at = datetime.utcnow()
        event.telegram_error = None
        session.commit()


def collect_once() -> None:
    if not settings.rgw_access_audit_enabled:
        return
    with db.SessionLocal() as session:
        clusters = session.query(Cluster).filter_by(is_active=True).all()
        for cluster in clusters:
            nodes = configured_nodes() if cluster.is_default else configured_nodes(cluster)
            for node in nodes:
                if "RGW" not in node["roles"]:
                    continue
                try:
                    _ingest_host(session, cluster, str(node["host"]))
                except Exception:
                    session.rollback()
                    logger.exception("RGW access audit collection failed for %s/%s", cluster.name, node["host"])
        _deliver_pending(session)


async def run() -> None:
    while True:
        await asyncio.to_thread(collect_once)
        await asyncio.sleep(max(5, settings.rgw_access_audit_interval_seconds))
