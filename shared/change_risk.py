"""Deterministic change-risk gate over verified Remediation Case Memory."""
from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError

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
    fingerprint: str

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
    all_cases = query.order_by(RemediationCase.verified_at.desc(), RemediationCase.created_at.desc()).limit(100).all()
    current_case = session.query(RemediationCase).filter_by(action_id=action.id).one_or_none()
    fault_family = current_case.fault_family if current_case is not None else (incident.ceph_code if incident else None)
    ceph_version = current_case.ceph_version if current_case is not None else None
    deployment_mode = current_case.deployment_mode if current_case is not None else None
    cases = [case for case in all_cases if (
        case.cluster_id == cluster_id
        and (fault_family is None or case.fault_family == fault_family)
        and (ceph_version is None or case.ceph_version in {None, ceph_version})
        and (deployment_mode is None or case.deployment_mode in {None, deployment_mode})
    )]
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
        "fault_family": fault_family,
        "ceph_version": ceph_version,
        "deployment_mode": deployment_mode,
        "global_case_count": len(all_cases),
        "global_regression_count": sum(any(value is True for value in (
            case.regressed_1h, case.regressed_24h, case.regressed_7d
        )) for case in all_cases),
        "case_ids": [case.id for case in cases],
        "outcomes": {outcome: sum(case.outcome == outcome for case in cases) for outcome in sorted(SUCCESS_OUTCOMES | FAILURE_OUTCOMES)},
        "regression_case_ids": [case.id for case in regressions],
    }
    summary = (
        f"Rủi ro thay đổi {level}: {reason}. Evidence: {len(cases)} RemediationCase cùng action "
        f"đúng cluster/fault/version/deployment; toàn hệ thống có {len(all_cases)} case cùng action."
    )
    fingerprint_evidence = {
        key: evidence[key] for key in (
            "action_id", "cluster_id", "fault_family", "ceph_version", "deployment_mode",
            "case_ids", "outcomes", "regression_case_ids",
        )
    }
    fingerprint = hashlib.sha256(json.dumps(
        {"level": level, "evidence": fingerprint_evidence},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return RiskResult(level, summary, len(cases), len(successes), len(failures), len(regressions), evidence, fingerprint)


def assess_and_record(session, *, action: Action, incident: Incident | None = None) -> RiskResult:
    result = assess(session, action=action, incident=incident)
    row = session.query(ChangeRiskAssessment).filter_by(action_id=action.id).one_or_none()
    if row is None:
        try:
            with session.begin_nested():
                row = ChangeRiskAssessment(
                    id=str(uuid.uuid4()), action_id=action.id,
                    cluster_id=result.evidence["cluster_id"], risk_level=result.level,
                    sample_count=result.sample_count, success_count=result.success_count,
                    failure_count=result.failure_count, regression_count=result.regression_count,
                    summary=result.summary,
                    evidence_json=json.dumps(result.evidence, ensure_ascii=False, sort_keys=True),
                    assessment_hash=result.fingerprint, analyzed_at=datetime.utcnow(),
                )
                session.add(row)
                session.flush()
        except IntegrityError:
            row = session.query(ChangeRiskAssessment).filter_by(action_id=action.id).one()
    row.cluster_id = result.evidence["cluster_id"]
    row.risk_level = result.level
    row.sample_count = result.sample_count
    row.success_count = result.success_count
    row.failure_count = result.failure_count
    row.regression_count = result.regression_count
    row.summary = result.summary
    row.evidence_json = json.dumps(result.evidence, ensure_ascii=False, sort_keys=True)
    row.assessment_hash = result.fingerprint
    row.analyzed_at = datetime.utcnow()
    return result


def acknowledge(session, *, action: Action, incident: Incident | None = None) -> RiskResult:
    result = assess_and_record(session, action=action, incident=incident)
    row = session.query(ChangeRiskAssessment).filter_by(action_id=action.id).one()
    row.acknowledged_hash = result.fingerprint
    return result


def attach_summary(action: Action, result: RiskResult) -> None:
    marker = "[Change-risk analyzer]"
    base = (action.rationale or "").split(marker, 1)[0].rstrip()
    action.rationale = f"{base}\n\n{marker} {result.summary}".strip()
