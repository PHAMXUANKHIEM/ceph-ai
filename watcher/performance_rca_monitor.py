"""Lifecycle and Telegram delivery for deterministic performance RCA candidates."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from hashlib import sha1

from config.settings import settings
from shared import db, telegram_alerts
from shared.models import Cluster, Incident, IncidentStatus
from watcher.performance_rca import PERFORMANCE_RCA_PREFIX, report

logger = logging.getLogger(__name__)

_OPEN_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    IncidentStatus.VERIFYING.value,
    IncidentStatus.FAILED.value,
}
_ALERT_HYPOTHESES = {
    "host_resource_candidate",
    "sampled_data_osd_latency_candidate",
}


def _code_for(analysis: dict) -> str:
    identity = f"{analysis.get('pool', '')}/{analysis.get('image', '')}"
    return PERFORMANCE_RCA_PREFIX + sha1(identity.encode(), usedforsecurity=False).hexdigest()[:20]


def _channel(cluster) -> tuple[str, str, bool, bool]:
    cluster_configured = bool(cluster and cluster.telegram_bot_token and cluster.telegram_chat_id)
    if cluster_configured:
        return cluster.telegram_bot_token, cluster.telegram_chat_id, cluster.telegram_enabled, True
    return settings.telegram_incident_bot_token, settings.telegram_incident_chat_id, settings.telegram_incident_enabled, False


def _candidate_payload(analysis: dict) -> dict:
    return {
        "source": "performance_rca",
        "hypothesis": analysis.get("hypothesis"),
        "pool": analysis.get("pool"),
        "image": analysis.get("image"),
        "confidence": analysis.get("confidence"),
        "current_latency_ms": analysis.get("current_latency_ms"),
        "baseline_latency_ms": analysis.get("baseline_latency_ms"),
        "explanation": analysis.get("explanation"),
        "host_evidence": analysis.get("host_evidence") or [],
        "topology": analysis.get("topology"),
    }


def _log_excerpt(analysis: dict) -> str:
    return (
        f"Performance RCA candidate {analysis.get('pool')}/{analysis.get('image')}: "
        f"{analysis.get('hypothesis')} — {analysis.get('explanation')}"
    )


def check_and_alert(cluster_id: str, cluster=None) -> int:
    """Create/resolve RCA alert state and deliver newly detected candidates."""
    if not settings.telegram_performance_rca_enabled:
        logger.info("performance RCA: alerting is disabled")
        return 0

    if cluster is None:
        with db.SessionLocal() as lookup:
            cluster = lookup.get(Cluster, cluster_id)
            if cluster is None:
                logger.warning("performance RCA: cluster %s not found", cluster_id)
                return 0

    token, chat_id, enabled, uses_cluster_channel = _channel(cluster)
    if not (enabled and token and chat_id):
        logger.info("performance RCA: Telegram incident channel is not configured")
        return 0

    try:
        data = report(cluster, window_hours=1)
    except Exception:
        logger.exception("performance RCA: report scan failed for cluster %s", cluster_id)
        return 0
    candidates = [
        analysis for analysis in data.get("analyses", [])
        if analysis.get("hypothesis") in _ALERT_HYPOTHESES
    ]
    current = {_code_for(analysis): analysis for analysis in candidates}
    pending_delivery: list[tuple[str, dict]] = []
    now = datetime.utcnow()

    with db.SessionLocal() as session:
        open_incidents = session.query(Incident).filter(
            Incident.cluster_id == cluster_id,
            Incident.ceph_code.like(f"{PERFORMANCE_RCA_PREFIX}%"),
            Incident.status.in_(_OPEN_STATUSES),
        ).all()
        by_code = {incident.ceph_code: incident for incident in open_incidents}

        for code, incident in by_code.items():
            if code not in current:
                incident.status = IncidentStatus.RESOLVED.value

        for code, analysis in current.items():
            incident = by_code.get(code)
            if incident is None:
                incident = Incident(
                    cluster_id=cluster_id,
                    ceph_code=code,
                    status=IncidentStatus.NEW.value,
                    severity="HEALTH_WARN",
                    detected_at=now,
                    log_excerpt=_log_excerpt(analysis),
                    signal_evidence_json=json.dumps(
                        _candidate_payload(analysis), ensure_ascii=False, sort_keys=True,
                    ),
                )
                session.add(incident)
                session.flush()
                pending_delivery.append((incident.id, analysis))
            elif incident.telegram_reminded_at is None:
                pending_delivery.append((incident.id, analysis))
        session.commit()

    delivered = 0
    for incident_id, analysis in pending_delivery:
        sent = telegram_alerts.send_performance_rca_alert(
            analysis,
            cluster_name=cluster.name,
            bot_token=token if uses_cluster_channel else None,
            chat_id=chat_id if uses_cluster_channel else None,
            enabled=enabled if uses_cluster_channel else None,
        )
        if not sent:
            continue
        with db.SessionLocal() as session:
            incident = session.get(Incident, incident_id)
            if incident is not None:
                incident.telegram_reminded_at = datetime.utcnow()
                session.commit()
        delivered += 1
    return delivered
