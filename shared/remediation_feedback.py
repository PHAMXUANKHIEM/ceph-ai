"""Read-only operator feedback metrics for AI remediation diagnoses."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import or_

from shared.models import Cluster, RemediationCase

POSITIVE_VERDICTS = {"CORRECT"}
NEGATIVE_VERDICTS = {"FALSE_POSITIVE", "UNSAFE", "INEFFECTIVE"}
SCORED_VERDICTS = POSITIVE_VERDICTS | NEGATIVE_VERDICTS


def _score(rows: list[RemediationCase]) -> dict:
    correct = sum(row.operator_verdict in POSITIVE_VERDICTS for row in rows)
    incorrect = sum(row.operator_verdict in NEGATIVE_VERDICTS for row in rows)
    scored = correct + incorrect
    return {
        "correct": correct,
        "incorrect": incorrect,
        "scored": scored,
        "precision_percent": round(correct * 100 / scored, 2) if scored else None,
    }


def summary(session, *, cluster_id: str, now: datetime | None = None) -> dict:
    """Return cluster-scoped feedback coverage, precision and recent trend."""
    now = now or datetime.utcnow()
    cluster = session.get(Cluster, cluster_id)
    query = session.query(RemediationCase)
    if cluster is not None and cluster.is_default:
        query = query.filter(or_(RemediationCase.cluster_id == cluster_id, RemediationCase.cluster_id.is_(None)))
    else:
        query = query.filter(RemediationCase.cluster_id == cluster_id)
    rows = query.order_by(RemediationCase.created_at.desc()).all()
    labeled = [row for row in rows if row.operator_verdict]
    scored = [row for row in labeled if row.operator_verdict in SCORED_VERDICTS]
    inconclusive = sum(row.operator_verdict == "INCONCLUSIVE" for row in labeled)
    overall = _score(scored)

    recent_cutoff = now - timedelta(days=30)
    recent_scored = [
        row for row in scored
        if (row.operator_verdict_at or row.updated_at or row.created_at) >= recent_cutoff
    ]
    recent = _score(recent_scored)

    by_family: dict[str, list[RemediationCase]] = defaultdict(list)
    for row in scored:
        by_family[row.fault_family].append(row)
    families = [
        {"fault_family": family, **_score(items)}
        for family, items in by_family.items()
    ]
    families.sort(key=lambda item: (-item["scored"], item["fault_family"]))

    eligible = len(rows)
    return {
        **overall,
        "total_cases": eligible,
        "labeled": len(labeled),
        "unlabeled": eligible - len(labeled),
        "inconclusive": inconclusive,
        "coverage_percent": round(len(labeled) * 100 / eligible, 2) if eligible else None,
        "recent_30d": recent,
        "by_fault_family": families[:20],
        "recent_unlabeled": [
            {
                "id": row.id,
                "incident_id": row.incident_id,
                "fault_family": row.fault_family,
                "diagnosis": row.diagnosis,
                "created_at": row.created_at,
            }
            for row in rows if not row.operator_verdict
        ][:20],
    }
