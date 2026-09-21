"""Evidence-linked deterministic root-cause chain."""

from __future__ import annotations

from typing import Any


def _kind(event: dict[str, Any]) -> str:
    return str(event.get("kind") or "").casefold()


def _root_signal(event: dict[str, Any]) -> bool:
    value = _kind(event) + " " + str(event.get("summary") or "").casefold()
    return any(token in value for token in (
        "detected", "anomaly", "health", "osd_down", "latency", "scrub",
        "inconsistent", "degraded", "error",
    ))


def _response_event(event: dict[str, Any]) -> bool:
    value = _kind(event)
    return any(token in value for token in (
        "action", "approved", "executed", "postcheck", "rollback", "resolved",
    ))


def build_root_cause_chain(timeline: dict[str, Any] | None) -> dict[str, Any]:
    source = timeline if isinstance(timeline, dict) else {}
    events = [event for event in source.get("events", []) if isinstance(event, dict)]
    events.sort(key=lambda event: (str(event.get("at") or ""), str(event.get("id") or "")))
    gaps: list[dict[str, str]] = []
    if not events:
        return {
            "status": "not_available",
            "chain": [],
            "hypotheses": [],
            "evidence_gaps": [{"code": "TIMELINE_EMPTY", "reason": "Không có event để dựng root cause chain."}],
            "read_only": True,
            "action_id": None,
        }

    roots = [event for event in events if _root_signal(event)]
    root = roots[0] if roots else events[0]
    if not roots:
        gaps.append({
            "code": "ROOT_SIGNAL_NOT_OBSERVED",
            "reason": "Timeline chỉ có lifecycle event; chưa có metric/health/log signal.",
        })
    if not any(_response_event(event) for event in events):
        gaps.append({
            "code": "RESPONSE_OUTCOME_NOT_OBSERVED",
            "reason": "Chưa có action/post-check/resolution event để đánh giá response.",
        })

    chain = []
    for index, event in enumerate(events):
        event_id = str(event.get("id") or f"event:{index}")
        if event is root:
            role = "root_candidate"
            relation = "earliest supported signal"
            confidence = 0.65 if _root_signal(event) else 0.35
        elif _response_event(event):
            role = "response_or_effect"
            relation = "operator/system response after signal"
            confidence = 0.9
        elif str(event.get("at") or "") < str(root.get("at") or ""):
            role = "preceding_context"
            relation = "context before candidate"
            confidence = 0.3
        else:
            role = "contributing_or_effect"
            relation = "event after candidate; causality not proven"
            confidence = 0.35
        chain.append({
            "event_id": event_id,
            "at": event.get("at"),
            "kind": event.get("kind"),
            "role": role,
            "relation": relation,
            "confidence": confidence,
            "citations": [event_id],
        })

    root_id = str(root.get("id") or "unknown")
    return {
        "status": "observed",
        "incident_id": source.get("incident_id"),
        "ceph_code": source.get("ceph_code"),
        "chain": chain,
        "hypotheses": [{
            "id": "root-candidate-1",
            "statement": str(root.get("summary") or root.get("kind") or "Root signal"),
            "confidence": 0.65 if _root_signal(root) else 0.35,
            "citations": [root_id],
            "causality": "candidate_only",
        }],
        "evidence_gaps": gaps,
        "read_only": True,
        "recommendation_mode": "EVIDENCE_ONLY",
        "action_id": None,
    }
