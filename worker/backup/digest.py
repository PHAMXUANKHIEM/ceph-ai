"""BackupDigest — periodic AI-summarized digest of backup health (Story
9.5, PRD FR-14). Runs as a plain sync APScheduler job on the SAME scheduler
instance Story 9.1 built (not a new coroutine) — daily/weekly cadence
configured in `backup_policy.yaml`'s `schedule.digest_cron`.

Persists the digest text to `BackupDigestLog` (Dashboard display for it is
Story 9.6's job — see that model's own docstring) and pushes it through
`worker/backup/alerting.py`'s existing webhook (no second HTTP POST
mechanism written for this).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from shared import db
from shared.models import BackupAnomaly, BackupDigestLog, BackupJob
from worker.backup import ai_analysis, alerting
from worker.backup.cluster_scope import get_cluster
from worker.backup.policy_config import load_backup_policy

logger = logging.getLogger(__name__)

DEFAULT_PERIOD_HOURS = 24


def _gather_stats(period_start: datetime, period_end: datetime, cluster_id: str | None = None) -> dict:
    with db.SessionLocal() as session:
        jobs = (
            session.query(BackupJob)
            .filter(
                BackupJob.cluster_id == cluster_id,
                BackupJob.created_at >= period_start,
                BackupJob.created_at < period_end,
            )
            .all()
        )
        anomalies = (
            session.query(BackupAnomaly)
            .join(BackupJob, BackupAnomaly.backup_job_id == BackupJob.id)
            .filter(
                BackupJob.cluster_id == cluster_id,
                BackupAnomaly.created_at >= period_start,
                BackupAnomaly.created_at < period_end,
            )
            .all()
        )
        latest_drill = (
            session.query(BackupJob)
            .filter(BackupJob.cluster_id == cluster_id, BackupJob.job_type == "restore_drill")
            .order_by(BackupJob.created_at.desc())
            .first()
        )
        return {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "succeeded_count": sum(1 for j in jobs if j.status == "SUCCESS"),
            "failed_count": sum(1 for j in jobs if j.status == "FAILED"),
            "anomaly_count": len(anomalies),
            "anomaly_details": [a.ai_summary for a in anomalies if a.ai_summary],
            "latest_restore_drill_status": latest_drill.status if latest_drill else None,
            "latest_restore_drill_at": (
                latest_drill.created_at.isoformat() if latest_drill else None
            ),
        }


def _fallback_summary(stats: dict, period_hours: int) -> str:
    return (
        f"Trong {period_hours}h qua: {stats['succeeded_count']} job backup thành công, "
        f"{stats['failed_count']} job thất bại, {stats['anomaly_count']} bất thường phát hiện. "
        f"RestoreDrill gần nhất: {stats['latest_restore_drill_status'] or 'chưa từng chạy'}."
    )


def run_digest(cluster_id: str | None = None) -> None:
    """The APScheduler job callable — plain sync (APScheduler runs it in
    its own thread-pool executor automatically, same as
    `alerting.check_overdue_and_failed_backups`)."""
    policy = load_backup_policy()
    digest_config = (policy.get("schedule") or {}).get("digest") or {}
    period_hours = digest_config.get("period_hours", DEFAULT_PERIOD_HOURS)
    period_end = datetime.utcnow()
    period_start = period_end - timedelta(hours=period_hours)

    stats = _gather_stats(period_start, period_end, cluster_id=cluster_id)
    try:
        summary = asyncio.run(ai_analysis.summarize_digest(stats))
    except ai_analysis.AIAnalysisError:
        logger.exception("run_digest: AI digest summary failed — falling back to raw stats")
        summary = _fallback_summary(stats, period_hours)

    with db.SessionLocal() as session:
        session.add(
            BackupDigestLog(
                period_start=period_start,
                period_end=period_end,
                cluster_id=cluster_id,
                succeeded_count=stats["succeeded_count"],
                failed_count=stats["failed_count"],
                anomaly_count=stats["anomaly_count"],
                summary_text=summary,
            )
        )
        session.commit()

    logger.info("BackupDigest: %s", summary)
    if cluster_id is None:
        alerting.send_alert("info", summary)
    else:
        alerting.send_alert("info", summary, cluster=get_cluster(cluster_id))
