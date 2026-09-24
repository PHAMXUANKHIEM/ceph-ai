"""Read-only aggregation of the evidence already stored by the AIOps flows.

This module deliberately does not introduce a second event store.  The source
tables remain authoritative; the returned rows are a normalized view for the
Dashboard/API and can be rebuilt for any cluster/time window.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import or_

from shared.models import (
    Action,
    AuditEntry,
    Incident,
    IncidentTimelineEvent,
    LogFinding,
    LogIngestRun,
    ObjectStorageAuditEntry,
)

DEFAULT_LIMIT = 200
MAX_LIMIT = 500
_SENSITIVE_KEY = re.compile(r"(?:password|secret|token|api.?key|credential|private.?key)", re.I)


def _safe_json(raw: str | None):
    try:
        value = json.loads(raw or "null")
    except (TypeError, ValueError):
        return None

    def scrub(item):
        if isinstance(item, dict):
            return {
                key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else scrub(val)
                for key, val in item.items()
            }
        if isinstance(item, list):
            return [scrub(val) for val in item]
        return item

    return scrub(value)


def _scope_incident(column, cluster_id: str, is_default: bool):
    # Rows created before multi-cluster support have NULL cluster_id and are
    # legacy rows for the default cluster, never an unscoped global event.
    return or_(column == cluster_id, column.is_(None)) if is_default else column == cluster_id


def _window(query, column, since: datetime | None, until: datetime | None):
    if since is not None:
        query = query.filter(column >= since)
    if until is not None:
        query = query.filter(column <= until)
    return query


def _at(value: datetime) -> str:
    return value.isoformat()


def _event(*, event_id: str, at: datetime, kind: str, source: str, source_id: str,
           cluster_id: str, summary: str, severity: str = "INFO", actor: str = "system",
           status: str | None = None, evidence: dict | None = None) -> dict:
    return {
        "id": event_id,
        "at": _at(at),
        "kind": kind,
        "source": source,
        "source_id": source_id,
        "cluster_id": cluster_id,
        "severity": severity or "INFO",
        "actor": actor,
        "status": status,
        "summary": summary,
        "evidence": evidence or {},
    }


def _audit_kind(event_type: str, actor: str) -> str:
    normalized = event_type.lower()
    if "approv" in normalized:
        return "approval"
    if "execut" in normalized or "command" in normalized:
        return "command"
    return "operator_event" if actor != "system" else "audit_event"


def build_unified_timeline(
    session,
    *,
    cluster_id: str,
    is_default: bool,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Return a deterministic, newest-first event view for one cluster.

    Only persisted evidence is used.  In particular, an Action's
    ``updated_at`` is not presented as a command event: a command event is
    emitted only when the executor persisted the exact ``executed_at``.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    events: list[dict] = []
    incident_scope = _scope_incident(Incident.cluster_id, cluster_id, is_default)

    incidents = _window(
        session.query(Incident).filter(incident_scope), Incident.detected_at, since, until
    ).order_by(Incident.detected_at.desc(), Incident.id.desc()).limit(limit).all()
    for incident in incidents:
        events.append(_event(
            event_id=f"incident:{incident.id}:detected", at=incident.detected_at,
            kind="health_transition", source="incident", source_id=incident.id,
            cluster_id=cluster_id, severity=incident.severity or "INFO", actor="watcher",
            status=incident.status, summary=f"{incident.ceph_code} — {incident.status}",
            evidence={
                "incident_id": incident.id,
                "ceph_code": incident.ceph_code,
                "signal_evidence": _safe_json(incident.signal_evidence_json),
            },
        ))

    action_query = session.query(Action, Incident).join(
        Incident, Incident.id == Action.incident_id
    ).filter(incident_scope)
    # A proposal and its command are separate timeline points.  Include an
    # Action when either point intersects the window; otherwise a command
    # executed now would disappear merely because approval happened earlier.
    action_times = []
    if since is not None:
        action_times.append(or_(Action.created_at >= since, Action.executed_at >= since))
    if until is not None:
        action_times.append(or_(Action.created_at <= until, Action.executed_at <= until))
    if action_times:
        action_query = action_query.filter(*action_times)
    actions = action_query.order_by(Action.created_at.desc(), Action.id.desc()).limit(limit).all()
    for action, incident in actions:
        proposal_in_window = (since is None or action.created_at >= since) and (
            until is None or action.created_at <= until
        )
        if proposal_in_window:
            events.append(_event(
                event_id=f"action:{action.id}:proposal", at=action.created_at,
                kind="proposal", source="action", source_id=action.id, cluster_id=cluster_id,
                severity="WARNING" if action.status in {"PENDING_APPROVAL", "FAILED"} else "INFO",
                actor="system", status=action.status,
                summary=f"Đề xuất {action.action_id} ({action.status})",
                evidence={
                    "incident_id": incident.id, "action_id": action.action_id,
                    "classification": action.classification,
                    "target_nodes": _safe_json(action.target_nodes),
                },
            ))
        if action.executed_at is not None and (since is None or action.executed_at >= since) \
                and (until is None or action.executed_at <= until):
            events.append(_event(
                event_id=f"action:{action.id}:command", at=action.executed_at,
                kind="command", source="action", source_id=action.id,
                cluster_id=cluster_id,
                severity="CRITICAL" if action.status in {"FAILED", "INCONCLUSIVE"} else "INFO",
                actor="worker", status=action.status,
                summary=f"Lệnh {action.action_id} — {action.status}",
                evidence={"incident_id": incident.id, "action_id": action.action_id},
            ))

    timeline_events = _window(
        session.query(IncidentTimelineEvent, Incident)
        .join(Incident, Incident.id == IncidentTimelineEvent.incident_id)
        .filter(incident_scope),
        IncidentTimelineEvent.created_at, since, until,
    ).order_by(IncidentTimelineEvent.created_at.desc(), IncidentTimelineEvent.id.desc()).limit(limit).all()
    mirrored_audit_ids = {
        event.source_id for event, _incident in timeline_events
        if event.source_type == "audit" and event.source_id
    }
    for event, incident in timeline_events:
        events.append(_event(
            event_id=f"timeline:{event.id}", at=event.created_at,
            kind="post_check" if "verif" in event.event_type.lower() else "lifecycle",
            source="incident_timeline", source_id=event.id, cluster_id=cluster_id,
            actor=event.actor, status=incident.status,
            summary=event.event_type,
            evidence={
                "incident_id": incident.id,
                "action_id": event.action_id,
                "source_type": event.source_type,
                "source_id": event.source_id,
                "payload": _safe_json(event.evidence_json),
            },
        ))

    audits = _window(
        session.query(AuditEntry, Incident)
        .join(Incident, Incident.id == AuditEntry.incident_id)
        .filter(incident_scope),
        AuditEntry.created_at, since, until,
    ).order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc()).limit(limit).all()
    for audit, incident in audits:
        if audit.id in mirrored_audit_ids:
            continue
        events.append(_event(
            event_id=f"audit:{audit.id}", at=audit.created_at,
            kind=_audit_kind(audit.event_type, audit.actor),
            source="audit", source_id=audit.id, cluster_id=cluster_id,
            actor=audit.actor, status=incident.status, summary=audit.event_type,
            evidence={"incident_id": incident.id, "action_id": audit.action_id},
        ))

    findings = _window(
        session.query(LogFinding), LogFinding.created_at, since, until,
    ).filter(LogFinding.cluster_id == cluster_id).order_by(
        LogFinding.created_at.desc(), LogFinding.id.desc()
    ).limit(limit).all()
    for finding in findings:
        events.append(_event(
            event_id=f"finding:{finding.id}", at=finding.created_at,
            kind="metric_anomaly" if finding.verdict != "FINDING" else "alert",
            source="log_finding", source_id=finding.id, cluster_id=cluster_id,
            severity=finding.severity or "INFO", actor="log-intelligence",
            status=finding.status,
            summary=finding.title or finding.fault_family or "Log finding",
            evidence={
                "verdict": finding.verdict, "confidence": finding.confidence,
                "ingest_run_id": finding.ingest_run_id,
                "evidence_pattern_ids": _safe_json(finding.evidence_pattern_ids_json),
                "correlated_incident_id": finding.correlated_incident_id,
            },
        ))

    runs = _window(
        session.query(LogIngestRun).filter(LogIngestRun.cluster_id == cluster_id),
        LogIngestRun.created_at, since, until,
    ).order_by(LogIngestRun.created_at.desc(), LogIngestRun.id.desc()).limit(limit).all()
    for run in runs:
        events.append(_event(
            event_id=f"log-run:{run.id}", at=run.created_at,
            kind="evidence_collection", source="log_ingest", source_id=run.id,
            cluster_id=cluster_id, severity="WARNING" if run.status != "OK" else "INFO",
            actor="watcher", status=run.status,
            summary=f"Thu thập log — {run.status}",
            evidence={
                "source": run.source, "window_start": _at(run.window_start),
                "window_end": _at(run.window_end), "hosts_scanned": run.hosts_scanned,
                "hosts_failed": run.hosts_failed, "error_message": run.error_message,
            },
        ))

    rgw_audits = _window(
        session.query(ObjectStorageAuditEntry).filter(ObjectStorageAuditEntry.cluster_id == cluster_id),
        ObjectStorageAuditEntry.created_at, since, until,
    ).order_by(ObjectStorageAuditEntry.created_at.desc(), ObjectStorageAuditEntry.id.desc()).limit(limit).all()
    for entry in rgw_audits:
        events.append(_event(
            event_id=f"rgw-audit:{entry.id}", at=entry.created_at,
            kind="operator_event", source="rgw_audit", source_id=entry.id,
            cluster_id=cluster_id, severity="WARNING" if entry.result != "success" else "INFO",
            actor=entry.actor, status=entry.result,
            summary=f"RGW {entry.action}: {entry.target_id}",
            evidence={
                "target_type": entry.target_type, "action": entry.action,
                "result": entry.result, "completed_at": _at(entry.completed_at) if entry.completed_at else None,
                "error_message": entry.error_message,
            },
        ))

    # Stable ordering makes late-arriving and same-second events reproducible.
    events.sort(key=lambda item: (item["at"], item["id"]), reverse=True)
    return events[:limit]
