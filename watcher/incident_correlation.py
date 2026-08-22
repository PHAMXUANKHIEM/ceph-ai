"""Deterministic correlation between AI log findings and Ceph incidents."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import or_

from shared.models import Cluster, Incident, IncidentStatus, LogFinding

CORRELATION_LOOKBACK = timedelta(hours=6)

_FAMILY_CODES: dict[str, tuple[str, ...]] = {
    "disk_io": ("DEVICE_HEALTH_", "BLUESTORE_DISK_", "OSD_UNREACHABLE"),
    "bluestore_slow_ops": (
        "BLUESTORE_SLOW_OP_ALERT", "SLOW_OPS", "OSD_SLOW_PING_TIME", "OSD_LATENCY_HIGH:"
    ),
    "network_heartbeat": ("OSD_DOWN", "MON_DOWN", "MGR_DOWN", "OSD_HOST_DOWN"),
    "pg_peering": ("PG_", "OBJECT_"),
    "capacity_pressure": ("OSD_NEARFULL", "OSD_FULL", "POOL_NEARFULL", "POOL_FULL", "BACKFILL_FULL"),
    "daemon_crash": ("RECENT_CRASH", "DAEMON_CRASH"),
    "clock_skew": ("MON_CLOCK_SKEW", "CLOCK_SKEW"),
    "authentication": ("AUTH_",),
    "rgw_request": ("RGW_",),
}

_TERMINAL_STATUSES = {
    IncidentStatus.RESOLVED.value,
    IncidentStatus.REJECTED.value,
    IncidentStatus.AUTO_FIXED.value,
}
_OSD_RE = re.compile(r"\bosd[.\s_-]?(\d+)\b", re.IGNORECASE)


def family_matches_code(fault_family: str | None, ceph_code: str) -> bool:
    if not fault_family or ceph_code.startswith("LOG_INTEL_"):
        return False
    return any(ceph_code == prefix or ceph_code.startswith(prefix) for prefix in _FAMILY_CODES.get(fault_family, ()))


def _finding_osds(finding: LogFinding) -> set[str]:
    try:
        entities = json.loads(finding.semantic_entities_json or "[]")
    except (TypeError, ValueError):
        return set()
    result: set[str] = set()
    for entity in entities:
        if not isinstance(entity, str) or not entity.startswith("daemon:"):
            continue
        match = _OSD_RE.search(entity.removeprefix("daemon:"))
        if match:
            result.add(match.group(1))
    return result


def _incident_osds(incident: Incident) -> set[str]:
    text = f"{incident.ceph_code}\n{incident.log_excerpt or ''}"
    return set(_OSD_RE.findall(text))


def _same_cluster_filter(cluster_id: str, is_default: bool):
    if is_default:
        return or_(Incident.cluster_id == cluster_id, Incident.cluster_id.is_(None))
    return Incident.cluster_id == cluster_id


def correlate_finding(session, finding: LogFinding, *, now: datetime | None = None) -> Incident | None:
    """Attach the best active health incident, or leave the finding unlinked."""
    if not finding.fault_family:
        return None
    now = now or datetime.utcnow()
    cluster = session.get(Cluster, finding.cluster_id)
    is_default = bool(cluster and cluster.is_default)
    candidates = (
        session.query(Incident)
        .filter(_same_cluster_filter(finding.cluster_id, is_default))
        .filter(Incident.status.notin_(_TERMINAL_STATUSES))
        .filter(Incident.detected_at >= now - CORRELATION_LOOKBACK)
        .filter(Incident.detected_at <= now + timedelta(minutes=15))
        .order_by(Incident.detected_at.desc(), Incident.created_at.desc())
        .all()
    )
    finding_osds = _finding_osds(finding)
    for incident in candidates:
        if not family_matches_code(finding.fault_family, incident.ceph_code):
            continue
        incident_osds = _incident_osds(incident)
        if finding_osds and incident_osds and finding_osds.isdisjoint(incident_osds):
            continue
        finding.correlated_incident_id = incident.id
        finding.correlation_reason = (
            f"server:{finding.fault_family}:ceph_code={incident.ceph_code}"
            + (f":osd={','.join(sorted(finding_osds & incident_osds))}" if finding_osds & incident_osds else "")
        )
        finding.correlated_at = now
        finding.correlation_evidence_json = incident.signal_evidence_json
        return incident
    return None
