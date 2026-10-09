"""Past cases and proposal history for a diagnosis prompt (learning plan LL1 + LL6).

Context only, never authorization: nothing here changes what may execute.

``find_verified_cases`` (shared/case_retrieval.py) required the exact code
with its entity ("OSD_LATENCY_HIGH:3" never matched ":6"), the exact node
set and a verified execution. With 97 % of incidents diagnosis-only, it had
never returned a single case on production. Here:

* the fault family (code without entity) and the Ceph major version are hard
  filters; the exact code, node set and deployment mode only rank;
* three kinds of reference are used, strongest first: a verified fix, a
  diagnosis the operator marked CORRECT, and an incident that resolved by
  itself with no execution and no recurrence;
* ``proposal_history`` tells the model how its past proposals for the family
  ended — e.g. a reboot proposed many times whose incident always resolved
  before anyone approved it.

RemediationCase.fault_family keeps the raw code on purpose: the Trust Engine
scopes trust by it, and widening that scope would widen permissions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import or_

from shared import audit
from shared.autonomy_kpi import fault_family
from shared.case_retrieval import _BAD_VERDICTS, _eligible, _load, _major, _normalized_nodes
from shared.models import Action, AuditEntry, Incident, IncidentTimelineEvent, RemediationCase
from shared.time import utc_now

NO_ACTION = "investigate_manually"
CANDIDATES = 300
HISTORY_DAYS = 30
# A proposal is worth flagging once it was not needed this many times.
HISTORY_MIN_NOT_NEEDED = 3
_KIND_WEIGHT = {"verified_fix": 30, "diagnosis_confirmed": 20, "self_resolved": 10}
_EXECUTED = {"EXECUTED_PENDING_VERIFY", "EXECUTED_UNVERIFIED", "EXECUTION_FAILED", "VERIFIED_SUCCESS",
             "VERIFIED_FAILED", "INCONCLUSIVE"}


def _family_filter(column, family: str):
    return or_(column == family, column.like(family + ":%"))


def _scope(column, cluster_id: str | None, include_unscoped: bool):
    """Legacy rows with a NULL cluster belong to the default cluster."""
    if cluster_id is None:
        return column.is_(None)
    return or_(column == cluster_id, column.is_(None)) if include_unscoped else column == cluster_id


def _recurred(session, incident_ids: list[str]) -> set[str]:
    if not incident_ids:
        return set()
    rows = session.query(AuditEntry.incident_id).filter(
        AuditEntry.incident_id.in_(incident_ids), AuditEntry.event_type == audit.EVENT_INCIDENT_RECURRED)
    return {incident_id for (incident_id,) in rows}


def _kind(case: RemediationCase, incident: Incident, recurred: set[str]) -> str | None:
    pre_state = _load(case.pre_state_json, {})
    if case.operator_verdict in _BAD_VERDICTS or (isinstance(pre_state, dict) and pre_state.get("synthetic_injection")):
        return None
    if case.outcome == "VERIFIED_SUCCESS" and _eligible(case):
        return "verified_fix"
    if case.operator_verdict == "CORRECT":
        return "diagnosis_confirmed"
    if incident.status == "RESOLVED" and case.outcome not in _EXECUTED and incident.id not in recurred:
        return "self_resolved"
    return None


def _minutes(incident: Incident) -> int | None:
    if incident.status != "RESOLVED" or incident.updated_at is None or incident.detected_at is None:
        return None
    return max(0, round((incident.updated_at - incident.detected_at).total_seconds() / 60))


def _reference(case: RemediationCase, action: Action, incident: Incident, kind: str) -> dict:
    return {
        "kind": kind, "case_id": case.id, "ceph_code": incident.ceph_code, "playbook_id": action.action_id,
        "diagnosis": (case.diagnosis or incident.diagnosis_text or "")[:400],
        "resolved_after_minutes": _minutes(incident), "operator_verdict": case.operator_verdict,
    }


def find_reference_cases(
    session, *, incident_id: str, cluster_id: str | None, include_unscoped: bool, ceph_code: str,
    nodes: list[str] | None, ceph_version: str | None, deployment_mode: str | None, limit: int = 3,
) -> list[dict]:
    family = fault_family(ceph_code)
    rows = (
        session.query(RemediationCase, Action, Incident)
        .join(Action, Action.id == RemediationCase.action_id)
        .join(Incident, Incident.id == RemediationCase.incident_id)
        .filter(RemediationCase.incident_id != incident_id,
                _family_filter(RemediationCase.fault_family, family),
                _scope(RemediationCase.cluster_id, cluster_id, include_unscoped))
        .order_by(RemediationCase.created_at.desc()).limit(CANDIDATES).all()
    )
    recurred = _recurred(session, [incident.id for _case, _action, incident in rows])
    wanted_nodes, wanted_major = _normalized_nodes(nodes), _major(ceph_version)
    scored: list[tuple[int, dict]] = []
    for case, action, incident in rows:
        kind = _kind(case, incident, recurred)
        case_major = _major(case.ceph_version)
        if kind is None or "unknown" not in (case_major, wanted_major) and case_major != wanted_major:
            continue
        entities = _load(case.entity_keys_json, {})
        same_nodes = _normalized_nodes(entities.get("nodes") if isinstance(entities, dict) else None) == wanted_nodes
        score = (_KIND_WEIGHT[kind] + 5 * (incident.ceph_code == ceph_code) + 3 * same_nodes
                 + ((case.deployment_mode or "unknown") == (deployment_mode or "unknown")))
        scored.append((score, _reference(case, action, incident, kind)))
    return _top(scored, limit) + lab_references(session, family, incident_id=incident_id)


# FL3: Failure Lab runs reproduce a fault on a lab cluster with a known cause.
LAB_LABEL_EVENT = "failure_lab_label"
LAB_REFERENCES = 2


def lab_references(session, family: str, *, incident_id: str, limit: int = LAB_REFERENCES) -> list[dict]:
    """The newest lab reproductions of this fault family, from any cluster, with their known cause."""
    rows = (
        session.query(IncidentTimelineEvent, Incident)
        .join(Incident, Incident.id == IncidentTimelineEvent.incident_id)
        .filter(IncidentTimelineEvent.event_type == LAB_LABEL_EVENT,
                IncidentTimelineEvent.incident_id != incident_id,
                _family_filter(Incident.ceph_code, family))
        .order_by(IncidentTimelineEvent.created_at.desc()).limit(limit).all()
    )
    references = []
    for event, incident in rows:
        raw = _load(event.evidence_json, {})
        label: dict = raw if isinstance(raw, dict) else {}
        if not label.get("cause"):
            continue
        raw_stages = label.get("stages")
        stages: dict = raw_stages if isinstance(raw_stages, dict) else {}
        references.append({
            "kind": "lab_reproduced", "case_id": None, "ceph_code": incident.ceph_code,
            "playbook_id": None, "diagnosis": str(label["cause"])[:400],
            "acceptable_action_ids": list(label.get("acceptable_action_ids") or []),
            "ai_diagnosis_correct": stages.get("diagnosis"), "resolved_after_minutes": _minutes(incident),
            "operator_verdict": None,
        })
    return references


def _top(scored: list[tuple[int, dict]], limit: int) -> list[dict]:
    """Best first (stable: newest first among equals); one self-resolved case is
    enough to make that point, the history block gives the counts."""
    chosen: list[dict] = []
    seen: set[str] = set()
    for _score, reference in sorted(scored, key=lambda item: -item[0]):
        text = reference["diagnosis"].strip()[:160]
        if reference["kind"] == "self_resolved":
            if any(item["kind"] == "self_resolved" for item in chosen):
                continue
        elif not text or text in seen:
            continue  # a confirmed diagnosis with no text, or the same text again, teaches nothing
        seen.add(text)
        chosen.append(reference)
        if len(chosen) >= max(1, limit):
            break
    return chosen


def proposal_history(
    session, *, cluster_id: str | None, include_unscoped: bool, ceph_code: str,
    now: datetime | None = None, days: int = HISTORY_DAYS,
) -> list[dict]:
    """How each real action proposed for this fault family ended in ``days``.

    Only actions whose proposal was not needed at least HISTORY_MIN_NOT_NEEDED
    times are returned: "not needed" means the incident resolved before
    approval (auto-cancelled) or an operator rejected it.
    """
    since = (now or utc_now()) - timedelta(days=days)
    actions = (
        session.query(Action.id, Action.action_id)
        .join(Incident, Incident.id == Action.incident_id)
        .filter(Action.created_at >= since, Action.action_id != NO_ACTION,
                _family_filter(Incident.ceph_code, fault_family(ceph_code)),
                _scope(Incident.cluster_id, cluster_id, include_unscoped))
        .all()
    )
    playbook_of = dict(actions)
    counts: dict[str, Counter] = defaultdict(Counter)
    for _pk, playbook in actions:
        counts[playbook]["proposed"] += 1
    if playbook_of:
        _count_outcomes(session, playbook_of, counts)
    return sorted(
        ({"action_id": playbook, **dict(counter)} for playbook, counter in counts.items()
         if counter["self_resolved"] + counter["operator_rejected"] >= HISTORY_MIN_NOT_NEEDED),
        key=lambda item: -item["proposed"],
    )


def _count_outcomes(session, playbook_of: dict[str, str], counts: dict[str, Counter]) -> None:
    events = session.query(AuditEntry.action_id, AuditEntry.event_type, AuditEntry.actor).filter(
        AuditEntry.action_id.in_(list(playbook_of)),
        AuditEntry.event_type.in_([audit.EVENT_RISKY_ACTION_AUTO_CANCELLED_INCIDENT_RESOLVED,
                                   audit.EVENT_RISKY_ACTION_REJECTED]),
    )
    for action_pk, event_type, actor in events:
        if event_type == audit.EVENT_RISKY_ACTION_AUTO_CANCELLED_INCIDENT_RESOLVED:
            counts[playbook_of[action_pk]]["self_resolved"] += 1
        elif not str(actor).startswith("system"):
            counts[playbook_of[action_pk]]["operator_rejected"] += 1
    verified = session.query(RemediationCase.action_id).filter(
        RemediationCase.action_id.in_(list(playbook_of)), RemediationCase.outcome == "VERIFIED_SUCCESS")
    for (action_pk,) in verified:
        counts[playbook_of[action_pk]]["verified_success"] += 1


_KIND_LABEL = {
    "verified_fix": "đã sửa và xác minh",
    "diagnosis_confirmed": "operator xác nhận chẩn đoán đúng",
    "self_resolved": "tự hết, không cần hành động, không tái phát",
    "lab_reproduced": "tái hiện trên cụm lab, nguyên nhân đã biết",
}


def references_block(references: list[dict]) -> str:
    if not references:
        return ""
    lines = ["Case cũ cùng loại lỗi (chỉ tham khảo; không cấp quyền thực thi):"]
    for item in references:
        resolved = item.get("resolved_after_minutes")
        after = f", hết sau ~{resolved} phút" if resolved is not None else ""
        playbook = item.get("playbook_id")
        if item.get("kind") == "verified_fix":
            handled = f"→ {playbook}"
        elif playbook and playbook != NO_ACTION:
            handled = f"(đã đề xuất {playbook} nhưng KHÔNG chạy)"
        else:
            handled = "(không hành động)"
        kind = str(item.get("kind"))
        if kind == "lab_reproduced":
            accepted = ", ".join(item.get("acceptable_action_ids") or []) or NO_ACTION
            correct = item.get("ai_diagnosis_correct")
            verdict = ("AI lần đó chẩn đoán ĐÚNG" if correct is True
                       else "AI lần đó chẩn đoán SAI" if correct is False else "chưa chấm chẩn đoán")
            lines.append(f"  - [tái hiện trên cụm lab, nguyên nhân đã biết] {item.get('ceph_code')}: "
                         f"{item.get('diagnosis')} Hành động chấp nhận: {accepted}. ({verdict})")
            continue
        lines.append(f"  - [{_KIND_LABEL.get(kind, kind)}{after}] {item.get('ceph_code')} "
                     f"{handled}: {item.get('diagnosis') or '(không lưu chẩn đoán)'}")
    return "\n".join(lines) + "\n"


def history_block(history: list[dict], days: int = HISTORY_DAYS) -> str:
    if not history:
        return ""
    lines = [f"Đề xuất trước đây cho loại lỗi này ({days} ngày) thường KHÔNG cần thiết:"]
    for item in history:
        lines.append(
            f"  - {item['action_id']}: đề xuất {item.get('proposed', 0)} lần; sự cố tự hết trước khi duyệt "
            f"{item.get('self_resolved', 0)} lần; operator từ chối {item.get('operator_rejected', 0)} lần; "
            f"sửa thành công đã xác minh {item.get('verified_success', 0)} lần.")
    lines.append("Chỉ đề xuất lại các hành động trên nếu bằng chứng hiện tại khác hẳn những lần đó (nêu rõ khác ở "
                 "đâu); nếu không, chọn investigate_manually và nói rõ sự cố loại này thường tự hết.")
    return "\n".join(lines) + "\n"
