"""Weekly read-only Ceph operations digest."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_

from config.settings import settings
from shared import db
from shared.ai_cost import summary as ai_cost_summary
from shared.clusters import list_active_clusters
from shared.models import BackupJob, CephCapacitySample, Cluster, Incident
from shared.telegram_alerts import send_ai_ops_digest_alert

logger = logging.getLogger(__name__)


def _cluster_filter(column, cluster: Cluster):
    if cluster.is_default:
        return or_(column == cluster.id, column.is_(None))
    return column == cluster.id


def build_digest(*, now: datetime | None = None, period_days: int = 7) -> list[tuple[str, str]]:
    now = now or datetime.utcnow()
    start = now - timedelta(days=max(1, min(period_days, 31)))
    with db.SessionLocal() as session:
        clusters = list_active_clusters(session)
        payloads = []
        for cluster in clusters:
            incidents = session.query(Incident).filter(
                _cluster_filter(Incident.cluster_id, cluster),
                Incident.detected_at >= start,
            ).all()
            jobs = session.query(BackupJob).filter(
                _cluster_filter(BackupJob.cluster_id, cluster),
                BackupJob.created_at >= start,
            ).all()
            latest = session.query(CephCapacitySample).filter(
                CephCapacitySample.cluster_id == cluster.id,
            ).order_by(CephCapacitySample.captured_at.desc()).limit(100).all()
            max_capacity = max((float(row.used_percent) for row in latest), default=None)
            payloads.append((cluster.name, {
                "incidents": incidents,
                "backup_success": sum(row.status == "SUCCESS" for row in jobs),
                "backup_failed": sum(row.status == "FAILED" for row in jobs),
                "max_capacity": max_capacity,
            }))
    ai = ai_cost_summary(period_days * 24, now=now)
    messages = []
    for name, data in payloads:
        by_status = {}
        for row in data["incidents"]:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        status = ", ".join(f"{key}: {value}" for key, value in sorted(by_status.items())) or "không có incident"
        capacity = f"{data['max_capacity']:.1f}%" if data["max_capacity"] is not None else "chưa có mẫu"
        text = "\n".join((
            f"📊 Báo cáo Ceph AIOps 7 ngày · {name}",
            f"Incident: {len(data['incidents'])} ({status})",
            f"Backup: {data['backup_success']} thành công · {data['backup_failed']} thất bại",
            f"Dung lượng cao nhất trong mẫu gần nhất: {capacity}",
            f"AI: {ai['calls']} lượt gọi · {ai['errors']} lỗi · {ai['input_tokens'] + ai['output_tokens']} tokens ước tính",
            "Chỉ là báo cáo tổng hợp từ dữ liệu đã lưu; không tự thực thi thao tác.",
        ))
        messages.append((name, text))
    return messages


def run_digest() -> None:
    if not settings.ai_ops_weekly_digest_enabled:
        return
    for cluster_name, text in build_digest():
        send_ai_ops_digest_alert(text, cluster_name=cluster_name)
        logger.info("AI Ops weekly digest sent for cluster=%s", cluster_name)
