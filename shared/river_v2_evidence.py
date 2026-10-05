"""River v2 promotion evidence by scope, with a shadow verdict (read-only).

Plan 7.2: before any promotion is even requested, report per
cluster/host/metric scope how many independent verified outcomes exist, how
many were scored (consumed by the learner), rejected (revoked), inconclusive
or stale, how old the samples are, the data-quality ratio of the learner's
audit stream, and whether any label traces back to the candidate model
itself.  The verdict is ``KEEP_SHADOW`` unless every threshold is met, in
which case it is ``ELIGIBLE_FOR_REVIEW`` — an operator decision, never an
automatic promotion.

Shared by scripts/river_v2_promotion_evidence.py (CLI) and the weekly AI Ops
digest (worker/ai_ops_digest.py, autonomy plan WP7).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

SCHEMA = "ceph-ai.river-v2-promotion-evidence.v1"
VERIFIED = ("VERIFIED_SUCCESS", "VERIFIED_FAILED")
READY_TO_LEARN = "READY_TO_LEARN"
CANDIDATE_PREFIX = "river"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _self_labelled(label: Any) -> bool:
    """A label produced from the candidate's own forecast is not independent."""
    version = str(label.source_model_version or "").lower()
    actor = str(label.source_actor or "").lower()
    return version.startswith(CANDIDATE_PREFIX) or actor.startswith(CANDIDATE_PREFIX)


def _new_scope() -> dict[str, Any]:
    return {
        "verified": 0, "scored": 0, "ready": 0, "rejected": 0, "inconclusive": 0,
        "stale": 0, "self_labelled": 0, "ages_hours": [],
        "audit_samples": 0, "audit_ready_to_learn": 0,
    }


def _add_label(scope: dict[str, Any], label: Any, *, now: datetime, stale_after: timedelta) -> None:
    observed = _utc(label.outcome_observed_at or label.observed_at)
    age = now - observed if observed else None
    if label.status == "REVOKED":
        scope["rejected"] += 1
        return
    if label.outcome not in VERIFIED:
        scope["inconclusive"] += 1
        return
    if _self_labelled(label):
        scope["self_labelled"] += 1
        return
    if not label.evidence_fingerprint:
        scope["inconclusive"] += 1
        return
    scope["verified"] += 1
    if label.status == "CONSUMED":
        scope["scored"] += 1
    elif label.status == "READY":
        scope["ready"] += 1
        if age is not None and age > stale_after:
            scope["stale"] += 1
    if age is not None:
        scope["ages_hours"].append(age.total_seconds() / 3600)


def _finish(key: tuple[str, str, str], scope: dict[str, Any]) -> dict[str, Any]:
    ages = scope.pop("ages_hours")
    audit_samples = scope.pop("audit_samples")
    ready = scope.pop("audit_ready_to_learn")
    return {
        "cluster_key": key[0], "host": key[1], "metric": key[2], **scope,
        "sample_age_hours": {
            "newest": round(min(ages), 2) if ages else None,
            "median": round(median(ages), 2) if ages else None,
            "oldest": round(max(ages), 2) if ages else None,
        },
        "data_quality": {
            "audit_samples": audit_samples,
            "ready_to_learn_ratio": round(ready / audit_samples, 4) if audit_samples else None,
        },
    }


def verdict(scopes: list[dict[str, Any]], *, min_verified: int, min_scopes: int,
            min_clusters: int, min_per_scope: int, min_quality: float) -> dict[str, Any]:
    qualifying = [scope for scope in scopes if scope["verified"] >= min_per_scope]
    total = sum(scope["verified"] for scope in scopes)
    clusters = {scope["cluster_key"] for scope in qualifying}
    reasons = []
    if total < min_verified:
        reasons.append(f"verified independent outcomes {total} < {min_verified}")
    if len(qualifying) < min_scopes:
        reasons.append(f"scopes with >= {min_per_scope} verified outcomes {len(qualifying)} < {min_scopes}")
    if len(clusters) < min_clusters:
        reasons.append(f"clusters covered {len(clusters)} < {min_clusters}")
    self_labelled = sum(scope["self_labelled"] for scope in scopes)
    if self_labelled:
        reasons.append(f"{self_labelled} label(s) trace back to the candidate model")
    low_quality = [
        f"{scope['cluster_key']}/{scope['host']}/{scope['metric']}"
        for scope in qualifying
        if (scope["data_quality"]["ready_to_learn_ratio"] or 0.0) < min_quality
    ]
    if low_quality:
        reasons.append(f"data-quality ratio below {min_quality} in: {', '.join(low_quality[:10])}")
    return {
        "decision": "ELIGIBLE_FOR_REVIEW" if not reasons else "KEEP_SHADOW",
        "reasons": reasons,
        "verified_total": total,
        "qualifying_scopes": len(qualifying),
        "clusters": sorted(clusters),
        "note": "Evidence only. Promotion still requires temporal holdout, operator approval and rollback rehearsal.",
    }


