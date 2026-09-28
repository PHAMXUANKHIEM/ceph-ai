"""Paired forecast evaluation and guarded model promotion.

This module is intentionally independent from a particular learner.  A
champion and challenger must be scored on the same input window, target
timestamp, and quality decision before the result can become promotion
evidence.  Database helpers below persist that evidence and enforce the
approval, expiry, append-only audit, and rollback boundaries.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Iterable

from shared.models import (
    ForecastEvaluationEvidence,
    ForecastModelRegistry,
    ForecastPromotionApproval,
    ForecastPromotionAudit,
)


GOOD_QUALITY_STATUSES = frozenset({"OK", "HEALTHY", "VALID"})
PROMOTION_EVENTS = frozenset({
    "PROMOTION_REQUESTED",
    "PROMOTION_BLOCKED",
    "PROMOTION_APPROVED",
    "PROMOTED",
    "PROMOTION_EXPIRED",
    "ROLLBACK_REQUESTED",
    "ROLLED_BACK",
    "ROLLBACK_BLOCKED",
    "HEALTH_VERIFIED",
    "HEALTH_FAILED",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value))


def _smape(actual: float, predicted: float) -> float:
    denominator = (abs(actual) + abs(predicted)) / 2.0
    return 0.0 if denominator == 0 else abs(actual - predicted) / denominator * 100.0


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


@dataclass(frozen=True)
class PairedSample:
    """One champion/challenger observation with shared ground truth."""

    input_window_id: str
    target_at: datetime
    actual: float
    champion_prediction: float
    challenger_prediction: float
    input_window_start: datetime | None = None
    input_window_end: datetime | None = None
    quality_status: str = "OK"
    champion_lower: float | None = None
    champion_upper: float | None = None
    challenger_lower: float | None = None
    challenger_upper: float | None = None
    event_observed: bool | None = None
    champion_detected_at: datetime | None = None
    challenger_detected_at: datetime | None = None
    drift: bool = False


def pair_forecast_runs(champion_runs, challenger_runs) -> list[PairedSample]:
    """Pair persisted forecast rows by input window and target timestamp.

    This adapter deliberately requires the same ``predicted_at``,
    ``window_hours`` and ``target_at``.  Rows that cannot be paired are emitted
    as quality failures instead of being silently compared against a different
    horizon.
    """

    def key(run):
        return (run.predicted_at, run.window_hours, run.target_at)

    champion = {key(run): run for run in champion_runs}
    challenger = {key(run): run for run in challenger_runs}
    pairs: list[PairedSample] = []
    for pair_key in sorted(set(champion) | set(challenger)):
        left = champion.get(pair_key)
        right = challenger.get(pair_key)
        reference = left if left is not None else right
        actual = left.actual_percent if left is not None else right.actual_percent
        same_actual = (
            left is not None and right is not None
            and _finite(actual) and _finite(right.actual_percent)
            and actual == right.actual_percent
        )
        quality = "OK"
        if left is None or right is None or not same_actual:
            quality = "UNPAIRED" if left is None or right is None else "PAIR_MISMATCH"
        elif left.status != "EVALUATED" or right.status != "EVALUATED":
            quality = "INSUFFICIENT_DATA"
        if (
            (left is not None and left.drift_status not in {"OK", ""})
            or (right is not None and right.drift_status not in {"OK", ""})
        ):
            quality = "DRIFT"
        pairs.append(PairedSample(
            input_window_id=f"{reference.predicted_at.isoformat()}|{reference.window_hours}h",
            input_window_start=reference.predicted_at - timedelta(hours=reference.window_hours),
            input_window_end=reference.predicted_at,
            target_at=reference.target_at,
            actual=float(actual) if _finite(actual) else float("nan"),
            champion_prediction=float(left.predicted_percent) if left is not None else float("nan"),
            challenger_prediction=float(right.predicted_percent) if right is not None else float("nan"),
            quality_status=quality,
            drift=quality == "DRIFT" or bool(
                (left is not None and left.promotion_blocked)
                or (right is not None and right.promotion_blocked)
            ),
        ))
    return pairs


@dataclass(frozen=True)
class EvaluationMetrics:
    sample_count: int
    mae: float | None
    rmse: float | None
    smape: float | None
    bias: float | None
    p95_abs_error: float | None
    interval_coverage: float | None
    interval_sample_count: int
    recall: float | None
    delay_seconds: float | None
    quality_ratio: float
    event_count: int
    detected_count: int


@dataclass(frozen=True)
class PairedEvaluation:
    cluster_name: str
    host: str
    metric: str
    horizon_hours: int
    champion_version: str
    challenger_version: str
    input_window_start: datetime | None
    input_window_end: datetime | None
    target_start: datetime | None
    target_end: datetime | None
    champion: EvaluationMetrics
    challenger: EvaluationMetrics
    paired_count: int
    quality_status: str
    drift_count: int
    evidence_hash: str
    evaluated_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class PromotionConfig:
    """Configurable gates; defaults are conservative and fail closed."""

    minimum_samples: int = 30
    minimum_evaluation_streak: int = 3
    minimum_quality_ratio: float = 0.95
    minimum_interval_coverage: float = 0.90
    minimum_recall: float = 0.90
    maximum_delay_seconds: float = 3600.0
    maximum_mae_regression: float = 0.0
    maximum_rmse_regression: float = 0.0
    maximum_smape_regression: float = 0.0
    maximum_p95_regression: float = 0.0
    maximum_recall_regression: float = 0.0
    maximum_delay_regression: float = 0.0
    maximum_bias_abs: float = float("inf")
    evidence_ttl_hours: int = 72


@dataclass(frozen=True)
class PromotionGateResult:
    passed: bool
    scope: tuple[str, str, str, int]
    candidate_version: str
    evidence_hash: str
    evaluation_streak: int
    reasons: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class HealthVerification:
    healthy: bool
    quality_status: str
    reason: str
    checked_at: datetime


def configured_promotion_config(settings_obj=None) -> PromotionConfig:
    """Build the gate from operator settings without enabling promotion."""

    if settings_obj is None:
        from config.settings import settings as settings_obj
    return PromotionConfig(
        minimum_samples=settings_obj.forecast_promotion_min_samples,
        minimum_evaluation_streak=settings_obj.forecast_promotion_min_evaluation_streak,
        minimum_quality_ratio=settings_obj.forecast_promotion_min_quality_ratio,
        minimum_interval_coverage=settings_obj.forecast_promotion_min_interval_coverage,
        minimum_recall=settings_obj.forecast_promotion_min_recall,
        maximum_delay_seconds=settings_obj.forecast_promotion_max_delay_seconds,
        maximum_mae_regression=settings_obj.forecast_promotion_max_mae_regression,
        maximum_rmse_regression=settings_obj.forecast_promotion_max_rmse_regression,
        maximum_smape_regression=settings_obj.forecast_promotion_max_smape_regression,
        maximum_p95_regression=settings_obj.forecast_promotion_max_p95_regression,
        maximum_bias_abs=settings_obj.forecast_promotion_max_bias_abs,
        maximum_recall_regression=settings_obj.forecast_promotion_max_recall_regression,
        maximum_delay_regression=settings_obj.forecast_promotion_max_delay_regression,
        evidence_ttl_hours=settings_obj.forecast_promotion_evidence_ttl_hours,
    )


def _model_metrics(
    samples: list[PairedSample], *, prediction: str,
) -> EvaluationMetrics:
    valid = [
        sample for sample in samples
        if sample.quality_status in GOOD_QUALITY_STATUSES
        and _finite(sample.actual)
        and _finite(getattr(sample, prediction))
    ]
    errors = [sample.actual - float(getattr(sample, prediction)) for sample in valid]
    absolute = [abs(value) for value in errors]
    squared = [value * value for value in errors]
    interval_hits = []
    detected_delays = []
    event_count = 0
    detected_count = 0
    for sample in valid:
        prefix = "champion" if prediction == "champion_prediction" else "challenger"
        lower = getattr(sample, f"{prefix}_lower", None)
        upper = getattr(sample, f"{prefix}_upper", None)
        if _finite(lower) and _finite(upper):
            interval_hits.append(float(lower) <= sample.actual <= float(upper))
        if sample.event_observed:
            event_count += 1
            detected_at = getattr(sample, f"{prefix}_detected_at", None)
            if detected_at is not None:
                detected_count += 1
                detected_delays.append(max(0.0, (detected_at - sample.target_at).total_seconds()))
    return EvaluationMetrics(
        sample_count=len(valid),
        mae=mean(absolute) if absolute else None,
        rmse=math.sqrt(mean(squared)) if squared else None,
        smape=mean(_smape(sample.actual, float(getattr(sample, prediction))) for sample in valid)
        if valid else None,
        bias=mean(errors) if errors else None,
        p95_abs_error=_p95(absolute),
        interval_coverage=(sum(interval_hits) / len(interval_hits)) if interval_hits else None,
        interval_sample_count=len(interval_hits),
        recall=(detected_count / event_count) if event_count else None,
        delay_seconds=mean(detected_delays) if detected_delays else None,
        quality_ratio=(len(valid) / len(samples)) if samples else 0.0,
        event_count=event_count,
        detected_count=detected_count,
    )


def evaluate_paired(
    samples: Iterable[PairedSample],
    *,
    cluster_name: str,
    host: str,
    metric: str,
    horizon_hours: int,
    champion_version: str,
    challenger_version: str,
    now: datetime | None = None,
    ttl_hours: int = 72,
) -> PairedEvaluation:
    """Build immutable evidence from paired samples only.

    A sample with a missing target, input-window identity, actual, prediction,
    or quality evidence is not silently matched with another model.  It stays
    in the denominator as a quality failure and therefore lowers the quality
    ratio used by the promotion gate.
    """

    ordered = sorted(list(samples), key=lambda sample: (sample.target_at, sample.input_window_id))
    reference = (now or _utc_now()).replace(tzinfo=None)
    expiry = reference + timedelta(hours=max(1, ttl_hours))
    complete_pairs = [
        sample for sample in ordered
        if sample.input_window_id
        and sample.target_at is not None
        and _finite(sample.actual)
        and _finite(sample.champion_prediction)
        and _finite(sample.challenger_prediction)
    ]
    payload = {
        "scope": [cluster_name, host, metric, horizon_hours],
        "versions": [champion_version, challenger_version],
        "pairs": [
            {
                "window": sample.input_window_id,
                "target": sample.target_at.isoformat(),
                "input_start": sample.input_window_start.isoformat()
                if sample.input_window_start else None,
                "input_end": sample.input_window_end.isoformat()
                if sample.input_window_end else None,
                "quality": sample.quality_status,
                "actual": sample.actual,
                "champion": sample.champion_prediction,
                "challenger": sample.challenger_prediction,
                "drift": sample.drift,
            }
            for sample in ordered
        ],
    }
    evidence_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    quality_failures = len(ordered) - len([
        sample for sample in complete_pairs
        if sample.quality_status in GOOD_QUALITY_STATUSES
    ])
    if not ordered:
        quality_status = "INSUFFICIENT_EVIDENCE"
    elif quality_failures:
        quality_status = "PARTIAL_QUALITY"
    else:
        quality_status = "OK"
    return PairedEvaluation(
        cluster_name=cluster_name,
        host=host,
        metric=metric,
        horizon_hours=horizon_hours,
        champion_version=champion_version,
        challenger_version=challenger_version,
        input_window_start=min(
            (sample.input_window_start for sample in ordered if sample.input_window_start),
            default=None,
        ),
        input_window_end=max(
            (sample.input_window_end for sample in ordered if sample.input_window_end),
            default=None,
        ),
        target_start=ordered[0].target_at if ordered else None,
        target_end=ordered[-1].target_at if ordered else None,
        champion=_model_metrics(ordered, prediction="champion_prediction"),
        challenger=_model_metrics(ordered, prediction="challenger_prediction"),
        paired_count=len(complete_pairs),
        quality_status=quality_status,
        drift_count=sum(1 for sample in ordered if sample.drift),
        evidence_hash=evidence_hash,
        evaluated_at=reference,
        expires_at=expiry,
    )


def _not_worse(candidate: float | None, champion: float | None, allowed_regression: float) -> bool:
    if candidate is None or champion is None:
        return False
    if champion == 0:
        return candidate <= 0
    return candidate <= champion * (1.0 + allowed_regression)


def promotion_gate(
    evaluation: PairedEvaluation,
    *,
    config: PromotionConfig,
    prior_pass_streak: int = 0,
    now: datetime | None = None,
) -> PromotionGateResult:
    """Evaluate every required gate for one host/metric/horizon scope."""

    reasons: list[str] = []
    candidate = evaluation.challenger
    champion = evaluation.champion
    if not all((evaluation.cluster_name, evaluation.host, evaluation.metric)):
        reasons.append("promotion scope is incomplete")
    if not evaluation.champion_version or not evaluation.challenger_version:
        reasons.append("model version is missing")
    if evaluation.paired_count < config.minimum_samples:
        reasons.append(f"paired samples {evaluation.paired_count}/{config.minimum_samples}")
    if candidate.sample_count < config.minimum_samples:
        reasons.append(f"quality-valid samples {candidate.sample_count}/{config.minimum_samples}")
    if candidate.quality_ratio < config.minimum_quality_ratio:
        reasons.append(f"quality ratio {candidate.quality_ratio:.3f} below {config.minimum_quality_ratio:.3f}")
    if evaluation.quality_status != "OK":
        reasons.append(f"quality status is {evaluation.quality_status}")
    if evaluation.drift_count:
        reasons.append(f"candidate drift evidence present ({evaluation.drift_count})")
    if not _not_worse(candidate.mae, champion.mae, config.maximum_mae_regression):
        reasons.append("candidate MAE regresses or is unavailable")
    if not _not_worse(candidate.rmse, champion.rmse, config.maximum_rmse_regression):
        reasons.append("candidate RMSE regresses or is unavailable")
    if not _not_worse(candidate.smape, champion.smape, config.maximum_smape_regression):
        reasons.append("candidate SMAPE regresses or is unavailable")
    if not _not_worse(candidate.p95_abs_error, champion.p95_abs_error, config.maximum_p95_regression):
        reasons.append("candidate p95 absolute error regresses or is unavailable")
    if candidate.bias is None or abs(candidate.bias) > config.maximum_bias_abs:
        reasons.append("candidate bias exceeds the configured bound")
    if (
        candidate.interval_coverage is None
        or candidate.interval_sample_count < candidate.sample_count
        or candidate.interval_coverage < config.minimum_interval_coverage
    ):
        reasons.append("candidate interval coverage is below the configured minimum")
    if candidate.recall is None or candidate.recall < config.minimum_recall:
        reasons.append("candidate recall is below the configured minimum")
    elif (
        champion.recall is not None
        and candidate.recall < champion.recall * (1.0 - config.maximum_recall_regression)
    ):
        reasons.append("candidate recall regresses against champion")
    if candidate.delay_seconds is None or candidate.delay_seconds > config.maximum_delay_seconds:
        reasons.append("candidate detection delay exceeds the configured maximum")
    elif (
        champion.delay_seconds is not None
        and candidate.delay_seconds > champion.delay_seconds * (1.0 + config.maximum_delay_regression)
    ):
        reasons.append("candidate detection delay regresses against champion")
    reference = (now or _utc_now()).replace(tzinfo=None)
    if evaluation.expires_at <= reference:
        reasons.append("evaluation evidence has expired")
    current_streak = (prior_pass_streak + 1) if not reasons else 0
    if current_streak < config.minimum_evaluation_streak:
        reasons.append(f"evaluation streak {current_streak}/{config.minimum_evaluation_streak}")
    return PromotionGateResult(
        passed=not reasons,
        scope=(evaluation.cluster_name, evaluation.host, evaluation.metric, evaluation.horizon_hours),
        candidate_version=evaluation.challenger_version,
        evidence_hash=evaluation.evidence_hash,
        evaluation_streak=current_streak,
        reasons=tuple(reasons),
        expires_at=evaluation.expires_at,
    )


def persist_evaluation(session, evaluation: PairedEvaluation, *, passed: bool = False):
    """Persist immutable paired evidence; duplicate evidence is idempotent."""

    existing = session.query(ForecastEvaluationEvidence).filter_by(
        evidence_hash=evaluation.evidence_hash,
    ).one_or_none()
    if existing is not None:
        return existing
    row = ForecastEvaluationEvidence(
        id=str(uuid.uuid4()),
        cluster_name=evaluation.cluster_name,
        host=evaluation.host,
        metric=evaluation.metric,
        horizon_hours=evaluation.horizon_hours,
        champion_version=evaluation.champion_version,
        challenger_version=evaluation.challenger_version,
        input_window_start=evaluation.input_window_start,
        input_window_end=evaluation.input_window_end,
        target_start=evaluation.target_start,
        target_end=evaluation.target_end,
        paired_count=evaluation.paired_count,
        quality_status=evaluation.quality_status,
        quality_ratio=evaluation.challenger.quality_ratio,
        metrics_json=json.dumps({
            "champion": asdict(evaluation.champion),
            "challenger": asdict(evaluation.challenger),
            "drift_count": evaluation.drift_count,
        }, sort_keys=True, default=str),
        evidence_hash=evaluation.evidence_hash,
        passed=passed,
        created_at=evaluation.evaluated_at,
        expires_at=evaluation.expires_at,
    )
    session.add(row)
    session.flush()
    return row


def persisted_pass_streak(session, *, evaluation: PairedEvaluation,
                           now: datetime | None = None) -> int:
    """Count consecutive unexpired passed evidence for the exact scope."""

    reference = (now or _utc_now()).replace(tzinfo=None)
    rows = (
        session.query(ForecastEvaluationEvidence)
        .filter_by(
            cluster_name=evaluation.cluster_name,
            host=evaluation.host,
            metric=evaluation.metric,
            horizon_hours=evaluation.horizon_hours,
            champion_version=evaluation.champion_version,
            challenger_version=evaluation.challenger_version,
        )
        .order_by(ForecastEvaluationEvidence.created_at.desc())
        .all()
    )
    streak = 0
    for row in rows:
        if not row.passed or row.expires_at <= reference:
            break
        streak += 1
    return streak


def append_promotion_audit(
    session,
    *,
    event_type: str,
    scope: tuple[str, str, str, int],
    candidate_version: str,
    actor: str,
    reason: str,
    evidence_hash: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
    now: datetime | None = None,
):
    if event_type not in PROMOTION_EVENTS:
        raise ValueError(f"unsupported promotion audit event: {event_type}")
    row = ForecastPromotionAudit(
        id=str(uuid.uuid4()),
        event_type=event_type,
        cluster_name=scope[0],
        host=scope[1],
        metric=scope[2],
        horizon_hours=scope[3],
        candidate_version=candidate_version,
        from_version=from_version,
        to_version=to_version,
        evidence_hash=evidence_hash,
        actor=actor,
        reason=reason,
        created_at=(now or _utc_now()).replace(tzinfo=None),
    )
    session.add(row)
    session.flush()
    return row


def request_promotion(
    session,
    *,
    gate: PromotionGateResult,
    evidence_id: str,
    actor: str,
    now: datetime | None = None,
):
    """Record a promotion request without granting active-model authority."""

    event = "PROMOTION_REQUESTED" if gate.passed else "PROMOTION_BLOCKED"
    reason = "promotion request created; operator approval is still required"
    if not gate.passed:
        reason = "; ".join(gate.reasons)
    return append_promotion_audit(
        session,
        event_type=event,
        scope=gate.scope,
        candidate_version=gate.candidate_version,
        actor=actor,
        evidence_hash=gate.evidence_hash,
        reason=reason,
        now=now,
    )


def approve_promotion(
    session,
    *,
    gate: PromotionGateResult,
    evidence_id: str,
    actor: str,
    expires_at: datetime,
    now: datetime | None = None,
):
    """Create an operator approval bound to exact scope/version/evidence."""

    reference = (now or _utc_now()).replace(tzinfo=None)
    if not gate.passed:
        append_promotion_audit(
            session, event_type="PROMOTION_BLOCKED", scope=gate.scope,
            candidate_version=gate.candidate_version, actor=actor,
            evidence_hash=gate.evidence_hash, reason="; ".join(gate.reasons), now=reference,
        )
        raise ValueError("promotion gate is not passed")
    expiry = expires_at.replace(tzinfo=None)
    if expiry <= reference or expiry > gate.expires_at:
        raise ValueError("approval expiry must be inside the evidence validity window")
    evidence = session.get(ForecastEvaluationEvidence, evidence_id)
    if evidence is None:
        raise ValueError("promotion evidence is missing")
    if evidence.evidence_hash != gate.evidence_hash:
        raise ValueError("approval evidence hash does not match the gate")
    if (
        evidence.cluster_name, evidence.host, evidence.metric, evidence.horizon_hours,
        evidence.challenger_version,
    ) != (*gate.scope, gate.candidate_version):
        raise ValueError("approval scope or candidate version does not match evidence")
    if evidence.expires_at <= reference:
        raise ValueError("promotion evidence has expired")
    row = ForecastPromotionApproval(
        id=str(uuid.uuid4()),
        evidence_id=evidence_id,
        cluster_name=gate.scope[0],
        host=gate.scope[1],
        metric=gate.scope[2],
        horizon_hours=gate.scope[3],
        candidate_version=gate.candidate_version,
        evidence_hash=gate.evidence_hash,
        approved_by=actor,
        approved_at=reference,
        expires_at=expiry,
        status="APPROVED",
    )
    session.add(row)
    append_promotion_audit(
        session, event_type="PROMOTION_APPROVED", scope=gate.scope,
        candidate_version=gate.candidate_version, actor=actor,
        evidence_hash=gate.evidence_hash,
        reason="operator approval bound to scope, version and evidence expiry",
        now=reference,
    )
    return row


def promote_model(session, *, approval_id: str, actor: str, now: datetime | None = None):
    """Promote only the exact approved candidate and retire the old champion."""

    from config.settings import settings

    if not settings.forecast_promotion_enabled:
        raise ValueError("forecast promotion is disabled by configuration")
    reference = (now or _utc_now()).replace(tzinfo=None)
    approval = session.get(ForecastPromotionApproval, approval_id)
    if approval is None or approval.status != "APPROVED":
        raise ValueError("promotion approval is missing or not approved")
    if approval.expires_at <= reference:
        approval.status = "EXPIRED"
        append_promotion_audit(
            session, event_type="PROMOTION_EXPIRED",
            scope=(approval.cluster_name, approval.host, approval.metric, approval.horizon_hours),
            candidate_version=approval.candidate_version, actor=actor,
            evidence_hash=approval.evidence_hash, reason="approval expired before promotion", now=reference,
        )
        raise ValueError("promotion approval has expired")
    evidence = session.get(ForecastEvaluationEvidence, approval.evidence_id)
    if evidence is None or evidence.evidence_hash != approval.evidence_hash:
        raise ValueError("promotion evidence is missing or has changed")
    if evidence.expires_at <= reference:
        raise ValueError("promotion evidence has expired")
    # The application uses sessions with autoflush disabled in several worker
    # paths; make the registry lookup see an approval/candidate created in the
    # same transaction.
    session.flush()
    scope_filter = dict(
        cluster_name=approval.cluster_name, host=approval.host,
        metric=approval.metric, horizon_hours=approval.horizon_hours,
    )
    candidate = session.query(ForecastModelRegistry).filter_by(
        **scope_filter, version=approval.candidate_version,
    ).one_or_none()
    if candidate is None or candidate.status not in {"CANDIDATE", "SHADOW"}:
        raise ValueError("candidate model is not promotable")
    active = session.query(ForecastModelRegistry).filter_by(
        **scope_filter, status="ACTIVE",
    ).one_or_none()
    previous = active.version if active is not None else None
    if active is not None:
        active.status = "RETIRED"
        session.flush()
    candidate.status = "ACTIVE"
    candidate.previous_version = previous
    candidate.activated_at = reference
    approval.status = "CONSUMED"
    append_promotion_audit(
        session, event_type="PROMOTED", scope=(approval.cluster_name, approval.host, approval.metric, approval.horizon_hours),
        candidate_version=candidate.version, from_version=previous, to_version=candidate.version,
        actor=actor, evidence_hash=approval.evidence_hash,
        reason="approved candidate became active", now=reference,
    )
    return candidate


def rollback_model(
    session,
    *,
    scope: tuple[str, str, str, int],
    version: str,
    actor: str,
    reason: str,
    now: datetime | None = None,
):
    """Explicitly roll an active model back to its last-known-good version."""

    reference = (now or _utc_now()).replace(tzinfo=None)
    session.flush()
    active = session.query(ForecastModelRegistry).filter_by(
        cluster_name=scope[0], host=scope[1], metric=scope[2],
        horizon_hours=scope[3], version=version, status="ACTIVE",
    ).one_or_none()
    if active is None:
        append_promotion_audit(
            session, event_type="ROLLBACK_BLOCKED", scope=scope,
            candidate_version=version, actor=actor,
            reason="requested model is not active", now=reference,
        )
        raise ValueError("requested model is not active")
    if not active.previous_version:
        append_promotion_audit(
            session, event_type="ROLLBACK_BLOCKED", scope=scope,
            candidate_version=version, actor=actor,
            reason="no last-known-good model is recorded", now=reference,
        )
        raise ValueError("no last-known-good model is recorded")
    target = session.query(ForecastModelRegistry).filter_by(
        cluster_name=scope[0], host=scope[1], metric=scope[2],
        horizon_hours=scope[3], version=active.previous_version,
    ).one_or_none()
    if target is None:
        append_promotion_audit(
            session, event_type="ROLLBACK_BLOCKED", scope=scope,
            candidate_version=version, actor=actor,
            reason="last-known-good model is missing", now=reference,
        )
        raise ValueError("last-known-good model is missing")
    append_promotion_audit(
        session, event_type="ROLLBACK_REQUESTED", scope=scope,
        candidate_version=version, actor=actor, from_version=version,
        to_version=target.version, reason=reason, now=reference,
    )
    active.status = "RETIRED"
    active.last_health_status = "UNHEALTHY"
    session.flush()
    target.status = "ACTIVE"
    target.activated_at = reference
    append_promotion_audit(
        session, event_type="ROLLED_BACK", scope=scope,
        candidate_version=version, actor=actor, from_version=version,
        to_version=target.version, reason=reason, now=reference,
    )
    return target


def verify_active_health(
    session,
    *,
    scope: tuple[str, str, str, int],
    version: str,
    verification: HealthVerification,
    actor: str,
):
    """Verify post-promotion health; rollback to last-known-good on failure."""

    session.flush()
    active = session.query(ForecastModelRegistry).filter_by(
        cluster_name=scope[0], host=scope[1], metric=scope[2],
        horizon_hours=scope[3], version=version, status="ACTIVE",
    ).one_or_none()
    if active is None:
        raise ValueError("the requested model is not active for this scope")
    if verification.healthy and verification.quality_status in GOOD_QUALITY_STATUSES:
        active.last_health_status = "HEALTHY"
        append_promotion_audit(
            session, event_type="HEALTH_VERIFIED", scope=scope,
            candidate_version=version, actor=actor, reason=verification.reason,
            now=verification.checked_at,
        )
        return active
    previous = active.previous_version
    if not previous:
        append_promotion_audit(
            session, event_type="ROLLBACK_BLOCKED", scope=scope,
            candidate_version=version, actor=actor,
            reason=f"health verification failed and no last-known-good model: {verification.reason}",
            now=verification.checked_at,
        )
        raise ValueError("health verification failed and no rollback target exists")
    target = session.query(ForecastModelRegistry).filter_by(
        cluster_name=scope[0], host=scope[1], metric=scope[2],
        horizon_hours=scope[3], version=previous,
    ).one_or_none()
    if target is None:
        append_promotion_audit(
            session, event_type="ROLLBACK_BLOCKED", scope=scope,
            candidate_version=version, actor=actor,
            reason="last-known-good rollback target is missing", now=verification.checked_at,
        )
        raise ValueError("last-known-good rollback target is missing")
    active.status = "RETIRED"
    active.last_health_status = "UNHEALTHY"
    session.flush()
    target.status = "ACTIVE"
    target.activated_at = verification.checked_at
    append_promotion_audit(
        session, event_type="HEALTH_FAILED", scope=scope,
        candidate_version=version, from_version=version, to_version=previous,
        actor=actor, reason=verification.reason, now=verification.checked_at,
    )
    append_promotion_audit(
        session, event_type="ROLLED_BACK", scope=scope,
        candidate_version=version, from_version=version, to_version=previous,
        actor=actor, reason="automatic rollback after failed health verification", now=verification.checked_at,
    )
    return target
