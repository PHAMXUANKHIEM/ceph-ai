"""Deterministic change-risk gate over verified Remediation Case Memory."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from shared.models import Action, ChangeRiskAssessment, Incident, RemediationCase

FAILURE_OUTCOMES = {"VERIFIED_FAILED", "EXECUTION_FAILED", "INCONCLUSIVE"}
SUCCESS_OUTCOMES = {"VERIFIED_SUCCESS"}


@dataclass(frozen=True)
class RiskResult:
    level: str
    summary: str
    sample_count: int
    success_count: int
    failure_count: int
    regression_count: int
    evidence: dict

    @property
    def blocks_autopilot(self) -> bool:
        return self.level == "HIGH"


def assess(session, *, action: Action, incident: Incident | None = None) -> RiskResult:
    incident = incident or session.get(Incident, action.incident_id)
    cluster_id = incident.cluster_id if incident is not None else None
    query = session.query(RemediationCase).join(Action, Action.id == RemediationCase.action_id).filter(
        Action.action_id == action.action_id,
        Action.id != action.id,
        RemediationCase.outcome.in_(sorted(SUCCESS_OUTCOMES | FAILURE_OUTCOMES)),
    )
    cases = query.order_by(RemediationCase.verified_at.desc(), RemediationCase.created_at.desc()).limit(100).all()
    failures = [case for case in cases if case.outcome in FAILURE_OUTCOMES]
    successes = [case for case in cases if case.outcome in SUCCESS_OUTCOMES]
    regressions = [case for case in cases if any(
        value is True for value in (case.regressed_1h, case.regressed_24h, case.regressed_7d)
    )]
    if regressions:
        level = "HIGH"
        reason = f"đã có {len(regressions)} case verified bị regression"
    elif len(failures) >= 2 and len(failures) >= len(successes):
        level = "HIGH"
        reason = f"{len(failures)}/{len(cases)} case kết thúc thất bại hoặc inconclusive"
    elif failures:
        level = "MEDIUM"
        reason = f"đã có {len(failures)} case thất bại, chưa đủ bằng chứng để tự động chặn"
    elif len(successes) >= 3:
        level = "LOW"
        reason = f"{len(successes)} case verified thành công và chưa thấy regression"
    else:
        level = "INSUFFICIENT_EVIDENCE"
        reason = f"chỉ có {len(cases)} case terminal cùng action/cluster"
    evidence = {
        "source": "remediation_cases",
        "action_id": action.action_id,
        "cluster_id": cluster_id,
        "same_cluster_count": sum(case.cluster_id == cluster_id for case in cases),
        "case_ids": [case.id for case in cases],
        "outcomes": {outcome: sum(case.outcome == outcome for case in cases) for outcome in sorted(SUCCESS_OUTCOMES | FAILURE_OUTCOMES)},
        "regression_case_ids": [case.id for case in regressions],
    }
    summary = (
        f"Rủi ro thay đổi {level}: {reason}. Evidence: {len(cases)} RemediationCase cùng action "
        f"trên mọi cluster, {evidence['same_cluster_count']} case cùng cluster hiện tại."
    )
    return RiskResult(level, summary, len(cases), len(successes), len(failures), len(regressions), evidence)


def assess_and_record(session, *, action: Action, incident: Incident | None = None) -> RiskResult:
    result = assess(session, action=action, incident=incident)
    row = session.query(ChangeRiskAssessment).filter_by(action_id=action.id).one_or_none()
    if row is None:
        row = ChangeRiskAssessment(id=str(uuid.uuid4()), action_id=action.id)
        session.add(row)
    row.cluster_id = result.evidence["cluster_id"]
    row.risk_level = result.level
    row.sample_count = result.sample_count
    row.success_count = result.success_count
    row.failure_count = result.failure_count
    row.regression_count = result.regression_count
    row.summary = result.summary
    row.evidence_json = json.dumps(result.evidence, ensure_ascii=False, sort_keys=True)
    row.analyzed_at = datetime.utcnow()
    return result


def attach_summary(action: Action, result: RiskResult) -> None:
    marker = "[Change-risk analyzer]"
    base = (action.rationale or "").split(marker, 1)[0].rstrip()
    action.rationale = f"{base}\n\n{marker} {result.summary}".strip()
