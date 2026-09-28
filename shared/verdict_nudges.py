"""Pick the remediation cases most worth an operator verdict (autonomy plan WP2.2).

A daily Telegram nudge asks for verdicts on a handful of cases instead of
all of them. Cases are ranked so each answer teaches the learning loop the
most:

* the outcome is already known (verified or failed execution),
* the model was confident but the operator rejected it (disagreement),
* the fault family has few labels so far,
* the proposal is a concrete action rather than ``investigate_manually``.

At most ``per_family_cap`` cases per fault family keep the batch varied. A
case is nudged once: the nudge is recorded as an incident timeline event,
so no schema change is needed and repeated runs skip it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from shared import incident_events
from shared.models import Action, IncidentTimelineEvent, RemediationCase
from shared.time import utc_now

NUDGE_EVENT = "verdict_nudge_sent"
PLACEHOLDER_ACTION = "investigate_manually"
_KNOWN_OUTCOMES = {"VERIFIED_SUCCESS", "VERIFIED_FAILED", "EXECUTION_FAILED"}


@dataclass(frozen=True)
class NudgeCandidate:
    case_id: str
    incident_id: str
    action_pk: str
    fault_family: str
    score: float
    reasons: tuple[str, ...]


def _score(case, action_id: str | None, action_status: str | None,
           labelled_in_family: int) -> tuple[float, tuple[str, ...]]:
    score, reasons = 0.0, []
    if case.outcome in _KNOWN_OUTCOMES:
        score += 3
        reasons.append(f"outcome {case.outcome}")
    if (case.diagnosis_confidence or 0) >= 0.7 and action_status == "REJECTED":
        score += 2
        reasons.append("AI tự tin nhưng bị từ chối")
    rarity = 2.0 / (1 + labelled_in_family)
    score += rarity
    if labelled_in_family == 0:
        reasons.append("fault family chưa có nhãn")
    if action_id and action_id != PLACEHOLDER_ACTION:
        score += 1
        reasons.append(f"đề xuất cụ thể: {action_id}")
    return score, tuple(reasons)


def select_cases(session, *, limit: int = 5, lookback_days: int = 14, per_family_cap: int = 2,
                 now: datetime | None = None) -> list[NudgeCandidate]:
    if limit <= 0:
        return []
    since = (now or utc_now()) - timedelta(days=lookback_days)
    labelled = Counter(
        family for (family,) in session.query(RemediationCase.fault_family)
        .filter(RemediationCase.operator_verdict.isnot(None)).all()
    )
    already_nudged = {
        action_pk for (action_pk,) in session.query(IncidentTimelineEvent.action_id)
        .filter(IncidentTimelineEvent.event_type == NUDGE_EVENT).all()
    }
    rows = (
        session.query(RemediationCase, Action.action_id, Action.status)
        .join(Action, Action.id == RemediationCase.action_id)
        .filter(RemediationCase.operator_verdict.is_(None), RemediationCase.created_at >= since)
        .all()
    )
    ranked = []
    for case, action_id, action_status in rows:
        if case.action_id in already_nudged:
            continue
        score, reasons = _score(case, action_id, action_status, labelled[case.fault_family])
        ranked.append(NudgeCandidate(
            case_id=case.id, incident_id=case.incident_id, action_pk=case.action_id,
            fault_family=case.fault_family, score=round(score, 3), reasons=reasons,
        ))
    ranked.sort(key=lambda item: (-item.score, item.case_id))
    chosen: list[NudgeCandidate] = []
    per_family: Counter = Counter()
    for candidate in ranked:
        if per_family[candidate.fault_family] >= per_family_cap:
            continue
        chosen.append(candidate)
        per_family[candidate.fault_family] += 1
        if len(chosen) >= limit:
            break
    return chosen


def mark_nudged(session, candidate: NudgeCandidate) -> None:
    incident_events.record(
        session, incident_id=candidate.incident_id, action_id=candidate.action_pk,
        event_type=NUDGE_EVENT, actor="system",
        evidence={"score": candidate.score, "reasons": list(candidate.reasons)},
    )


def nudged_since(session, since: datetime) -> bool:
    """True when a nudge was already sent after ``since`` (restart-safe)."""
    return session.query(IncidentTimelineEvent.id).filter(
        IncidentTimelineEvent.event_type == NUDGE_EVENT,
        IncidentTimelineEvent.created_at >= since,
    ).first() is not None

