"""Fail-closed delayed-feedback policy for offline/staging evaluation.

This is the stable Ceph-AI contract around a future NannyML adapter. The
adapter may estimate performance without immediate labels, but it cannot
promote a model until verified outcomes satisfy coverage and confidence gates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Iterable, Mapping


class FeedbackStatus(StrEnum):
    NO_LABEL = "NO_LABEL"
    PENDING_LABEL = "PENDING_LABEL"
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    VERIFIED_FAILURE = "VERIFIED_FAILURE"
    INCONCLUSIVE = "INCONCLUSIVE"


def _utc(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("feedback timestamp is required")
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


@dataclass(frozen=True)
class DelayedFeedback:
    scope_key: str
    sample_id: str
    observed_at: datetime
    predicted: float | None
    actual: float | None
    status: FeedbackStatus
    absolute_error: float | None


@dataclass(frozen=True)
class DelayedPerformance:
    scope_key: str
    total: int
    verified: int
    coverage: float
    confidence: float
    verified_mae: float | None
    estimated_mae: float | None
    status: str
    promotion_allowed: bool


@dataclass(frozen=True)
class DelayedPerformanceComparison:
    scope_key: str
    estimated_mae: float | None
    verified_mae: float | None
    absolute_delta: float | None
    verified_count: int
    status: str


def classify_feedback(
    row: Mapping[str, object], *, now: datetime, label_sla_hours: float = 6.0,
    tolerance: float = 5.0,
) -> DelayedFeedback:
    scope_key = str(row.get("scope_key") or "").strip()
    sample_id = str(row.get("sample_id") or "").strip()
    observed_at = _utc(row.get("observed_at"))
    predicted = _number(row.get("predicted"))
    actual = _number(row.get("actual", row.get("label")))
    explicit = str(row.get("feedback_status") or "").strip().upper()
    try:
        status = FeedbackStatus(explicit) if explicit else None
    except ValueError:
        status = FeedbackStatus.INCONCLUSIVE
    error = abs(actual - predicted) if actual is not None and predicted is not None else None
    if status is None:
        age = _utc(now) - observed_at
        if actual is None:
            status = FeedbackStatus.NO_LABEL if age > timedelta(hours=label_sla_hours) else FeedbackStatus.PENDING_LABEL
        else:
            status = FeedbackStatus.VERIFIED_SUCCESS if (error or 0.0) <= tolerance else FeedbackStatus.VERIFIED_FAILURE
    if status not in {FeedbackStatus.VERIFIED_SUCCESS, FeedbackStatus.VERIFIED_FAILURE}:
        error = None
    return DelayedFeedback(scope_key, sample_id, observed_at, predicted, actual, status, error)


def estimate_performance(
    rows: Iterable[Mapping[str, object]], *, now: datetime | None = None,
    label_sla_hours: float = 6.0, tolerance: float = 5.0,
    minimum_verified: int = 3, minimum_coverage: float = 0.6,
) -> tuple[DelayedPerformance, ...]:
    """Estimate each scope independently; never writes or promotes anything."""

    reference_now = now or datetime.now(timezone.utc)
    grouped: dict[str, list[DelayedFeedback]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        feedback = classify_feedback(
            row, now=reference_now, label_sla_hours=label_sla_hours,
            tolerance=tolerance,
        )
        identity = (feedback.scope_key, feedback.sample_id)
        if identity in seen:
            continue
        seen.add(identity)
        grouped.setdefault(feedback.scope_key, []).append(feedback)
    output: list[DelayedPerformance] = []
    for scope_key in sorted(grouped):
        items = grouped[scope_key]
        verified = [item for item in items if item.status in {
            FeedbackStatus.VERIFIED_SUCCESS, FeedbackStatus.VERIFIED_FAILURE,
        } and item.absolute_error is not None]
        coverage = len(verified) / len(items) if items else 0.0
        verified_mae = sum(item.absolute_error for item in verified) / len(verified) if verified else None
        # Until an optional NannyML adapter supplies a calibrated estimate,
        # the conservative estimate equals verified MAE only when coverage is
        # sufficient. Otherwise it is deliberately unavailable.
        enough = len(verified) >= minimum_verified and coverage >= minimum_coverage
        output.append(DelayedPerformance(
            scope_key=scope_key, total=len(items), verified=len(verified),
            coverage=coverage, confidence=coverage, verified_mae=verified_mae,
            estimated_mae=verified_mae if enough else None,
            status="READY" if enough else "INSUFFICIENT_CONFIDENCE",
            promotion_allowed=False,
        ))
    return tuple(output)


def compare_estimate_with_verified(
    estimated_mae_by_scope: Mapping[str, float | None],
    rows: Iterable[Mapping[str, object]], *,
    now: datetime | None = None, tolerance: float = 5.0,
) -> tuple[DelayedPerformanceComparison, ...]:
    """Reconcile a delayed estimator once verified labels become available."""

    verified_by_scope: dict[str, list[float]] = {}
    reference_now = now or datetime.now(timezone.utc)
    for row in rows:
        feedback = classify_feedback(row, now=reference_now, tolerance=tolerance)
        if feedback.status in {FeedbackStatus.VERIFIED_SUCCESS, FeedbackStatus.VERIFIED_FAILURE}:
            if feedback.absolute_error is not None:
                verified_by_scope.setdefault(feedback.scope_key, []).append(feedback.absolute_error)
    scopes = sorted(set(estimated_mae_by_scope) | set(verified_by_scope))
    result = []
    for scope in scopes:
        estimated = estimated_mae_by_scope.get(scope)
        verified_values = verified_by_scope.get(scope, [])
        verified = sum(verified_values) / len(verified_values) if verified_values else None
        delta = abs(verified - estimated) if verified is not None and estimated is not None else None
        result.append(DelayedPerformanceComparison(
            scope_key=scope, estimated_mae=estimated, verified_mae=verified,
            absolute_delta=delta, verified_count=len(verified_values),
            status="MATCH" if delta is not None and delta <= tolerance else (
                "DIVERGED" if delta is not None else "INSUFFICIENT_DATA"
            ),
        ))
    return tuple(result)
