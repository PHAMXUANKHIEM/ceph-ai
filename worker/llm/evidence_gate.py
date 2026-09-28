"""Evidence before the LLM (autonomy plan WP3.4).

The Worker diagnoses an incident as soon as it opens, before the Watcher's
investigation scan (WP3.3) has had a chance to run.  This gate closes that
gap: if the incident has no evidence yet, it runs the same runbook with the
same read-only collectors and limits, stores the rows (so the scanner skips
the incident) and applies the deterministic rules.

* A confident rule conclusion replaces the LLM call; its diagnosis cites the
  evidence, and its action (if any) goes through the same classification,
  preflight and approval/autopilot gates as an LLM proposal.
* Otherwise the LLM gets the real evidence in its prompt, with the
  instruction to cite it and to say when evidence is missing.

``settings.investigation_enabled`` switches the whole gate off (the test
suite does that), in which case diagnosis behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from config.settings import settings
from shared import db, deterministic_triage, incident_evidence, investigation_runbooks
from shared.deterministic_triage import Triage
from shared.autonomy_kpi import fault_family
from shared.evidence_collectors import EvidenceRunner, SshTransport
from shared.models import Cluster, Incident, IncidentEvidence

logger = logging.getLogger(__name__)

NO_ACTION = "investigate_manually"
PROMPT_EVIDENCE_MAX_CHARS = 4000
PROMPT_ROW_MAX_CHARS = 700


@dataclass
class GateResult:
    triage: Triage | None = None
    decided: bool = False
    prompt_block: str = ""
    summary: list[str] = field(default_factory=list)


def _transport(session, cluster_id: str | None) -> SshTransport:
    cluster = session.get(Cluster, cluster_id) if cluster_id else None
    return SshTransport(None if cluster is None or cluster.is_default else cluster)


def _collect_if_missing(incident_id: str) -> list[IncidentEvidence]:
    from watcher.investigation_scanner import runner_for, signal_evidence

    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        if incident is None:
            return []
        rows = incident_evidence.for_incident(session, incident_id)
        if rows:
            return rows
        transport = _transport(session, incident.cluster_id)
        code, cluster_key = incident.ceph_code, incident.cluster_id or "default"
        signal = signal_evidence(incident.signal_evidence_json)
    if signal.get("flapping"):
        return []          # the scanner marks flapping repeats; no SSH for them
    if not transport.mon_nodes:
        return []
    runner = runner_for(f"worker:{cluster_key}", lambda: EvidenceRunner(transport))
    if not runner.claim(f"{cluster_key}:{fault_family(code)}"):
        return []          # a similar incident was investigated moments ago
    plan = investigation_runbooks.plan(code, investigation_runbooks.context_for(code, signal),
                                       mon_host=transport.mon_nodes[0])
    results = runner.run(plan.requests)
    with db.SessionLocal() as session:
        incident_evidence.store(session, incident_id, plan, results)
        incident_evidence.record_triage(session, incident_id, code)
        session.commit()
        return incident_evidence.for_incident(session, incident_id)


def prompt_block(rows: list[IncidentEvidence]) -> str:
    collected = [row for row in rows if row.status not in {incident_evidence.SKIPPED_CONTEXT,
                                                          incident_evidence.SKIPPED_FLAPPING}]
    if not collected:
        return ""
    lines = [
        "\nBằng chứng chỉ-đọc vừa thu từ cluster (đã redact). Mỗi khẳng định trong chẩn đoán phải "
        "trích mã [E#] tương ứng; nếu bằng chứng không đủ, nói rõ là chưa chắc chắn thay vì đoán:",
    ]
    for index, row in enumerate(collected, start=1):
        output = (row.output_redacted or "")[:PROMPT_ROW_MAX_CHARS]
        lines.append(f"[E{index}] {row.collector_id} ({row.status}): {output}")
    return "\n".join(lines)[:PROMPT_EVIDENCE_MAX_CHARS] + "\n"


def _min_confidence() -> float:
    return max(float(settings.triage_min_confidence), float(settings.ai_min_diagnosis_confidence))


async def prepare(incident_id: str, ceph_code: str | None) -> GateResult:
    if not settings.investigation_enabled:
        return GateResult()
    try:
        rows = await asyncio.to_thread(_collect_if_missing, incident_id)
    except Exception:  # noqa: BLE001 - evidence must never block a diagnosis
        logger.exception("evidence_gate: collecting evidence for %s failed", incident_id)
        return GateResult()
    if not rows:
        return GateResult()
    triage = deterministic_triage.triage(ceph_code, rows)
    return GateResult(
        triage=triage,
        decided=triage.is_known and triage.confidence >= _min_confidence(),
        prompt_block=prompt_block(rows),
        summary=incident_evidence.summary_lines(rows),
    )


def result_from_triage(triage: Triage) -> dict:
    """The router-shaped result for a rule conclusion (no LLM call)."""
    diagnosis = f"[Chẩn đoán theo luật: {triage.conclusion}] {triage.summary}"
    if triage.recommendation:
        diagnosis += f" Khuyến nghị: {triage.recommendation}"
    return {
        "diagnosis_text": diagnosis,
        "action_id": triage.action_id or NO_ACTION,
        "rationale": f"{triage.summary} Bằng chứng: {', '.join(triage.cited) or 'không có'}.",
        "diagnosis_confidence": triage.confidence,
    }


def annotate(result: dict, gate: GateResult) -> dict:
    """Append the short evidence summary so Telegram and the incident show it."""
    if not gate.summary or not result.get("diagnosis_text"):
        return result
    return {**result, "diagnosis_text": f"{result['diagnosis_text']}\n\n" + "\n".join(gate.summary)}
