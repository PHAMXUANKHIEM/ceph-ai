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
import uuid
from datetime import datetime, timedelta
from shared.time import utc_now
from typing import TYPE_CHECKING

import httpx

from config.settings import settings
from shared import db, telegram_outbox
from shared.clusters import list_active_clusters
from shared.models import BackupJob
from shared.notification_channels import enqueue_external_alert
from worker.backup.cluster_scope import parse_tracked_images
from worker.backup.policy_config import load_backup_policy

if TYPE_CHECKING:
    from shared.models import Cluster

logger = logging.getLogger(__name__)

RPO_HOURS = 24
METADATA_RPO_HOURS = 12
RESTORE_DRILL_RPO_HOURS = 192
WEBHOOK_TIMEOUT_SECONDS = 10

def _send_telegram_alert(
    severity: str, message: str, backup_job_id: str | None, cluster: "Cluster | None" = None
) -> None:
    """Queue a backup notification and deliver it after the DB commit.

    Only non-secret alert context is persisted. Telegram credentials are
    resolved by the outbox dispatcher at delivery time. A queue failure is
    logged and swallowed so notification handling never breaks a backup run.
    """
    if cluster is not None:
        if not cluster.telegram_enabled or not cluster.telegram_bot_token or not cluster.telegram_chat_id:
            logger.info(
                "send_alert: cluster %s has no Telegram channel configured — skipping Telegram delivery",
                cluster.id,
            )
            return
        cluster_id = str(cluster.id)
        cluster_name = cluster.name.strip()
    else:
        if (
            not settings.telegram_backup_enabled
            or not settings.telegram_backup_bot_token
            or not settings.telegram_backup_chat_id
        ):
            return
        cluster_id = None
        cluster_name = settings.cluster_name.strip()

    event_id = (
        f"backup-alert:{cluster_id or 'default'}:"
        f"{backup_job_id or 'adhoc'}:{uuid.uuid4().hex}"
    )
    try:
        telegram_outbox.enqueue_backup_alert_and_dispatch(
            event_id=event_id,
            severity=severity,
            message=message,
            backup_job_id=backup_job_id,
            cluster_id=cluster_id,
            cluster_name=cluster_name,
        )
    except Exception:
        logger.exception("send_alert: failed to enqueue backup Telegram alert")


