"""Deterministic, evidence-cited operations briefing for the Dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from shared import db
from shared.models import Incident
from watcher.capacity_failure_simulation import simulate
from watcher.capacity_forecast import forecasts
from watcher.disk_failure_prediction import predict

OPEN = {"NEW", "DIAGNOSING", "PENDING_APPROVAL", "APPROVED", "EXECUTING", "VERIFYING", "FAILED"}


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def build(cluster_id: str, *, heartbeat_stale: bool = False, now: datetime | None = None) -> dict:
    """Return at most five ranked operational priorities without invoking an LLM."""
    now = now or datetime.utcnow()
    current_start, previous_start = now - timedelta(hours=24), now - timedelta(hours=48)
    with db.SessionLocal() as session:
        rows = session.query(Incident).filter(
            Incident.cluster_id == cluster_id,
            or_(Incident.detected_at >= previous_start, Incident.status.in_(OPEN)),
        ).order_by(Incident.detected_at.desc()).all()

    current = [row for row in rows if row.detected_at >= current_start]
    previous = [row for row in rows if row.detected_at < current_start]
    opened_delta = len(current) - len(previous)
    active = [row for row in rows if row.status in OPEN]
    priorities: list[dict] = []

    def add(rank: int, severity: str, title: str, summary: str, evidence: list[dict], href: str) -> None:
        priorities.append({
            "rank": rank, "severity": severity, "title": title, "summary": summary,
            "evidence": evidence, "href": href,
        })

    if heartbeat_stale:
        add(100, "CRITICAL", "Mất telemetry từ cụm",
            "Không thể tin cậy kết luận sức khoẻ cho tới khi Watcher poll thành công trở lại.",
            [{"source_id": f"watcher-heartbeat:{cluster_id}", "observed_at": None}], "/settings")

    serious = [row for row in active if row.severity == "HEALTH_ERR" or row.status == "FAILED"]
    if serious:
        latest = serious[0]
        add(90, "CRITICAL" if latest.severity == "HEALTH_ERR" else "HIGH",
            f"{len(serious)} incident cần xử lý ngay",
            f"Gần nhất: {latest.ceph_code}; trạng thái {latest.status}.",
            [{"source_id": f"incident:{row.id}", "observed_at": _iso(row.detected_at)} for row in serious[:5]],
            f"/incidents/{latest.id}/timeline")
    elif active:
        latest = active[0]
        add(70, "MEDIUM", f"{len(active)} incident đang cần chú ý",
            f"Gần nhất: {latest.ceph_code}; theo dõi tiến trình trước khi can thiệp.",
            [{"source_id": f"incident:{row.id}", "observed_at": _iso(row.detected_at)} for row in active[:5]],
            f"/incidents/{latest.id}/timeline")

    disk = predict(cluster_id, now=now)
    risky_disks = [row for row in disk.get("predictions", []) if row["risk_level"] in {"HIGH", "CRITICAL"}]
    if risky_disks:
        worst = risky_disks[0]
        add(85, worst["risk_level"], f"osd.{worst['osd_id']} có nguy cơ hỏng",
            f"Risk {worst['risk_score']}/100, confidence {worst['confidence']:.0%}; xác minh SMART trước khi lập kế hoạch thay.",
            worst.get("citations", []) or disk.get("_citations", []), "/disk-risk")

    failure = simulate(cluster_id)
    scenarios = failure.get("scenarios", [])
    if scenarios:
        worst = scenarios[0]
        maximum = worst.get("max_osd_projected_percent", 0)
        if maximum >= 80:
            severity = "CRITICAL" if maximum >= 95 else "HIGH" if maximum >= 90 else "MEDIUM"
            add(80, severity, f"Mất {worst['domain_type']} {worst['domain_name']} gây áp lực dung lượng",
                f"OSD còn lại cao nhất dự kiến {maximum:.1f}%; đây là mô phỏng capacity, không chứng minh data availability.",
                failure.get("_citations", []), "/capacity-failure-simulation")

    capacity = forecasts(cluster_id)
    approaching = []
    for item in capacity.get("forecasts", []):
        for threshold, date_text in item.get("thresholds", {}).items():
            if date_text:
                days = (datetime.fromisoformat(date_text) - now).total_seconds() / 86400
                if 0 <= days <= 30:
                    approaching.append((days, threshold, item))
    if approaching:
        days, threshold, item = min(approaching, key=lambda value: value[0])
        add(75, "HIGH" if days <= 7 else "MEDIUM",
            f"{item['entity_name']} tiến gần {threshold}%",
            f"Dự kiến còn {days:.1f} ngày theo xu hướng đã lưu ({item['sample_count']} mẫu).",
            [{"source_id": f"capacity-series:{cluster_id}:{item['entity_type']}:{item['entity_name']}",
              "observed_at": item["thresholds"][threshold]}], "/capacity-forecast")

    if not priorities:
        add(10, "LOW", "Chưa có vấn đề cần can thiệp",
            "Không có incident mở hoặc tín hiệu rủi ro cao trong evidence hiện có; tiếp tục quan sát.",
            disk.get("_citations", []), "/ai-copilot")

    priorities.sort(key=lambda item: item["rank"], reverse=True)
    trend = "tăng" if opened_delta > 0 else "giảm" if opened_delta < 0 else "không đổi"
    return {
        "generated_at": _iso(now), "priorities": priorities[:5], "priority_count": len(priorities),
        "active_incident_count": len(active), "incidents_24h": len(current),
        "incident_delta": opened_delta, "incident_trend": trend,
        "disk_coverage": disk.get("osd_count", 0),
        "smart_metrics_available": any(row.get("smart_metrics_available") for row in disk.get("predictions", [])),
    }
