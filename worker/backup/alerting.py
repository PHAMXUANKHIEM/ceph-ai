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
from worker.backup.policy_config import load_backup_policy

logger = logging.getLogger(__name__)

RPO_HOURS = 24
WEBHOOK_TIMEOUT_SECONDS = 10


def send_alert(severity: str, message: str, backup_job_id: str | None = None) -> None:
    """Always logged; POSTs to `settings.backup_alert_webhook_url` only if
    configured (blank = disabled, not an error). A webhook delivery
    failure is logged and swallowed — sending an alert must never block
    or fail the backup/drill that triggered it."""
    logger.log(
        logging.CRITICAL if severity == "critical" else logging.WARNING,
        "backup alert [%s]: %s (backup_job_id=%s)",
        severity,
        message,
        backup_job_id,
    )
    url = settings.backup_alert_webhook_url
    if not url:
        return
    try:
        httpx.post(
            url,
            json={"severity": severity, "message": message, "backup_job_id": backup_job_id},
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("send_alert: webhook POST to %s failed — alert already logged above", url)


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
