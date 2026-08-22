"""Deterministic per-OSD disk failure risk from persisted operational evidence."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from shared import db
from shared.models import Action, CrushOsdDistribution, Incident
from watcher.device_health_monitor import DEVICE_HEALTH_EVACUATE_PREFIX, _parse_life_expectancy_min
from watcher.osd_latency_monitor import OSD_LATENCY_HIGH_PREFIX

OPEN = {"NEW", "DIAGNOSING", "PENDING_APPROVAL", "APPROVED", "EXECUTING", "VERIFYING", "FAILED"}


def _evidence(incident: Incident) -> dict:
    try:
        value = json.loads(incident.signal_evidence_json or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _osd_id(incident: Incident, evidence: dict) -> int | None:
    value = evidence.get("osd_id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    for prefix in (DEVICE_HEALTH_EVACUATE_PREFIX, OSD_LATENCY_HIGH_PREFIX):
        if (incident.ceph_code or "").startswith(prefix):
            try:
                return int(incident.ceph_code[len(prefix):].split(":", 1)[0])
            except ValueError:
                return None
    return None


def _level(score: int) -> str:
    if score >= 75:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 25:
        return "MEDIUM"
    return "LOW"


def predict(cluster_id: str, *, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=90)
    with db.SessionLocal() as session:
        osds = session.query(CrushOsdDistribution).filter_by(cluster_id=cluster_id).order_by(
            CrushOsdDistribution.osd_id).all()
        incidents = session.query(Incident).filter(
            Incident.cluster_id == cluster_id, Incident.detected_at >= cutoff,
        ).order_by(Incident.detected_at.desc()).all()
        restart_actions = session.query(Action).join(Incident, Incident.id == Action.incident_id).filter(
            Incident.cluster_id == cluster_id, Action.action_id == "restart_osd_daemon",
            Action.created_at >= now - timedelta(days=30),
        ).all()
        evidence_by_incident = {row.id: _evidence(row) for row in incidents}
        incidents_by_id = {row.id: row for row in incidents}
        restart_count: dict[int, int] = {}
        for action in restart_actions:
            incident = incidents_by_id.get(action.incident_id)
            osd_id = _osd_id(incident, evidence_by_incident.get(action.incident_id, {})) if incident else None
            if osd_id is not None:
                restart_count[osd_id] = restart_count.get(osd_id, 0) + 1

        results = []
        for osd in osds:
            score, signals, citations = 0, [], []
            for incident in incidents:
                evidence = evidence_by_incident[incident.id]
                if _osd_id(incident, evidence) != osd.osd_id:
                    continue
                active = str(incident.status) in OPEN
                code = incident.ceph_code or ""
                points = 0
                if code.startswith(DEVICE_HEALTH_EVACUATE_PREFIX):
                    life_min = _parse_life_expectancy_min(evidence.get("life_expectancy_min"))
                    days = (life_min.replace(tzinfo=None) - now).total_seconds() / 86400 if life_min else None
                    points = 75 if days is not None and days <= 7 else 60 if days is not None and days <= 30 else 45
                    if not active:
                        points = min(points, 15)
                    label = f"Ceph DeviceHealth life expectancy: {evidence.get('life_expectancy_min') or 'unknown'}"
                elif code.startswith(OSD_LATENCY_HIGH_PREFIX):
                    points = 30 if active else 12
                    label = f"OSD latency outlier ({evidence.get('commit_latency_ms', 'unknown')} ms)"
                elif any(token in code.upper() for token in ("SLOW_OP", "IO_ERROR", "READ_ERROR", "WRITE_ERROR")):
                    points = 25 if active else 10
                    label = f"Ceph health signal {code}"
                else:
                    continue
                score += points
                signals.append({"signal": label, "points": points, "active": active})
                citations.append({"source_id": f"incident:{incident.id}",
                                  "observed_at": incident.detected_at.replace(tzinfo=timezone.utc).isoformat(),
                                  "confidence": 1.0, "source_type": "persisted_incident"})
            restarts = restart_count.get(osd.osd_id, 0)
            if restarts:
                points = min(20, restarts * 5)
                score += points
                signals.append({"signal": f"OSD daemon restarted {restarts} time(s) in 30 days",
                                "points": points, "active": False})
            score = min(100, score)
            confidence = min(.9, .45 + .1 * len({item["source_type"] for item in citations}) + (.1 if restarts else 0))
            level = _level(score)
            recommendation = {
                "CRITICAL": "Validate SMART immediately and prepare an approval-gated OSD drain/replacement plan.",
                "HIGH": "Schedule SMART validation and plan a controlled OSD drain during maintenance.",
                "MEDIUM": "Increase observation frequency and compare latency/device-health on the next scans.",
                "LOW": "No persisted failure signal; continue monitoring. This is not proof that SMART is healthy.",
            }[level]
            results.append({
                "osd_id": osd.osd_id, "host": osd.host, "risk_score": score, "risk_level": level,
                "confidence": round(confidence, 2), "signals": signals, "recommendation": recommendation,
                "smart_metrics_available": any("DeviceHealth" in item["signal"] for item in signals),
                "citations": citations,
            })
        results.sort(key=lambda row: (row["risk_score"], row["osd_id"]), reverse=True)
        observed_at = max((row.updated_at for row in osds), default=None)
        source_manifest = [{"source_id": f"osd-distribution:{cluster_id}",
                            "observed_at": observed_at.replace(tzinfo=timezone.utc).isoformat() if observed_at else None,
                            "confidence": .7, "source_type": "osd_inventory"}]
        seen = {source_manifest[0]["source_id"]}
        for row in results:
            for citation in row["citations"]:
                if citation["source_id"] not in seen:
                    source_manifest.append(citation)
                    seen.add(citation["source_id"])
        return {
            "status": "ready" if osds else "unavailable", "osd_count": len(results),
            "high_risk_count": sum(row["risk_level"] in {"HIGH", "CRITICAL"} for row in results),
            "observed_at": observed_at.replace(tzinfo=timezone.utc).isoformat() if observed_at else None,
            "limitations": "A LOW score means no persisted signal, not a clean SMART test. Raw temperature/wear/reallocated-sector collection is not yet available.",
            "predictions": results,
            "_citations": source_manifest,
        }
