"""Deterministic grouping of related Ceph incidents for AI context.

This module deliberately does not ask the model to decide whether two
incidents are related.  It uses the cluster, a bounded time window, the Ceph
code family, and concrete entities extracted from trusted detector evidence.
The resulting root id is persisted on each Incident and can be used by the
AI prompt as additional context without changing the remediation gate.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import or_

from shared.models import Incident, IncidentStatus

GROUP_LOOKBACK = timedelta(hours=6)
MAX_GROUP_CANDIDATES = 200
MAX_GROUP_CONTEXT_INCIDENTS = 8

_NON_GROUPABLE_STATUSES = {IncidentStatus.REJECTED.value}
_OSD_RE = re.compile(r"\bosd[.\s_-]?(\d+)\b", re.IGNORECASE)
_VOLUME_RE = re.compile(r"\bvolume[=: ]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.IGNORECASE)
_HOST_RE = re.compile(r"\bhost[=: ]+([A-Za-z0-9_.:-]+)", re.IGNORECASE)

_FAMILY_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("disk_io", ("DEVICE_HEALTH_", "BLUESTORE_DISK_", "OSD_UNREACHABLE")),
    ("bluestore_slow_ops", ("BLUESTORE_SLOW_OP_ALERT", "SLOW_OPS", "OSD_SLOW_PING_TIME", "OSD_LATENCY_HIGH:")),
    ("network_heartbeat", ("OSD_DOWN", "MON_DOWN", "MGR_DOWN", "OSD_HOST_DOWN")),
    ("pg_peering", ("PG_", "OBJECT_")),
    ("capacity_pressure", ("OSD_NEARFULL", "OSD_BACKFILLFULL", "OSD_FULL", "POOL_NEARFULL", "POOL_NEAR_FULL", "POOL_FULL", "BACKFILL_FULL")),
    ("volume_saturation", ("VOLUME_SATURATED:",)),
    ("node_resource", ("NODE_RESOURCE_HIGH:",)),
    ("daemon_crash", ("RECENT_CRASH", "DAEMON_CRASH")),
    ("clock_skew", ("MON_CLOCK_SKEW", "CLOCK_SKEW")),
    ("authentication", ("AUTH_",)),
    ("rgw_request", ("RGW_",)),
    ("mon_operational", ("MON_",)),
    ("mgr_operational", ("MGR_",)),
    ("osd_operational", ("OSD_",)),
    ("mds_operational", ("MDS_",)),
)


def _family(code: str) -> str | None:
    for name, prefixes in _FAMILY_PREFIXES:
        if any(code == prefix or code.startswith(prefix) for prefix in prefixes):
            return name
    return None


def _evidence(incident: Incident) -> dict:
    try:
        value = json.loads(incident.signal_evidence_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _entities(incident: Incident) -> set[str]:
    """Extract only concrete entities, never arbitrary log words."""
    evidence = _evidence(incident)
    text = f"{incident.ceph_code}\n{incident.log_excerpt or ''}"
    entities = {f"osd:{match}" for match in _OSD_RE.findall(text)}
    for key in ("osd_id", "osd_ids"):
        values = evidence.get(key)
        values = values if isinstance(values, list) else [values]
        entities.update(f"osd:{value}" for value in values if str(value).isdigit())

    volume_match = re.match(r"^VOLUME_SATURATED:([^/]+)/(.+)$", incident.ceph_code, re.IGNORECASE)
    if volume_match:
        entities.add(f"volume:{volume_match.group(1)}/{volume_match.group(2)}".lower())
    entities.update(f"volume:{match.lower()}" for match in _VOLUME_RE.findall(text))

    for key in ("host", "hostname", "node"):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            entities.add(f"host:{value.strip().lower()}")
    entities.update(f"host:{match.lower()}" for match in _HOST_RE.findall(text))
    return entities


def incidents_are_related(left: Incident, right: Incident) -> bool:
    """Return True only for exact codes or same family plus shared entity."""
    if left.ceph_code == right.ceph_code:
        return True
    left_family = _family(left.ceph_code)
    return bool(left_family and left_family == _family(right.ceph_code) and _entities(left) & _entities(right))


def _same_cluster(session, incident: Incident):
    query = session.query(Incident).filter(Incident.id != incident.id)
    if incident.cluster_id is None:
        return query.filter(Incident.cluster_id.is_(None))
    return query.filter(Incident.cluster_id == incident.cluster_id)


def assign_incident_group(session, incident: Incident) -> str:
    """Assign `incident` to a stable root group and merge matching roots."""
    if not incident.id:
        session.flush()
    window_start = incident.detected_at - GROUP_LOOKBACK
    window_end = incident.detected_at + timedelta(minutes=15)
    candidates = (
        _same_cluster(session, incident)
        .filter(Incident.status.notin_(_NON_GROUPABLE_STATUSES))
        .filter(Incident.detected_at >= window_start, Incident.detected_at <= window_end)
        .order_by(Incident.detected_at.asc(), Incident.created_at.asc(), Incident.id.asc())
        .limit(MAX_GROUP_CANDIDATES)
        .all()
    )
    related = [candidate for candidate in candidates if incidents_are_related(incident, candidate)]
    if not related:
        incident.group_root_incident_id = incident.id
        return incident.id

    root_ids = {
        incident.group_root_incident_id or incident.id,
        *(candidate.group_root_incident_id or candidate.id for candidate in related),
    }
    roots = [session.get(Incident, root_id) for root_id in root_ids]
    roots = [root for root in roots if root is not None]
    canonical = min(roots, key=lambda row: (row.detected_at, row.created_at, row.id))
    canonical_id = canonical.id

    members = (
        session.query(Incident)
        .filter(Incident.detected_at >= window_start, Incident.detected_at <= window_end)
        .filter(or_(Incident.id.in_(root_ids), Incident.group_root_incident_id.in_(root_ids)))
        .all()
    )
    for member in members:
        member.group_root_incident_id = canonical_id
    incident.group_root_incident_id = canonical_id
    return canonical_id


def build_group_context(session, incident_id: str, *, limit: int = MAX_GROUP_CONTEXT_INCIDENTS) -> dict:
    """Build bounded, non-secret context for the diagnosis prompt."""
    incident = session.get(Incident, incident_id)
    if incident is None:
        return {"root_incident_id": incident_id, "related_incidents": []}
    root_id = incident.group_root_incident_id or incident.id
    context_limit = max(0, min(limit, MAX_GROUP_CONTEXT_INCIDENTS))
    root = session.get(Incident, root_id) if root_id != incident.id else None
    rows = [root] if root is not None and context_limit else []
    remaining = max(0, context_limit - len(rows))
    related_rows = (
        session.query(Incident)
        .filter(Incident.detected_at >= incident.detected_at - GROUP_LOOKBACK)
        .filter(Incident.detected_at <= incident.detected_at + timedelta(minutes=15))
        .filter(Incident.group_root_incident_id == root_id)
        .filter(Incident.id != incident.id)
        .filter(Incident.id != root_id)
        .order_by(Incident.detected_at.desc(), Incident.created_at.desc(), Incident.id.desc())
        .limit(remaining)
        .all()
    )
    rows.extend(related_rows)
    return {
        "root_incident_id": root_id,
        "related_incidents": [
            {
                "incident_id": row.id,
                "ceph_code": row.ceph_code,
                "status": row.status,
                "severity": row.severity,
                "detected_at": row.detected_at.isoformat() if row.detected_at else None,
                "diagnosis_text": (row.diagnosis_text or "")[:1000],
                "log_excerpt": (row.log_excerpt or "")[:1000],
            }
            for row in rows
        ],
    }
