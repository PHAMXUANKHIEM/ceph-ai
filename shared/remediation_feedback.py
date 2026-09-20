"""Read-only operator feedback metrics for AI remediation diagnoses."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from shared.time import utc_now

from sqlalchemy import or_

from shared.models import Cluster, RemediationCase

POSITIVE_VERDICTS = {"CORRECT"}
NEGATIVE_VERDICTS = {"FALSE_POSITIVE", "UNSAFE", "INEFFECTIVE"}
SCORED_VERDICTS = POSITIVE_VERDICTS | NEGATIVE_VERDICTS
VERIFIED_OUTCOMES = {"VERIFIED_SUCCESS", "VERIFIED_FAILED", "EXECUTION_FAILED"}


def _has_regression(row: RemediationCase) -> bool:
    return any(value is True for value in (
        row.regressed_1h, row.regressed_24h, row.regressed_7d,
    ))


def _telemetry_score(rows: list[RemediationCase]) -> dict:
    """Score deterministic post-check truth separately from operator labels."""
    correct = sum(
        row.outcome == "VERIFIED_SUCCESS" and not _has_regression(row)
        for row in rows
    )
    incorrect = sum(
        row.outcome in {"VERIFIED_FAILED", "EXECUTION_FAILED"} or _has_regression(row)
        for row in rows
    )
    scored = correct + incorrect
    return {
        "correct": correct,
        "incorrect": incorrect,
        "scored": scored,
        "precision_percent": round(correct * 100 / scored, 2) if scored else None,
    }


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
    now = now or utc_now()
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
    telemetry_scored = [row for row in rows if row.outcome in VERIFIED_OUTCOMES or _has_regression(row)]
    telemetry = _telemetry_score(telemetry_scored)
    outcome_counts = {
        outcome: sum(row.outcome == outcome for row in rows)
        for outcome in sorted({row.outcome for row in rows})
    }
    outcome_counts["REGRESSED"] = sum(_has_regression(row) for row in rows)

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
        # Operator feedback is intentionally kept separate from deterministic
        # telemetry truth.  This makes a zero operator-label count diagnosable
        # instead of hiding already verified outcomes behind it.
        "telemetry_labeled": len(telemetry_scored),
        "telemetry_scored": telemetry["scored"],
        "telemetry_correct": telemetry["correct"],
        "telemetry_incorrect": telemetry["incorrect"],
        "telemetry_precision_percent": telemetry["precision_percent"],
        "telemetry_unlabeled": max(0, len(rows) - len(telemetry_scored)),
        "outcome_counts": outcome_counts,
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
