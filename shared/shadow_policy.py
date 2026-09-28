"""Risk-constrained "execute or escalate" policy, shadow only (autonomy plan WP6.3).

For every logged decision this policy says what it *would* do. It never
acts: the recommendation is stored next to the decision (autonomy_decisions)
so it can be compared with operator verdicts and evaluated off-policy
before any promotion (WP6.4). Autopilot guardrails, kill switch, cooldowns
and the Trust Engine stay the only path to execution.

``execute`` needs every gate to pass; any failing gate means ``escalate``:

* the action is a real remediation with a registered playbook contract,
* blast radius: affected nodes within the contract's ``max_targets``,
* reversibility: the contract names a rollback action,
* uncertainty: diagnosis confidence high enough for its source,
* trust: the Trust Engine's sample count and score for this scope,
* risk budget: the cluster's recent false-release rate (FRR) of this
  policy's own ``execute`` recommendations is within budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from shared.trust_engine import SHADOW_MIN_TRUST_SCORE, SHADOW_MIN_VERIFIED_SAMPLES

EXECUTE, ESCALATE = "execute", "escalate"
NO_ACTION = "investigate_manually"
MIN_CONFIDENCE = {"llm": 0.9, "rules": 0.75, "deterministic": 0.9}
FRR_WINDOW = timedelta(days=30)
FRR_MIN_LABELLED = 10          # below this the FRR is unknown and not enforced
BAD_VERDICTS = frozenset({"FALSE_POSITIVE", "UNSAFE", "INEFFECTIVE"})


@dataclass(frozen=True)
class ContractInfo:
    registered: bool
    max_targets: int = 1
    rollback: str | None = None


@dataclass
class ShadowDecision:
    recommendation: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"recommendation": self.recommendation, "reasons": self.reasons}


def contract_info(action_id: str) -> ContractInfo:
    from worker.policy.playbook_registry import get_contract

    contract = get_contract(action_id)
    if contract is None:
        return ContractInfo(False)
    return ContractInfo(True, int(contract.max_targets or 1), contract.rollback)


def evaluate(context: dict, action_id: str, chosen_by: str, contract: ContractInfo,
             frr: float | None, frr_budget: float) -> ShadowDecision:
    if action_id == NO_ACTION:
        return ShadowDecision(ESCALATE, ["không có action khắc phục để tự chạy"])
    reasons = []
    if not contract.registered:
        reasons.append("playbook chưa có contract đăng ký")
    affected = int(context.get("affected_nodes") or 0)
    if affected > contract.max_targets:
        reasons.append(f"blast radius {affected} node > giới hạn {contract.max_targets}")
    if not contract.rollback:
        reasons.append("không có rollback đã đăng ký")
    confidence = context.get("triage_confidence") if chosen_by == "rules" else context.get("diagnosis_confidence")
    needed = MIN_CONFIDENCE.get(chosen_by, 1.0)
    if confidence is None or float(confidence) < needed:
        reasons.append(f"độ tin {confidence if confidence is not None else '—'} < {needed} ({chosen_by})")
    samples = int(context.get("shadow_sample_count") or 0)
    trust = float(context.get("shadow_trust_score") or 0.0)
    if samples < SHADOW_MIN_VERIFIED_SAMPLES or trust < SHADOW_MIN_TRUST_SCORE:
        reasons.append(f"Trust Engine {samples}/{SHADOW_MIN_VERIFIED_SAMPLES} mẫu, "
                       f"trust {trust:.2f} < {SHADOW_MIN_TRUST_SCORE}")
    if frr is not None and frr > frr_budget:
        reasons.append(f"FRR 30 ngày {frr:.1%} > ngân sách {frr_budget:.0%}")
    return ShadowDecision(ESCALATE if reasons else EXECUTE, reasons)


def false_release_rate(session, cluster_id: str | None, now: datetime) -> float | None:
    """Share of this policy's own ``execute`` recommendations that an
    operator later judged wrong; None until enough of them are labelled."""
    from shared.models import AutonomyDecision, RemediationCase

    query = session.query(RemediationCase.operator_verdict).join(
        AutonomyDecision, AutonomyDecision.case_id == RemediationCase.id,
    ).filter(
        AutonomyDecision.shadow_recommendation == EXECUTE,
        AutonomyDecision.created_at >= now - FRR_WINDOW,
        RemediationCase.operator_verdict.isnot(None),
        RemediationCase.operator_verdict != "INCONCLUSIVE",
    )
    query = query.filter(AutonomyDecision.cluster_id == cluster_id) if cluster_id else query.filter(
        AutonomyDecision.cluster_id.is_(None))
    verdicts: list[Any] = [verdict for (verdict,) in query.all()]
    if len(verdicts) < FRR_MIN_LABELLED:
        return None
    return sum(1 for verdict in verdicts if verdict in BAD_VERDICTS) / len(verdicts)
