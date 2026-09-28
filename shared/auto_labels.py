"""Weak-supervision labels for remediation cases (autonomy plan WP2.3).

Operators label few cases, but the database already records facts that
suggest whether a diagnosis/proposal was right: the incident cleared on its
own, the post-check passed without regression, the problem came back, the
host was flapping. Each labeling function (LF) turns one such fact into a
vote in the operator vocabulary (CORRECT / FALSE_POSITIVE / INEFFECTIVE) or
abstains; votes are combined by weight.

Rules of use (see the plan):

* labels are computed on demand from existing rows and never stored in
  ``operator_verdict``;
* they never grant autonomy — Trust Engine and autopilot keep using operator
  verdicts only;
* they serve evaluation, ranking cases for review and shadow policies, and
  their precision is measured against operator verdicts.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from shared.models import Action, Incident, RemediationCase
from shared.time import utc_now

CORRECT, FALSE_POSITIVE, INEFFECTIVE = "CORRECT", "FALSE_POSITIVE", "INEFFECTIVE"
SELF_RESOLVE_WINDOW = timedelta(minutes=30)
REOPEN_WINDOW = timedelta(hours=24)
_EXECUTED = {"EXECUTED", "AUTO_EXECUTED"}


@dataclass
class CaseFacts:
    case_id: str
    fault_family: str
    outcome: str | None
    regressed_1h: bool | None
    regressed_24h: bool | None
    incident_status: str | None
    detected_at: datetime | None
    resolved_at: datetime | None
    executed: bool
    flapping: bool
    reopened_after_execution: bool
    operator_verdict: str | None = None


@dataclass(frozen=True)
class Vote:
    lf: str
    label: str
    weight: float


@dataclass
class AutoLabel:
    case_id: str
    label: str | None
    confidence: float
    votes: list[Vote] = field(default_factory=list)


def lf_postcheck_passed(facts: CaseFacts) -> Vote | None:
    if facts.outcome == "VERIFIED_SUCCESS" and facts.regressed_24h is False:
        return Vote("postcheck_passed", CORRECT, 1.0)
    return None


def lf_regressed(facts: CaseFacts) -> Vote | None:
    if facts.regressed_1h or facts.regressed_24h:
        return Vote("regressed", INEFFECTIVE, 1.0)
    return None


def lf_execution_failed(facts: CaseFacts) -> Vote | None:
    if facts.outcome == "EXECUTION_FAILED":
        return Vote("execution_failed", INEFFECTIVE, 0.5)
    return None


def lf_reopened_after_execution(facts: CaseFacts) -> Vote | None:
    if facts.reopened_after_execution:
        return Vote("reopened_after_execution", INEFFECTIVE, 0.8)
    return None


def lf_self_resolved_without_action(facts: CaseFacts) -> Vote | None:
    if (
        not facts.executed
        and facts.incident_status == "RESOLVED"
        and facts.detected_at is not None
        and facts.resolved_at is not None
        and facts.resolved_at - facts.detected_at <= SELF_RESOLVE_WINDOW
    ):
        return Vote("self_resolved_without_action", FALSE_POSITIVE, 0.6)
    return None


def lf_flapping(facts: CaseFacts) -> Vote | None:
    if facts.flapping and not facts.executed:
        return Vote("flapping_signal", FALSE_POSITIVE, 0.7)
    return None


LABELING_FUNCTIONS: tuple[Callable[[CaseFacts], Vote | None], ...] = (
    lf_postcheck_passed,
    lf_regressed,
    lf_execution_failed,
    lf_reopened_after_execution,
    lf_self_resolved_without_action,
    lf_flapping,
)


def label_case(facts: CaseFacts) -> AutoLabel:
    votes = [vote for lf in LABELING_FUNCTIONS if (vote := lf(facts)) is not None]
    if not votes:
        return AutoLabel(facts.case_id, None, 0.0, [])
    weights: dict[str, float] = defaultdict(float)
    for vote in votes:
        weights[vote.label] += vote.weight
    label, top = max(weights.items(), key=lambda item: (item[1], item[0]))
    return AutoLabel(facts.case_id, label, round(top / sum(weights.values()), 3), votes)


def _flapping(evidence_json: str | None) -> bool:
    try:
        return bool(json.loads(evidence_json or "{}").get("flapping"))
    except (ValueError, AttributeError):
        return False


def collect_facts(session, *, days: int = 30, now: datetime | None = None) -> list[CaseFacts]:
    since = (now or utc_now()) - timedelta(days=days)
    rows = (
        session.query(
            RemediationCase.id, RemediationCase.fault_family, RemediationCase.outcome,
            RemediationCase.regressed_1h, RemediationCase.regressed_24h, RemediationCase.operator_verdict,
            RemediationCase.executed_at, Incident.status.label("incident_status"), Incident.detected_at,
            Incident.updated_at.label("incident_updated_at"), Incident.signal_evidence_json,
            Incident.ceph_code, Incident.cluster_id, Action.status.label("action_status"),
        )
        .join(Incident, Incident.id == RemediationCase.incident_id)
        .join(Action, Action.id == RemediationCase.action_id)
        .filter(RemediationCase.created_at >= since)
        .all()
    )
    codes = {(row.cluster_id, row.ceph_code) for row in rows}
    openings: dict[tuple, list[datetime]] = defaultdict(list)
    if codes:
        for cluster_id, ceph_code, created_at in session.query(
            Incident.cluster_id, Incident.ceph_code, Incident.created_at
        ).filter(Incident.created_at >= since, Incident.ceph_code.in_({code for _, code in codes})).all():
            openings[(cluster_id, ceph_code)].append(created_at)
    facts = []
    for row in rows:
        executed = row.action_status in _EXECUTED or row.executed_at is not None
        reopened = False
        if executed and row.executed_at is not None:
            reopened = any(
                row.executed_at < opened <= row.executed_at + REOPEN_WINDOW
                for opened in openings.get((row.cluster_id, row.ceph_code), [])
            )
        facts.append(CaseFacts(
            case_id=row.id, fault_family=row.fault_family, outcome=row.outcome,
            regressed_1h=row.regressed_1h, regressed_24h=row.regressed_24h,
            incident_status=row.incident_status, detected_at=row.detected_at,
            resolved_at=row.incident_updated_at if row.incident_status == "RESOLVED" else None,
            executed=executed, flapping=_flapping(row.signal_evidence_json),
            reopened_after_execution=reopened, operator_verdict=row.operator_verdict,
        ))
    return facts


def quality_report(facts: list[CaseFacts]) -> dict:
    """Coverage and conflicts of the LFs, and precision against operator verdicts."""
    labels = [label_case(item) for item in facts]
    by_case = {item.case_id: item for item in facts}
    coverage = Counter(vote.lf for label in labels for vote in label.votes)
    checked: Counter = Counter()
    agreed: Counter = Counter()
    for label in labels:
        verdict = by_case[label.case_id].operator_verdict
        if verdict not in {CORRECT, FALSE_POSITIVE, INEFFECTIVE}:
            continue
        for vote in label.votes:
            checked[vote.lf] += 1
            agreed[vote.lf] += int(vote.label == verdict)
    return {
        "cases": len(facts),
        "labelled": sum(1 for label in labels if label.label),
        "conflicts": sum(1 for label in labels if len({vote.label for vote in label.votes}) > 1),
        "by_label": dict(Counter(label.label for label in labels if label.label)),
        "lf_coverage": dict(coverage),
        "lf_precision_vs_operator": {
            lf: {"checked": checked[lf], "precision": round(agreed[lf] / checked[lf], 3)}
            for lf in checked
        },
        "note": "Auto labels never set operator_verdict and never grant autonomy.",
    }
