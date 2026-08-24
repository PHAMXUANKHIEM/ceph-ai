"""Durable per-request RGW audit delivery to the dedicated Telegram channel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import uuid
from datetime import timedelta
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from config.settings import settings
from shared import db
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import (
    Cluster, LogFinding, LogIngestRun, LogPattern, RgwAccessAuditEvent, RgwAnalysisJob,
    RgwErrorNotification,
)
from shared.telegram_client import TelegramSendError, send_telegram_message
from watcher.rgw_access_log import (
    fetch_rgw_audit_log, fetch_rgw_audit_log_with,
    fetch_rgw_error_log, fetch_rgw_error_log_with,
)

logger = logging.getLogger(__name__)
_MAX_OBJECT_CHARS = 500
_MAX_ERROR_CHARS = 1000
_VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_ANALYSIS_DEBOUNCE_SECONDS = 60
_ANALYSIS_PROGRESS_SECONDS = 600


def _analysis_signature(cluster_id: str, host: str, message: str) -> str:
    lowered = message.lower()
    if "vault" in lowered or "retrieve actual key" in lowered or "error -13" in lowered:
        family = "rgw-vault-key-retrieval"
    else:
        family = re.sub(r"[0-9a-f]{8,}|\d+", "#", lowered)
    return hashlib.sha256(f"{cluster_id}|{host}|{family}".encode()).hexdigest()


def _queue_analysis_job(session, event: RgwErrorNotification) -> RgwAnalysisJob | None:
    signature = _analysis_signature(event.cluster_id, event.rgw_host, event.message)
    cutoff = event.created_at - timedelta(seconds=_ANALYSIS_DEBOUNCE_SECONDS)
    duplicate = (
        session.query(RgwAnalysisJob.id)
        .filter(RgwAnalysisJob.signature == signature)
        .filter(RgwAnalysisJob.created_at >= cutoff)
        .first()
    )
    if duplicate:
        return None
    job = RgwAnalysisJob(
        id=str(uuid.uuid4()), cluster_id=event.cluster_id, source_event_id=event.id,
        signature=signature, status="QUEUED",
    )
    session.add(job)
    return job


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
    if event.encryption == "Plaintext":
        encryption_text = "🔓 Không mã hóa (Plaintext)"
    elif event.encryption:
        encryption_text = f"🔐 {event.encryption}"
    else:
        encryption_text = "❔ Không xác định từ RGW Ops Log"
    return "\n".join((
        "🔔 THÔNG BÁO CEPH S3",
        "━━━━━━━━━━━━━━━━━━",
        f"📝 Hành động: {_event_name(event)}",
        f"📁 Bucket: {event.bucket or '-'}",
        f"📄 File: {obj}",
        f"⚖️ Size: {_human_size(event.bytes_sent)}",
        f"🛡️ Mã hóa: {encryption_text}",
        f"👤 User: {event.requester or '-'}",
        f"🌐 IP thực hiện: {event.remote_addr or '-'}",
        *status_line,
        f"⏰ Giờ VN: {local_time:%H:%M:%S - %d/%m/%Y}",
        "━━━━━━━━━━━━━━━━━━",
    ))


def _fetch(cluster: Cluster, host: str) -> list[dict]:
    if cluster.is_default:
        return fetch_rgw_audit_log(host)
    ssh_user, ssh_key, _mode, _container = resolve_ssh_creds(cluster)
    return fetch_rgw_audit_log_with(host, ssh_user, ssh_key)


def _fetch_errors(cluster: Cluster, host: str) -> list[dict]:
    if cluster.is_default:
        return fetch_rgw_error_log(host)
    ssh_user, ssh_key, _mode, _container = resolve_ssh_creds(cluster)
    return fetch_rgw_error_log_with(host, ssh_user, ssh_key)


def _ingest_errors(session, cluster: Cluster, host: str) -> None:
    rows = _fetch_errors(cluster, host)
    initialized = session.query(RgwErrorNotification.id).filter_by(
        cluster_id=cluster.id, rgw_host=host
    ).first() is not None
    for row in rows:
        fingerprint = hashlib.sha256(
            f"{cluster.id}|{host}|{row['timestamp_raw']}|{row['raw']}".encode()
        ).hexdigest()
        if session.query(RgwErrorNotification.id).filter_by(fingerprint=fingerprint).first():
            continue
        event = RgwErrorNotification(
            cluster_id=cluster.id, rgw_host=host, fingerprint=fingerprint,
            message=str(row["message"])[:2000], event_at=_naive_utc(row.get("timestamp")),
            telegram_sent=not initialized,
        )
        session.add(event)
        session.flush()
        if initialized:
            _queue_analysis_job(session, event)
    session.commit()


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
            encryption=(str(row["encryption"])[:64] if row.get("encryption") else None),
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

    errors = session.query(RgwErrorNotification).filter_by(telegram_sent=False).order_by(
        RgwErrorNotification.event_at
    ).limit(200).all()
    for event in errors:
        event.telegram_attempts += 1
        local_time = event.event_at.replace(tzinfo=timezone.utc).astimezone(_VIETNAM_TZ)
        job = session.query(RgwAnalysisJob).filter_by(source_event_id=event.id).first()
        job_label = f"RGW-{job.id[:8]}" if job else "đã gộp với job gần nhất"
        text = "\n".join((
            "🚨 LỖI CEPH RGW",
            "━━━━━━━━━━━━━━━━━━",
            f"📍 Host: {event.rgw_host}",
            f"❌ Lỗi: {event.message}",
            f"🧠 AI job: {job_label}",
            "📋 Trạng thái: QUEUED — đã vào hàng đợi Log Intelligence.",
            f"⏰ Giờ VN: {local_time:%H:%M:%S - %d/%m/%Y}",
            "━━━━━━━━━━━━━━━━━━",
        ))
        try:
            send_telegram_message(settings.telegram_rgw_bot_token, settings.telegram_rgw_chat_id, text)
        except TelegramSendError as exc:
            event.telegram_error = str(exc)[:_MAX_ERROR_CHARS]
            session.commit()
            break
        event.telegram_sent = True
        event.telegram_sent_at = datetime.utcnow()
        event.telegram_error = None
        session.commit()


def _send_analysis_status(text: str) -> None:
    if settings.telegram_rgw_enabled and settings.telegram_rgw_bot_token and settings.telegram_rgw_chat_id:
        send_telegram_message(settings.telegram_rgw_bot_token, settings.telegram_rgw_chat_id, text)


def _analysis_heartbeat(stop: threading.Event, label: str, host: str) -> None:
    elapsed = 0
    while not stop.wait(_ANALYSIS_PROGRESS_SECONDS):
        elapsed += _ANALYSIS_PROGRESS_SECONDS
        try:
            _send_analysis_status(
                "⏳ AI VẪN ĐANG PHÂN TÍCH RGW\n"
                f"Job: {label}\nHost: {host}\n"
                f"Tiến trình: RUNNING — đã chạy {elapsed // 60} phút; đang quét Loki, "
                "đối chiếu finding và kiểm tra trạng thái Ceph thực tế."
            )
        except Exception:
            logger.exception("failed to send RGW analysis heartbeat for %s", label)


def _process_analysis_jobs(limit: int = 1) -> None:
    """Claim and execute immediate jobs sequentially; polling remains fallback."""
    from watcher import log_intel
    from watcher.ceph_finding_verifier import verify

    for _ in range(limit):
        with db.SessionLocal() as session:
            job = (
                session.query(RgwAnalysisJob)
                .filter(RgwAnalysisJob.status == "QUEUED")
                .order_by(RgwAnalysisJob.created_at)
                .first()
            )
            if job is None:
                return
            event = session.get(RgwErrorNotification, job.source_event_id)
            cluster = session.get(Cluster, job.cluster_id)
            if event is None or cluster is None:
                job.status = "FAILED"
                job.error = "source event or cluster no longer exists"
                job.finished_at = datetime.utcnow()
                session.commit()
                continue
            job.status = "RUNNING"
            job.attempts += 1
            job.started_at = datetime.utcnow()
            job_id, cluster_id = job.id, job.cluster_id
            host, message = event.rgw_host, event.message
            session.expunge(cluster)
            session.commit()

        label = f"RGW-{job_id[:8]}"
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=_analysis_heartbeat, args=(heartbeat_stop, label, host),
            name=f"rgw-analysis-{job_id[:8]}", daemon=True,
        )
        try:
            _send_analysis_status(
                "🧠 AI BẮT ĐẦU PHÂN TÍCH RGW\n"
                f"Job: {label}\nHost: {host}\nLỗi: {message[:500]}\n"
                "Trạng thái: RUNNING — đang quét Loki và phân tích nguyên nhân."
            )
            heartbeat.start()
            run_id = log_intel.scan_and_store(
                cluster_id, cluster=cluster, target_host=host, target_daemon_type="rgw",
            )
            with db.SessionLocal() as session:
                run = session.get(LogIngestRun, run_id) if run_id else None
                if run is None:
                    raise RuntimeError("Log Intelligence bị tắt hoặc không tạo được ingest run")
                if run.status == "FAILED":
                    raise RuntimeError(
                        "Quét Loki thất bại: " + (run.error_message or "không có chi tiết")
                    )
                finding = (
                    session.query(LogFinding)
                    .filter(LogFinding.ingest_run_id == run_id)
                    .order_by(LogFinding.created_at.desc())
                    .first()
                ) if run_id else None
                result_text = "Không có pattern đủ điều kiện tạo finding trong cửa sổ hiện tại."
                finding_id = None
                if finding is not None:
                    finding_id = finding.id
                    pattern_ids = json.loads(finding.evidence_pattern_ids_json or "[]")
                    patterns = session.query(LogPattern).filter(LogPattern.id.in_(pattern_ids)).all() if pattern_ids else []
                    verification = verify(finding, patterns, cluster)
                    result_text = (
                        f"Finding: {finding.title or finding.id}\n"
                        f"Live verification: {verification.code}\n{verification.summary}"
                    )
                row = session.get(RgwAnalysisJob, job_id)
                row.status = "COMPLETED"
                row.ingest_run_id = run_id
                row.finding_id = finding_id
                row.finished_at = datetime.utcnow()
                session.commit()
            _send_analysis_status(
                f"✅ AI PHÂN TÍCH RGW HOÀN TẤT\nJob: {label}\n{result_text}"
            )
        except Exception as exc:
            logger.exception("immediate RGW analysis job %s failed", job_id)
            with db.SessionLocal() as session:
                row = session.get(RgwAnalysisJob, job_id)
                if row:
                    row.status = "FAILED"
                    row.error = " ".join(str(exc).split())[:1000]
                    row.finished_at = datetime.utcnow()
                    session.commit()
            try:
                _send_analysis_status(
                    f"❌ AI PHÂN TÍCH RGW THẤT BẠI\nJob: {label}\nLỗi: {' '.join(str(exc).split())[:700]}"
                )
            except Exception:
                logger.exception("failed to send RGW analysis failure status")
        finally:
            heartbeat_stop.set()
            if heartbeat.is_alive():
                heartbeat.join(timeout=1)


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
                    _ingest_errors(session, cluster, str(node["host"]))
                except Exception:
                    session.rollback()
                    logger.exception("RGW access audit collection failed for %s/%s", cluster.name, node["host"])
        _deliver_pending(session)
    _process_analysis_jobs(limit=1)


async def run() -> None:
    while True:
        await asyncio.to_thread(collect_once)
        await asyncio.sleep(max(5, settings.rgw_access_audit_interval_seconds))
