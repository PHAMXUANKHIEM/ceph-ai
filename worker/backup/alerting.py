"""Backup failure/overdue alerting (Story 9.4, PRD FR-9) — the project's
FIRST outbound-webhook mechanism (verified before writing this: no Slack/
SMTP/Telegram/webhook code exists anywhere else in this codebase).

`check_overdue_and_failed_backups()` runs as a periodic job on the SAME
APScheduler instance `worker/backup/scheduler.py` already builds (not a
4th `asyncio.gather` coroutine) — a plain sync callable, which APScheduler
runs in its own thread-pool executor automatically, same as any blocking
job.

Deliberately simple for this story: no de-duplication/throttling of
repeated webhook sends while a failure persists across ticks, and no
severity nuance beyond "critical" (RestoreDrill failure, Story 9.4 AC #4)
vs "warning" (routine fail/overdue) — Story 9.5 (AI-powered monitoring)
layers smarter severity classification and de-dup on top of this.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from config.settings import settings
from shared import db
from shared.models import BackupJob
from shared.telegram_client import TelegramSendError, send_telegram_message
from worker.backup.policy_config import load_backup_policy

logger = logging.getLogger(__name__)

RPO_HOURS = 24
WEBHOOK_TIMEOUT_SECONDS = 10

# Prefixed onto every Telegram message so a severity is readable at a
# glance in a phone notification preview, before the operator even opens
# the chat — the generic webhook payload already carries severity as a
# separate JSON field, but a Telegram message is just one text blob.
_TELEGRAM_SEVERITY_PREFIX = {
    "critical": "\U0001f534 CRITICAL",  # red circle
    "warning": "\U0001f7e1 WARNING",  # yellow circle
    "info": "ℹ️ INFO",  # info symbol
}


def _send_telegram_alert(severity: str, message: str, backup_job_id: str | None) -> None:
    """Best-effort, same posture as the webhook POST below — a Telegram
    delivery failure (bad token, chat id the bot was never added to,
    network) is logged and swallowed, never allowed to fail the backup/
    drill run that triggered this alert. 2026-08-06: this Backup channel
    has its own independent Bot Token/Chat ID (no longer shared with
    Lỗi cụm/Phần cứng) — "configured" (both non-blank) IS the on/off
    switch, checked here rather than relying on send_telegram_message's
    own "missing config" error, so an operator who simply hasn't set up
    this channel never sees a log entry about it failing."""
    if not settings.telegram_backup_bot_token or not settings.telegram_backup_chat_id:
        return
    prefix = _TELEGRAM_SEVERITY_PREFIX.get(severity, severity.upper())
    text = f"{prefix}\n{message}"
    if backup_job_id:
        text += f"\nBackupJob: {backup_job_id}"
    try:
        send_telegram_message(settings.telegram_backup_bot_token, settings.telegram_backup_chat_id, text)
    except TelegramSendError:
        logger.exception("send_alert: Telegram delivery failed — alert already logged above")


def send_alert(severity: str, message: str, backup_job_id: str | None = None) -> None:
    """Always logged; then delivered over every channel currently
    configured — `settings.backup_alert_webhook_url` (generic JSON
    webhook) and/or the Backup Telegram channel (its own bot token/chat
    id), independently of each other. Both blank/disabled is a valid,
    silent (log-only) configuration, not an error. A delivery failure on
    either channel is logged and swallowed — sending an alert must never
    block or fail the backup/drill that triggered it, and a failure on one
    channel must never skip the other."""
    logger.log(
        logging.CRITICAL if severity == "critical" else logging.WARNING,
        "backup alert [%s]: %s (backup_job_id=%s)",
        severity,
        message,
        backup_job_id,
    )
    url = settings.backup_alert_webhook_url
    if url:
        try:
            httpx.post(
                url,
                json={"severity": severity, "message": message, "backup_job_id": backup_job_id},
                timeout=WEBHOOK_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("send_alert: webhook POST to %s failed — alert already logged above", url)
    _send_telegram_alert(severity, message, backup_job_id)


def check_overdue_and_failed_backups() -> None:
    """The periodic APScheduler job. For every tracked (pool, image) plus
    the cluster metadata backup, alerts if the latest BackupJob is FAILED,
    missing entirely, or older than the RPO."""
    policy = load_backup_policy()
    cutoff = datetime.utcnow() - timedelta(hours=RPO_HOURS)

    targets: list[tuple[str | None, str | None, str]] = [
        (t.get("pool"), t.get("image"), f"{t.get('pool')}/{t.get('image')}")
        for t in (policy.get("tracked_images") or [])
        if t.get("pool") and t.get("image")
    ]
    targets.append((None, None, "metadata cụm"))

    for pool, image, label in targets:
        with db.SessionLocal() as session:
            query = session.query(BackupJob)
            if pool is not None:
                query = query.filter(BackupJob.pool == pool, BackupJob.image == image, BackupJob.job_type != "restore_drill")
            else:
                query = query.filter(BackupJob.job_type == "metadata")
            latest = query.order_by(BackupJob.created_at.desc()).first()

        if latest is None:
            send_alert("warning", f"Chưa từng có backup thành công nào cho {label}")
            continue
        if latest.status == "FAILED":
            send_alert(
                "warning",
                f"Backup gần nhất cho {label} thất bại: {latest.error_message or 'không rõ nguyên nhân'}",
                backup_job_id=latest.id,
            )
            continue
        if latest.created_at < cutoff:
            send_alert(
                "warning",
                f"{label} quá hạn RPO {RPO_HOURS}h — lần backup thành công gần nhất lúc {latest.created_at.isoformat()}",
                backup_job_id=latest.id,
            )
