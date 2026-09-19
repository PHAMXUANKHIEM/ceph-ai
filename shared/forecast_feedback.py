"""Operator feedback for predictive resource alerts."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import func

from shared.models import (
    Cluster,
    Incident,
    NodeResourceForecastAlert,
    NodeResourceForecastFeedback,
    NodeResourceForecastRun,
    RemediationCase,
)


class ForecastVerdict(StrEnum):
    TRUE_POSITIVE = "TRUE_POSITIVE"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"
    UNKNOWN = "UNKNOWN"


def add_feedback(
    session,
    *,
    alert_id: str,
    verdict: str,
    submitted_by: str,
    note: str | None = None,
    impact_percent: float | None = None,
    incident_id: str | None = None,
    remediation_case_id: str | None = None,
) -> NodeResourceForecastFeedback:
    """Validate and append one operator verdict without changing alert state."""
    normalized = str(verdict or "").strip().upper()
    if normalized not in {item.value for item in ForecastVerdict}:
        raise ValueError(f"unsupported forecast verdict: {verdict}")
    if session.get(NodeResourceForecastAlert, alert_id) is None:
        raise LookupError(f"forecast alert not found: {alert_id}")
    alert = session.get(NodeResourceForecastAlert, alert_id)
    cluster = session.query(Cluster).filter_by(name=alert.cluster_name).one_or_none()
    incident = None
    if incident_id:
        incident = session.get(Incident, incident_id)
        if incident is None:
            raise LookupError(f"incident not found: {incident_id}")
        if cluster is not None and incident.cluster_id != cluster.id:
            raise ValueError("incident does not belong to the forecast alert cluster")
    case = None
    if remediation_case_id:
        case = session.get(RemediationCase, remediation_case_id)
        if case is None:
            raise LookupError(f"remediation case not found: {remediation_case_id}")
        if cluster is not None and case.cluster_id != cluster.id:
            raise ValueError("remediation case does not belong to the forecast alert cluster")
        if incident is not None and case.incident_id != incident.id:
            raise ValueError("remediation case does not belong to the linked incident")
    feedback = NodeResourceForecastFeedback(
        alert_id=alert_id,
        verdict=normalized,
        note=(note or "").strip() or None,
        impact_percent=impact_percent,
        incident_id=(incident_id or "").strip() or None,
        remediation_case_id=(remediation_case_id or "").strip() or None,
        submitted_by=(submitted_by or "system").strip()[:64],
    )
    session.add(feedback)
    return feedback


def summarize_feedback(session, *, cluster_name: str, trigger_threshold: float) -> dict:
    """Return labeled precision/coverage and a conservative recall estimate."""
    rows = (
        session.query(
            NodeResourceForecastFeedback.verdict,
            func.count(NodeResourceForecastFeedback.id),
        )
        .join(NodeResourceForecastAlert, NodeResourceForecastAlert.id == NodeResourceForecastFeedback.alert_id)
        .filter(NodeResourceForecastAlert.cluster_name == cluster_name)
        .group_by(NodeResourceForecastFeedback.verdict)
        .all()
    )
    verdict_counts = dict(rows)
    true_positive_alerts = (
        session.query(func.count(func.distinct(NodeResourceForecastFeedback.alert_id)))
        .join(NodeResourceForecastAlert, NodeResourceForecastAlert.id == NodeResourceForecastFeedback.alert_id)
        .filter(
            NodeResourceForecastAlert.cluster_name == cluster_name,
            NodeResourceForecastFeedback.verdict == ForecastVerdict.TRUE_POSITIVE.value,
        )
        .scalar()
        or 0
    )
    ground_truth = (
        session.query(func.count(NodeResourceForecastRun.id))
        .filter(
            NodeResourceForecastRun.cluster_name == cluster_name,
            NodeResourceForecastRun.status == "EVALUATED",
            NodeResourceForecastRun.actual_percent >= trigger_threshold,
        )
        .scalar()
        or 0
    )
    labeled = sum(verdict_counts.values())
    positives = verdict_counts.get(ForecastVerdict.TRUE_POSITIVE.value, 0)
    false_positives = verdict_counts.get(ForecastVerdict.FALSE_POSITIVE.value, 0)
    return {
        "verdict_counts": verdict_counts,
        "labeled_count": labeled,
        "precision": round(positives / (positives + false_positives), 4)
        if positives + false_positives else None,
        "ground_truth_breach_count": ground_truth,
        "recall": round(true_positive_alerts / ground_truth, 4)
        if ground_truth else None,
        "recall_reason": (
            "Đo trên evaluated forecast outcomes vượt trigger threshold; cần telemetry độc lập để recall đầy đủ."
            if ground_truth else "Chưa có evaluated outcome vượt trigger threshold để đo recall."
        ),
    }
