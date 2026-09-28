"""Weekly read-only Ceph operations digest."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from shared.time import utc_now

from sqlalchemy import or_

from config.settings import settings
from shared import db, telegram_outbox, weekly_autonomy_report
from shared.ai_cost import summary as ai_cost_summary
from shared.clusters import list_active_clusters
from shared.models import BackupJob, CephCapacitySample, Cluster, Incident
from shared.telegram_alerts import send_ai_ops_digest_alert as _send_ai_ops_digest_alert_direct

logger = logging.getLogger(__name__)


def send_ai_ops_digest_alert(text: str, *, cluster_name: str | None = None) -> bool:
    fingerprint = hashlib.sha256(
        f"{cluster_name or 'default'}|{text}".encode("utf-8")
    ).hexdigest()[:24]
    return telegram_outbox.enqueue_alert_call_and_dispatch(
        event_id=f"ai-ops-digest:{fingerprint}",
        category="incident",
        function="send_ai_ops_digest_alert",
        args=(text,),
        cluster_name=cluster_name,
        sender=_send_ai_ops_digest_alert_direct,
    )

def _cluster_filter(column, cluster: Cluster):
    if cluster.is_default:
        return or_(column == cluster.id, column.is_(None))
    return column == cluster.id


def _autonomy_report(cluster: Cluster, now: datetime, period_days: int) -> dict | None:
    """Autonomy section (plan WP8) in its own session: a failing section
    rolls back, which must not expire the digest's already-loaded rows."""
    try:
        with db.SessionLocal() as session:
            report = weekly_autonomy_report.build(session, cluster, now=now, period_days=period_days)
            session.rollback()
            return report
    except Exception:
        logger.exception("AI Ops digest: autonomy report unavailable for cluster=%s", cluster.name)
        return None


def build_digest(*, now: datetime | None = None, period_days: int = 7,
                 reports: list[dict] | None = None) -> list[tuple[str, str]]:
    """Digest texts per cluster; ``reports`` (if given) collects the
    autonomy report dicts for the JSON artifact."""
    now = now or utc_now()
    period_days = max(1, min(int(period_days), 31))
    start = now - timedelta(days=period_days)
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
                CephCapacitySample.captured_at >= start,
            ).order_by(CephCapacitySample.captured_at.desc()).limit(100).all()
            max_capacity = max((float(row.used_percent) for row in latest), default=None)
            payloads.append((cluster.name, {
                "incidents": incidents,
                "backup_success": sum(row.status == "SUCCESS" for row in jobs),
                "backup_failed": sum(row.status == "FAILED" for row in jobs),
                "max_capacity": max_capacity,
                "cluster": cluster,
            }))
    try:
        ai = ai_cost_summary(period_days * 24, now=now)
    except Exception:
        # AI telemetry is supplementary; one malformed/temporarily
        # unavailable row must not suppress the whole weekly digest.
        logger.exception("AI Ops digest: AI telemetry unavailable")
        ai = {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0}
    messages = []
    for name, data in payloads:
        by_status: dict[str, int] = {}
        for row in data["incidents"]:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        status = ", ".join(f"{key}: {value}" for key, value in sorted(by_status.items())) or "không có incident"
        capacity = f"{data['max_capacity']:.1f}%" if data["max_capacity"] is not None else "chưa có mẫu"
        autonomy = _autonomy_report(data["cluster"], now, period_days)
        if autonomy is not None and reports is not None:
            reports.append(autonomy)
        text = "\n".join((
            f"📊 Báo cáo Ceph AIOps {period_days} ngày · {name}",
            f"Incident: {len(data['incidents'])} ({status})",
            f"Backup: {data['backup_success']} thành công · {data['backup_failed']} thất bại",
            f"Dung lượng cao nhất trong mẫu gần nhất: {capacity}",
            f"AI toàn hệ thống: {ai['calls']} lượt gọi · {ai['errors']} lỗi · {ai['input_tokens'] + ai['output_tokens']} tokens ước tính",
            *(weekly_autonomy_report.format_lines(autonomy) if autonomy is not None else ()),
            "Chỉ là báo cáo tổng hợp từ dữ liệu đã lưu; không tự thực thi thao tác.",
        ))
        messages.append((name, text))
    return messages


def write_reports(reports: list[dict], directory: str, now: datetime) -> Path | None:
    """JSON artifact of the autonomy reports; skipped when no directory is set."""
    if not directory or not reports:
        return None
    year, week, _ = now.isocalendar()
    path = Path(directory) / f"weekly-autonomy-{year}-W{week:02d}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(reports, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    except OSError:
        logger.exception("AI Ops digest: cannot write the weekly autonomy JSON to %s", path)
        return None
    return path


def run_digest() -> None:
    if not settings.ai_ops_weekly_digest_enabled:
        return
    now = utc_now()
    reports: list[dict] = []
    for cluster_name, text in build_digest(now=now, reports=reports):
        send_ai_ops_digest_alert(text, cluster_name=cluster_name)
        logger.info("AI Ops weekly digest sent for cluster=%s", cluster_name)
    write_reports(reports, settings.ai_ops_weekly_report_dir, now)
