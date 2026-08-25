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
    "capacity_pressure": (
        "OSD_NEARFULL", "OSD_BACKFILLFULL", "OSD_FULL",
        "POOL_NEARFULL", "POOL_NEAR_FULL", "POOL_FULL", "BACKFILL_FULL",
    ),
    "volume_saturation": ("VOLUME_SATURATED:",),
    "node_resource": ("NODE_RESOURCE_HIGH:",),
    "daemon_crash": ("RECENT_CRASH", "DAEMON_CRASH"),
    "clock_skew": ("MON_CLOCK_SKEW", "CLOCK_SKEW"),
    "authentication": ("AUTH_",),
    "rgw_request": ("RGW_",),
    "rgw_encryption_key": ("RGW_", "AUTH_"),
    "rgw_permission": ("RGW_", "AUTH_"),
    "mon_operational": ("MON_",),
    "mgr_operational": ("MGR_",),
    "osd_operational": ("OSD_",),
    "mds_operational": ("MDS_",),
}

# A Loki window is commonly analysed after a fast autonomous repair has
# already closed its Incident.  RESOLVED/AUTO_FIXED therefore remain valid
# correlation truth inside CORRELATION_LOOKBACK.  REJECTED is the only
# terminal state that says the Incident must not supervise a log label.
_NON_CORRELATABLE_STATUSES = {IncidentStatus.REJECTED.value}
_OSD_RE = re.compile(r"\bosd[.\s_-]?(\d+)\b", re.IGNORECASE)
_VOLUME_CODE_RE = re.compile(r"^VOLUME_SATURATED:([^/]+)/(.+)$", re.IGNORECASE)
_NODE_RESOURCE_CODE_RE = re.compile(r"^NODE_RESOURCE_HIGH:(.+)$", re.IGNORECASE)


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


def _finding_volumes(finding: LogFinding) -> set[str]:
    try:
        entities = json.loads(finding.semantic_entities_json or "[]")
    except (TypeError, ValueError):
        return set()
    return {
        entity.removeprefix("volume:").lower()
        for entity in entities
        if isinstance(entity, str) and entity.startswith("volume:")
    }


def _incident_volumes(incident: Incident) -> set[str]:
    match = _VOLUME_CODE_RE.match(incident.ceph_code)
    return {f"{match.group(1).lower()}/{match.group(2).lower()}"} if match else set()


def _finding_hosts(finding: LogFinding) -> set[str]:
    try:
        entities = json.loads(finding.semantic_entities_json or "[]")
    except (TypeError, ValueError):
        return set()
    return {
        entity.removeprefix("host:").lower()
        for entity in entities
        if isinstance(entity, str) and entity.startswith("host:")
    }


def _incident_hosts(incident: Incident) -> set[str]:
    match = _NODE_RESOURCE_CODE_RE.match(incident.ceph_code)
    return {match.group(1).lower()} if match else set()


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
        .filter(Incident.status.notin_(_NON_CORRELATABLE_STATUSES))
        .filter(Incident.detected_at >= now - CORRELATION_LOOKBACK)
        .filter(Incident.detected_at <= now + timedelta(minutes=15))
        .order_by(Incident.detected_at.desc(), Incident.created_at.desc())
        .all()
    )
    finding_osds = _finding_osds(finding)
    finding_volumes = _finding_volumes(finding)
    finding_hosts = _finding_hosts(finding)
    for incident in candidates:
        if not family_matches_code(finding.fault_family, incident.ceph_code):
            continue
        incident_osds = _incident_osds(incident)
        if finding_osds and incident_osds and finding_osds.isdisjoint(incident_osds):
            continue
        incident_volumes = _incident_volumes(incident)
        if finding_volumes and incident_volumes and finding_volumes.isdisjoint(incident_volumes):
            continue
        matched_volumes = finding_volumes & incident_volumes
        incident_hosts = _incident_hosts(incident)
        if finding_hosts and incident_hosts and finding_hosts.isdisjoint(incident_hosts):
            continue
        matched_hosts = finding_hosts & incident_hosts
        finding.correlated_incident_id = incident.id
        finding.correlation_reason = (
            f"server:{finding.fault_family}:ceph_code={incident.ceph_code}"
            + (f":osd={','.join(sorted(finding_osds & incident_osds))}" if finding_osds & incident_osds else "")
            + (f":volume={','.join(sorted(matched_volumes))}" if matched_volumes else "")
            + (f":host={','.join(sorted(matched_hosts))}" if matched_hosts else "")
        )
        finding.correlated_at = now
        finding.correlation_evidence_json = incident.signal_evidence_json
        return incident
    return None
