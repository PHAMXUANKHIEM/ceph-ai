"""Decision log for off-policy learning (autonomy plan WP6.1).

Every remediation proposal is logged with the context the decision was
made in, the action chosen, who chose it and the probability it was
chosen with (propensity).  Today every choice is deterministic (rules, a
deterministic code path or the LLM's single answer), so the propensity is
1.0; that means off-policy evaluation can only score policies that agree
with what was logged until exploration is introduced (WP6.3), and the
report says so.

Rewards are not stored: ``reward_for`` derives them from the case's
operator verdict and verified outcome whenever a report runs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from shared import shadow_policy
from shared.autonomy_kpi import fault_family
from shared.models import AutonomyDecision, Incident, RemediationCase
from shared.time import utc_now

logger = logging.getLogger(__name__)

POLICY_VERSION = "rules-llm-v1"
ESCALATE = "investigate_manually"
CHOSEN_BY = frozenset({"rules", "llm", "deterministic"})

# Operator verdict first (ground truth), then verified outcome.
_VERDICT_REWARD = {"CORRECT": 1.0, "INEFFECTIVE": 0.0, "FALSE_POSITIVE": 0.0, "UNSAFE": 0.0}
_OUTCOME_REWARD = {"VERIFIED_SUCCESS": 1.0, "VERIFIED_FAILED": 0.0, "EXECUTION_FAILED": 0.0}


def _snapshot_status(envelope: dict) -> str:
    snapshot = envelope.get("cluster_snapshot")
    if isinstance(snapshot, dict):
        health = snapshot.get("health")
        status = health.get("status") if isinstance(health, dict) else snapshot.get("status")
        if isinstance(status, str):
            return status[:32]
    return "UNKNOWN"


def features(*, incident: Incident, case: RemediationCase, envelope: dict,
             triage: Any = None, now: datetime | None = None) -> dict:
    """Structured, bounded context only — never free text or raw evidence."""
    raw_nodes = envelope.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    return {
        "fault_family": fault_family(incident.ceph_code),
        "severity": str(incident.severity or "UNKNOWN")[:32],
        "cluster_status": _snapshot_status(envelope),
        "affected_nodes": len(nodes),
        "hour_utc": (now or utc_now()).hour,
        "diagnosis_confidence": case.diagnosis_confidence,
        "shadow_trust_score": case.shadow_trust_score,
        "shadow_sample_count": case.shadow_sample_count,
        "classification": case.classification,
        "triage_conclusion": getattr(triage, "conclusion", None),
        "triage_confidence": getattr(triage, "confidence", None),
    }


def record(session, *, incident: Incident, case: RemediationCase, chosen_action: str, chosen_by: str,
           envelope: dict, triage: Any = None, now: datetime | None = None) -> AutonomyDecision:
    """Log one decision in the caller's transaction (idempotent per case)."""
    if chosen_by not in CHOSEN_BY:
        raise ValueError(f"unknown decision source {chosen_by!r}")
    existing = session.query(AutonomyDecision).filter_by(case_id=case.id).one_or_none()
    if existing is not None:
        return existing
    candidates = sorted({chosen_action, ESCALATE})
    context = features(incident=incident, case=case, envelope=envelope, triage=triage, now=now)
    shadow = _shadow(session, context, chosen_action, chosen_by, incident.cluster_id, now or utc_now())
    row = AutonomyDecision(
        case_id=case.id, incident_id=incident.id, cluster_id=incident.cluster_id,
        fault_family=fault_family(incident.ceph_code)[:64],
        context_json=json.dumps(context, sort_keys=True),
        candidates_json=json.dumps(candidates),
        chosen_action=chosen_action[:64], chosen_by=chosen_by, propensity=1.0, policy_version=POLICY_VERSION,
        shadow_recommendation=shadow.recommendation if shadow else None,
        shadow_reasons_json=json.dumps(shadow.reasons, ensure_ascii=False) if shadow else None,
    )
    session.add(row)
    session.flush()
    return row


def _shadow(session, context: dict, action_id: str, chosen_by: str, cluster_id: str | None,
            now: datetime) -> shadow_policy.ShadowDecision | None:
    """What the WP6.3 shadow policy would do; a failure here only loses the
    shadow column, never the decision log or the diagnosis."""
    from config.settings import settings

    try:
        # Savepoint: a failed FRR query must not poison the caller's
        # transaction (the Action/RemediationCase being created).
        with session.begin_nested():
            frr = shadow_policy.false_release_rate(session, cluster_id, now)
        return shadow_policy.evaluate(
            context, action_id, chosen_by, shadow_policy.contract_info(action_id),
            frr, float(settings.shadow_frr_budget),
        )
    except Exception:  # noqa: BLE001
        logger.exception("decision_log: shadow policy failed for %s", action_id)
        return None


def reward_for(operator_verdict: str | None, outcome: str | None, regressed_24h: bool | None) -> float | None:
    """1.0 good, 0.0 bad, None unknown (INCONCLUSIVE or no signal yet)."""
    if operator_verdict in _VERDICT_REWARD:
        return _VERDICT_REWARD[operator_verdict]
    if operator_verdict:
        return None
    if outcome == "VERIFIED_SUCCESS" and regressed_24h:
        return 0.0
    return _OUTCOME_REWARD.get(str(outcome))


@dataclass(frozen=True)
class LoggedDecision:
    context: dict
    action: str
    propensity: float
    reward: float
    chosen_by: str
    fault_family: str


def load(session, *, days: int = 90, now: datetime | None = None) -> tuple[list[LoggedDecision], int]:
    """Logged decisions whose reward is known, plus how many had none yet."""
    since = (now or utc_now()) - timedelta(days=days)
    rows = (
        session.query(AutonomyDecision.context_json, AutonomyDecision.chosen_action, AutonomyDecision.propensity,
                      AutonomyDecision.chosen_by, AutonomyDecision.fault_family,
                      RemediationCase.operator_verdict, RemediationCase.outcome, RemediationCase.regressed_24h)
        .join(RemediationCase, RemediationCase.id == AutonomyDecision.case_id)
        .filter(AutonomyDecision.created_at >= since)
        .all()
    )
    logged, unknown = [], 0
    for row in rows:
        reward = reward_for(row.operator_verdict, row.outcome, row.regressed_24h)
        if reward is None:
            unknown += 1
            continue
        logged.append(LoggedDecision(json.loads(row.context_json), row.chosen_action, float(row.propensity),
                                     reward, row.chosen_by, row.fault_family))
    return logged, unknown
