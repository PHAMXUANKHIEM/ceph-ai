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
from typing import TYPE_CHECKING

import httpx

from config.settings import settings
from shared import db
from shared.clusters import list_active_clusters
from shared.models import BackupJob
from shared.telegram_client import TelegramSendError, send_telegram_message
from worker.backup.cluster_scope import parse_tracked_images
from worker.backup.policy_config import load_backup_policy

if TYPE_CHECKING:
    from shared.models import Cluster

logger = logging.getLogger(__name__)

RPO_HOURS = 24
WEBHOOK_TIMEOUT_SECONDS = 10
_MAX_TELEGRAM_MESSAGE_CHARS = 700

# Prefixed onto every Telegram message so a severity is readable at a
# glance in a phone notification preview, before the operator even opens
# the chat — the generic webhook payload already carries severity as a
# separate JSON field, but a Telegram message is just one text blob.
_TELEGRAM_SEVERITY_PREFIX = {
    "critical": "\U0001f534 CRITICAL",  # red circle
    "warning": "\U0001f7e1 WARNING",  # yellow circle
    "info": "ℹ️ INFO",  # info symbol
}


def _send_telegram_alert(
    severity: str, message: str, backup_job_id: str | None, cluster: "Cluster | None" = None
) -> None:
    """Best-effort, same posture as the webhook POST below — a Telegram
    delivery failure (bad token, chat id the bot was never added to,
    network) is logged and swallowed, never allowed to fail the backup/
    drill run that triggered this alert. 2026-08-06: this Backup channel
    has its own independent Bot Token/Chat ID (no longer shared with
    Lỗi cụm/Phần cứng) — checked here (along with `telegram_backup_enabled`,
    2026-08-07's separate on/off toggle) rather than relying on
    send_telegram_message's own "missing config" error, so an operator who
    simply hasn't set up (or has paused) this channel never sees a log
    entry about it failing.

    `cluster` (multi-tenant remediation Phase 3): `None` means the default
    cluster (unchanged — the global `telegram_backup_*` channel above). An
    additional cluster routes to its OWN Phase 2 channel
    (`cluster.telegram_bot_token/chat_id`) instead — reusing the same
    fields `dashboard/telegram_approval_bot.py::channels_for_incident`
    already uses for its Incidents/RISKY approvals. If that cluster has no
    channel configured, this is skipped (logged only) rather than falling
    back to the global Backup channel — never leak one cluster's backup
    status into another's ops channel, same narrowing posture Phase 2
    itself established."""
    if cluster is not None:
        if not cluster.telegram_enabled or not cluster.telegram_bot_token or not cluster.telegram_chat_id:
            logger.info(
                "send_alert: cluster %s has no Telegram channel configured — skipping Telegram delivery",
                cluster.id,
            )
            return
        bot_token, chat_id, cluster_name = cluster.telegram_bot_token, cluster.telegram_chat_id, cluster.name.strip()
    else:
        if (
            not settings.telegram_backup_enabled
            or not settings.telegram_backup_bot_token
            or not settings.telegram_backup_chat_id
        ):
            return
        bot_token, chat_id = settings.telegram_backup_bot_token, settings.telegram_backup_chat_id
        cluster_name = settings.cluster_name.strip()

    prefix = _TELEGRAM_SEVERITY_PREFIX.get(severity, severity.upper())
    compact_message = "\n".join(
        " ".join(line.split()) for line in message.splitlines() if line.strip()
    )
    if len(compact_message) > _MAX_TELEGRAM_MESSAGE_CHARS:
        compact_message = compact_message[: _MAX_TELEGRAM_MESSAGE_CHARS - 1].rstrip() + "…"
    text = f"{prefix}\n{compact_message}"
    if backup_job_id:
        text += f"\n🆔 Job: {backup_job_id[:8]}"
    # 2026-08-07: same cluster-name prefix as shared/telegram_alerts.py's
    # _with_cluster_prefix — this module has its own independent Backup
    # channel/send path (see module docstring), so it needs its own copy
    # rather than importing that helper across the watcher/worker boundary.
    if cluster_name:
        text = f"\U0001f4cd Cụm: {cluster_name}\n{text}"
    try:
        send_telegram_message(bot_token, chat_id, text)
    except TelegramSendError:
        logger.exception("send_alert: Telegram delivery failed — alert already logged above")


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
) -> None:
    """Shared by the default cluster's loop and the additional-clusters'
    loop below — `cluster_id` is ALWAYS filtered explicitly (never left
    implicit), same correctness requirement as every other BackupJob query
    in this codebase once more than one cluster can produce rows for a
    same-named pool/image (Phase 3)."""
    cluster_id = cluster.id if cluster is not None else None
    with db.SessionLocal() as session:
        query = session.query(BackupJob).filter(BackupJob.cluster_id == cluster_id)
        if pool is not None:
            query = query.filter(BackupJob.pool == pool, BackupJob.image == image, BackupJob.job_type != "restore_drill")
        else:
            query = query.filter(BackupJob.job_type == "metadata")
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
    try:
        rpo_hours = max(1, min(int(policy.get("rpo_hours", RPO_HOURS)), 24 * 365))
    except (TypeError, ValueError):
        rpo_hours = RPO_HOURS
    cutoff = datetime.utcnow() - timedelta(hours=rpo_hours)

    targets: list[tuple[str | None, str | None, str]] = [
        (t.get("pool"), t.get("image"), f"{t.get('pool')}/{t.get('image')}")
        for t in (policy.get("tracked_images") or [])
        if t.get("pool") and t.get("image")
    ]
    targets.append((None, None, "metadata cụm"))

    for pool, image, label in targets:
        _check_target(pool, image, label, cutoff, rpo_hours=rpo_hours)

    with db.SessionLocal() as session:
        clusters = [c for c in list_active_clusters(session) if not c.is_default and c.backup_enabled]
        session.expunge_all()

    for cluster in clusters:
        cluster_targets: list[tuple[str | None, str | None, str]] = [
            (pool, image, f"{pool}/{image} (cụm {cluster.name})")
            for pool, image in parse_tracked_images(cluster.backup_tracked_images)
        ]
        cluster_targets.append((None, None, f"metadata cụm ({cluster.name})"))
        for pool, image, label in cluster_targets:
            _check_target(pool, image, label, cutoff, cluster=cluster, rpo_hours=rpo_hours)
