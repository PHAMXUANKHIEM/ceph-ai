"""Store and summarise pre-diagnosis evidence for incidents (autonomy plan WP3.3)."""

from __future__ import annotations

from shared import deterministic_triage, incident_events
from shared.deterministic_triage import Triage
from shared.evidence_collectors import OK, EvidenceResult
from shared.investigation_runbooks import InvestigationPlan
from shared.models import IncidentEvidence

EVENT_COLLECTED = "evidence_collected"
EVENT_TRIAGED = "triage_concluded"
SKIPPED_CONTEXT = "skipped_context"
SKIPPED_FLAPPING = "skipped_flapping"


def store(session, incident_id: str, plan: InvestigationPlan, results: list[EvidenceResult]) -> dict:
    """Persist one investigation and add a timeline event; returns its summary."""
    for result in results:
        session.add(IncidentEvidence(
            incident_id=incident_id, runbook=plan.runbook, collector_id=result.collector_id,
            target=result.target[:255], status=result.status, command=result.command or None,
            output_redacted=result.output or None, truncated=result.truncated, duration_ms=result.duration_ms,
        ))
    for skipped in plan.skipped:
        session.add(IncidentEvidence(
            incident_id=incident_id, runbook=plan.runbook, collector_id=skipped["collector_id"],
            target="-", status=SKIPPED_CONTEXT, output_redacted=skipped["reason"],
        ))
    summary = {
        "runbook": plan.runbook,
        "ok": sum(1 for r in results if r.status == OK),
        "failed": sum(1 for r in results if r.status != OK),
        "skipped": len(plan.skipped),
        "duration_ms": sum(r.duration_ms for r in results),
    }
    incident_events.record(session, incident_id=incident_id, event_type=EVENT_COLLECTED, actor="watcher",
                           evidence=summary)
    return summary


def record_triage(session, incident_id: str, ceph_code: str | None) -> Triage:
    """Apply the deterministic rules (WP3.4) to the stored evidence and put
    the conclusion on the timeline; UNKNOWN is recorded too, so coverage of
    the rules can be measured."""
    session.flush()
    result = deterministic_triage.triage(ceph_code, for_incident(session, incident_id))
    incident_events.record(session, incident_id=incident_id, event_type=EVENT_TRIAGED, actor="watcher",
                           evidence=result.as_dict())
    return result


def mark_flapping(session, incident_id: str) -> None:
    """A flapping host's repeat incident is not investigated again, but is
    marked so the scanner does not pick it up on every tick."""
    session.add(IncidentEvidence(incident_id=incident_id, runbook="-", collector_id="-", target="-",
                                 status=SKIPPED_FLAPPING, output_redacted="host đang chập chờn (WP1.1)"))


def for_incident(session, incident_id: str) -> list[IncidentEvidence]:
    return (session.query(IncidentEvidence).filter(IncidentEvidence.incident_id == incident_id)
            .order_by(IncidentEvidence.created_at, IncidentEvidence.collector_id).all())


def summary_lines(rows: list[IncidentEvidence], limit: int = 3) -> list[str]:
    """Up to ``limit`` short lines for Telegram/timeline headers."""
    collected = [row for row in rows if row.status not in {SKIPPED_CONTEXT, SKIPPED_FLAPPING}]
    if not collected:
        return []
    ok = sum(1 for row in collected if row.status == OK)
    lines = [f"Bằng chứng ({collected[0].runbook}): {ok}/{len(collected)} collector thành công"]
    failed = sorted({f"{row.collector_id}={row.status}" for row in collected if row.status != OK})
    if failed:
        lines.append("Lỗi: " + ", ".join(failed[:4]))
    skipped = [row.collector_id for row in rows if row.status == SKIPPED_CONTEXT]
    if skipped:
        lines.append("Bỏ qua (thiếu ngữ cảnh): " + ", ".join(skipped[:4]))
    return lines[:limit]