def collect(session, *, now: datetime, stale_after: timedelta, audit_window: timedelta) -> tuple[list[dict], dict]:
    from sqlalchemy import func, literal_column, select

    from shared.models import OnlineLearnerAudit, OnlineLearnerLabel, OnlineLearnerLabelEvent

    scopes: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(_new_scope)
    for label in session.scalars(select(OnlineLearnerLabel)):
        key = (label.cluster_key, label.host, label.metric)
        _add_label(scopes[key], label, now=now, stale_after=stale_after)

    since = (now - audit_window).replace(tzinfo=None)
    audit_rows = session.execute(
        select(
            OnlineLearnerAudit.cluster_key, OnlineLearnerAudit.host, OnlineLearnerAudit.metric,
            OnlineLearnerAudit.quality_status, func.count(OnlineLearnerAudit.id),
        ).where(OnlineLearnerAudit.created_at >= since).group_by(
            OnlineLearnerAudit.cluster_key, OnlineLearnerAudit.host,
            OnlineLearnerAudit.metric, OnlineLearnerAudit.quality_status,
        )
    ).all()
    for cluster_key, host, metric, status, count in audit_rows:
        key = (cluster_key, host, metric)
        if key not in scopes:
            continue
        scopes[key]["audit_samples"] += int(count)
        if status == READY_TO_LEARN:
            scopes[key]["audit_ready_to_learn"] += int(count)

    # Inline constants: with bound parameters PostgreSQL sees the SELECT and
    # GROUP BY substr() calls as different expressions.
    reason = func.substr(OnlineLearnerLabelEvent.reason, literal_column("1"), literal_column("120"))
    blocked = session.execute(
        select(reason, func.count(OnlineLearnerLabelEvent.id))
        .where(OnlineLearnerLabelEvent.action == "BLOCKED")
        .group_by(reason)
        .order_by(func.count(OnlineLearnerLabelEvent.id).desc())
        .limit(20)
    ).all()
    finished = sorted(
        (_finish(key, value) for key, value in scopes.items()),
        key=lambda item: (-item["verified"], item["cluster_key"], item["host"], item["metric"]),
    )
    return finished, {reason: int(count) for reason, count in blocked}


DEFAULT_THRESHOLDS = {
    "min_verified": 100,
    "min_scopes": 3,
    "min_clusters": 2,
    "min_per_scope": 20,
    "min_quality": 0.8,
}


def build_report(session, *, now: datetime, stale_days: float = 7.0, audit_window_days: float = 30.0,
                 thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    """The full evidence report; reads only."""
    from config.settings import settings

    scopes, blocked = collect(
        session, now=now, stale_after=timedelta(days=stale_days),
        audit_window=timedelta(days=audit_window_days),
    )
    return {
        "schema": SCHEMA,
        "read_only": True,
        "generated_at": now.isoformat(),
        "algorithm": "river_linear_v2",
        "execution_mode": settings.online_learning_mode,
        "online_learning_enabled": bool(getattr(settings, "online_learning_enabled", False)),
        "totals": {
            key: sum(scope[key] for scope in scopes)
            for key in ("verified", "scored", "ready", "rejected", "inconclusive", "stale", "self_labelled")
        },
        "scopes": scopes,
        "blocked_reasons": blocked,
        "verdict": verdict(scopes, **{**DEFAULT_THRESHOLDS, **(thresholds or {})}),
    }