def send_alert(
    severity: str, message: str, backup_job_id: str | None = None, cluster: "Cluster | None" = None
) -> None:
    """Always logged; then delivered over every channel currently
    configured — `settings.backup_alert_webhook_url` (generic JSON
    webhook, stays GLOBAL/shared for every cluster — not secret-scoped the
    way Telegram is, out of Phase 3's stated bound) and/or the relevant
    Telegram channel (`cluster`'s own if given, else the default
    cluster's global Backup channel), independently of each other. Both
    blank/disabled is a valid, silent (log-only) configuration, not an
    error. A delivery failure on either channel is logged and swallowed —
    sending an alert must never block or fail the backup/drill that
    triggered it, and a failure on one channel must never skip the other."""
    logger.log(
        logging.CRITICAL if severity == "critical" else logging.WARNING,
        "backup alert [%s]: %s (backup_job_id=%s, cluster_id=%s)",
        severity,
        message,
        backup_job_id,
        cluster.id if cluster is not None else None,
    )
    enqueue_external_alert(
        category="backup", severity=severity, message=message,
        cluster_name=cluster.name.strip() if cluster is not None else settings.cluster_name.strip(),
    )
    url = settings.backup_alert_webhook_url
    if url:
        try:
            httpx.post(
                url,
                json={
                    "severity": severity,
                    "message": message,
                    "backup_job_id": backup_job_id,
                    "cluster_id": cluster.id if cluster is not None else None,
                },
                timeout=WEBHOOK_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("send_alert: webhook POST to %s failed — alert already logged above", url)
    _send_telegram_alert(severity, message, backup_job_id, cluster)


def _check_target(
    pool: str | None, image: str | None, label: str, cutoff: datetime,
    cluster: "Cluster | None" = None, rpo_hours: int = RPO_HOURS,
    job_type: str | None = None,
) -> None:
    """Shared by the default cluster's loop and the additional-clusters'
    loop below — `cluster_id` is ALWAYS filtered explicitly (never left
    implicit), same correctness requirement as every other BackupJob query
    in this codebase once more than one cluster can produce rows for a
    same-named pool/image (Phase 3)."""
    cluster_id = cluster.id if cluster is not None else None
    with db.SessionLocal() as session:
        query = session.query(BackupJob).filter(BackupJob.cluster_id == cluster_id)
        if job_type is not None:
            query = query.filter(BackupJob.job_type == job_type)
        elif pool is not None:
            query = query.filter(BackupJob.pool == pool, BackupJob.image == image,
                                 BackupJob.job_type.in_(("full", "incremental")))
        latest = query.order_by(BackupJob.created_at.desc()).first()

    if latest is None:
        send_alert("warning", f"Chưa từng có backup thành công nào cho {label}", cluster=cluster)
        return
    if latest.status == "FAILED":
        # The failure path calls ai_analysis.analyze_backup_job immediately
        # after persisting the row and already sends one concise AI analysis.
        # Do not periodically resend the raw traceback without its solution.
        logger.info("backup alert: latest %s job %s is FAILED and was already analyzed", label, latest.id)
        return
    if latest.created_at < cutoff:
        send_alert(
            "warning",
            f"{label} quá hạn RPO {rpo_hours}h — lần backup thành công gần nhất lúc {latest.created_at.isoformat()}",
            backup_job_id=latest.id,
            cluster=cluster,
        )


def check_overdue_and_failed_backups() -> None:
    """The periodic APScheduler job. For every tracked (pool, image) plus
    the cluster metadata backup — the default cluster's (YAML-driven,
    unchanged) AND, as of multi-tenant remediation Phase 3, every
    ADDITIONAL cluster with `backup_enabled` (its own
    `backup_tracked_images`) — alerts if the latest BackupJob is FAILED,
    missing entirely, or older than the RPO."""
    policy = load_backup_policy()
    def _hours(key: str, default: int) -> int:
        try:
            return max(1, min(int(policy.get(key, default)), 24 * 365))
        except (TypeError, ValueError):
            return default

    rpo_hours = _hours("rpo_hours", RPO_HOURS)
    metadata_rpo_hours = _hours("metadata_rpo_hours", METADATA_RPO_HOURS)
    drill_rpo_hours = _hours("restore_drill_rpo_hours", RESTORE_DRILL_RPO_HOURS)
    targets: list[tuple[str, str, str, int]] = []
    for target in policy.get("tracked_images") or []:
        pool, image = target.get("pool"), target.get("image")
        if not pool or not image:
            continue
        try:
            target_rpo = max(1, min(int(target.get("rpo_hours", rpo_hours)), 24 * 365))
        except (TypeError, ValueError):
            target_rpo = rpo_hours
        targets.append((pool, image, f"{pool}/{image}", target_rpo))
    for pool, image, label, target_rpo in targets:
        _check_target(pool, image, label, utc_now() - timedelta(hours=target_rpo),
                      rpo_hours=target_rpo)
    _check_target(None, None, "metadata cụm",
                  utc_now() - timedelta(hours=metadata_rpo_hours),
                  rpo_hours=metadata_rpo_hours, job_type="metadata")

    drill_config = policy.get("restore_drill") or {}
    if all(drill_config.get(key) for key in ("pool", "image", "scratch_pool", "scratch_image")):
        _check_target(drill_config["pool"], drill_config["image"], "RestoreDrill",
                      utc_now() - timedelta(hours=drill_rpo_hours),
                      rpo_hours=drill_rpo_hours, job_type="restore_drill")

    with db.SessionLocal() as session:
        clusters = [c for c in list_active_clusters(session) if not c.is_default and c.backup_enabled]
        session.expunge_all()

    for cluster in clusters:
        cluster_rpo_hours = max(1, min(int(cluster.backup_rpo_hours or rpo_hours), 24 * 365))
        cluster_targets: list[tuple[str | None, str | None, str]] = [
            (pool, image, f"{pool}/{image} (cụm {cluster.name})")
            for pool, image in parse_tracked_images(cluster.backup_tracked_images)
        ]
        for pool, image, label in cluster_targets:
            _check_target(pool, image, label,
                          utc_now() - timedelta(hours=cluster_rpo_hours),
                          cluster=cluster, rpo_hours=cluster_rpo_hours)
        _check_target(None, None, f"metadata cụm ({cluster.name})",
                      utc_now() - timedelta(hours=metadata_rpo_hours),
                      cluster=cluster, rpo_hours=metadata_rpo_hours, job_type="metadata")
