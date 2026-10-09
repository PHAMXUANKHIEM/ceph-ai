"""Bounded Watcher consumer for labelled node-resource samples."""

from __future__ import annotations

import logging
import math
from statistics import median
from itertools import islice
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import and_, desc, select

from config.settings import settings
from shared import db
from shared.clusters import get_default_cluster_id
from shared.learning_runtime import canary_scope_allows, evaluate, resolve_update_target
from shared.online_learning_controls import get_control
from shared.models import OnlineLearnerAudit, OnlineLearnerCycleAudit, OnlineLearnerLabel
from shared.online_learning import (
    BACKEND_NAME,
    BACKEND_VERSION,
    DEFAULT_FEATURE_SCHEMA,
    MODEL_ALGORITHM,
    MODEL_VERSION,
    LearningCircuitBreaker,
    guarded_update,
    load_or_reset_state,
    run_bounded_updates,
    save_state,
)
from shared.online_learning_gate import (
    OnlineLearningGateDecision,
    OnlineLearningInputTracker,
    OnlineLearningSample,
    effective_max_gap_seconds,
    evaluate_sample,
)
from shared.forecast_flags import candidate_enabled
from shared.online_learning_labels import (
    READY,
    enqueue_verified_outcomes,
    label_policy_paused,
    mark_consumed,
    normalize_metric,
    ready_label_for_sample,
)
from shared.online_model_backend import metadata_for_backend

logger = logging.getLogger(__name__)
_CIRCUIT_BREAKER = LearningCircuitBreaker(
    failure_threshold=settings.online_learning_circuit_breaker_failures,
    cooldown_seconds=settings.online_learning_cooldown_seconds,
)


@dataclass(frozen=True)
class ConsumedSample:
    sample_id: str
    metric: str
    quality: OnlineLearningGateDecision
    runtime_mode: str
    update_applied: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _observed_at(raw: object) -> datetime:
    if isinstance(raw, datetime):
        return _utc(raw)
    if isinstance(raw, str):
        return _utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    return datetime.now(timezone.utc)


def _cluster_key(cluster_id: str | None) -> str:
    return str(cluster_id or "__default__")


# The label drift guard compared a verified label with the learner's own
# prediction, so the labels the learner got most wrong were the ones refused:
# 128 of the canary's 129 labels on 09/10/2026 ("sample drift 34.1 exceeds
# limit 20"), and the learner could never correct itself. It now compares the
# label with the host/metric's recent observations: a jump beyond the larger of
# the configured limit and DRIFT_MAD_MULTIPLIER robust deviations is refused.
DRIFT_WINDOW_SAMPLES = 24
DRIFT_MIN_SAMPLES = 8
DRIFT_MAD_MULTIPLIER = 4.0
_MAD_TO_SIGMA = 1.4826


def _recent_drift_reference(
    session, cluster_key: str, host: str, metric: str, *, before: datetime,
) -> tuple[float | None, float]:
    """(median of recent observations, allowed distance), or (None, limit) on a cold start."""
    limit = float(settings.online_learning_drift_threshold_percent)
    values = [
        float(value) for value in session.scalars(
            select(OnlineLearnerAudit.value)
            .where(
                OnlineLearnerAudit.cluster_key == cluster_key,
                OnlineLearnerAudit.host == host,
                OnlineLearnerAudit.metric == metric,
                OnlineLearnerAudit.observed_at < before,
            )
            .order_by(desc(OnlineLearnerAudit.observed_at))
            .limit(DRIFT_WINDOW_SAMPLES)
        )
        if value is not None and math.isfinite(float(value))
    ]
    if len(values) < DRIFT_MIN_SAMPLES:
        return None, limit
    center = median(values)
    spread = median(abs(value - center) for value in values) * _MAD_TO_SIGMA
    return center, max(limit, DRIFT_MAD_MULTIPLIER * spread)


def _consume_one(
    *,
    cluster_id: str | None,
    host: str,
    metric: str,
    value: float,
    observed_at: datetime,
    sample_id: str,
    label: float | None = None,
) -> ConsumedSample:
    """Process one sample and commit exactly one audit decision.

    Labels are optional at the API boundary but required by the gate unless
    explicitly configured otherwise.
    """
    if not candidate_enabled("river_mean"):
        return ConsumedSample(
            sample_id=sample_id,
            metric=normalize_metric(metric),
            quality=OnlineLearningGateDecision(
                "FEATURE_DISABLED", False,
                "river_mean candidate is disabled by forecast_candidate_flags",
                sample_id, None,
            ),
            runtime_mode="FEATURE_DISABLED",
            update_applied=False,
        )
    observed = _utc(observed_at)
    metric = normalize_metric(metric)
    with db.SessionLocal() as session:
        effective_cluster_id = cluster_id or get_default_cluster_id(session)
        cluster_key = _cluster_key(effective_cluster_id)
        control = get_control(
            session,
            cluster_id=effective_cluster_id,
            host=host,
            metric=metric,
        )
        if control is not None and control.status == "PAUSED":
            session.commit()
            return ConsumedSample(
                sample_id=sample_id,
                metric=metric,
                quality=OnlineLearningGateDecision(
                    "PAUSED",
                    False,
                    f"learner stream is paused by {control.updated_by}: {control.reason}",
                    sample_id,
                    None,
                ),
                runtime_mode="PAUSED",
                update_applied=False,
            )
        # Forecast outcomes are evaluated asynchronously. Reconcile them
        # before reading this sample so a previously audited NO_LABEL row can
        # become learnable on a later Watcher cycle.
        enqueue_verified_outcomes(session, max_rows=100)
        policy_paused = label_policy_paused(session)
        existing = session.scalar(
            select(OnlineLearnerAudit).where(
                OnlineLearnerAudit.cluster_key == cluster_key,
                OnlineLearnerAudit.host == host,
                OnlineLearnerAudit.metric == metric,
                OnlineLearnerAudit.sample_id == sample_id,
            )
        )
        if existing is not None:
            ready_label = ready_label_for_sample(
                session,
                cluster_key=cluster_key,
                host=host,
                metric=metric,
                sample_id=sample_id,
            )
            if ready_label is not None and not existing.update_applied:
                if policy_paused:
                    session.commit()
                    return ConsumedSample(
                        sample_id=sample_id,
                        metric=metric,
                        quality=OnlineLearningGateDecision(
                            "DATA_QUALITY", False,
                            "label poisoning guard is paused for the current rate-limit window",
                            sample_id, None,
                        ),
                        runtime_mode="LABEL_POLICY_PAUSED",
                        update_applied=False,
                    )
                runtime = evaluate(
                    session, effective_cluster_id, host=host, metric=metric,
                )
                target_decision = resolve_update_target(
                    session,
                    runtime,
                    cluster_id=effective_cluster_id,
                    host=host,
                    metric=metric,
                    algorithm=MODEL_ALGORITHM,
                    model_version=MODEL_VERSION,
                )
                if not target_decision.allowed:
                    runtime = replace(
                        runtime,
                        can_update_shadow=False,
                        can_update_active=False,
                        reason=f"{runtime.reason}; {target_decision.reason}",
                    )
                learner, _state = load_or_reset_state(
                    session,
                    cluster_id=effective_cluster_id,
                    host=host,
                    metric=metric,
                    model_version=MODEL_VERSION,
                )
                reference, drift_limit = _recent_drift_reference(
                    session, cluster_key, host, metric, before=existing.observed_at,
                )
                quality = evaluate_sample(
                    OnlineLearningSample(
                        value=ready_label.label_value,
                        label=ready_label.label_value,
                        observed_at=_utc(existing.observed_at),
                        sample_id=sample_id,
                    ),
                    OnlineLearningInputTracker(),
                    now=_utc(existing.observed_at),
                    max_age_seconds=settings.online_learning_sample_max_age_seconds,
                    max_forward_gap_seconds=effective_max_gap_seconds(settings),
                    require_label=True,
                    drift_reference=reference,
                    drift_absolute_threshold=drift_limit,
                )
                quality = OnlineLearningGateDecision(
                    quality.status,
                    quality.allowed,
                    f"{quality.reason}; outcome={ready_label.outcome}; evidence={ready_label.evidence_count}",
                    sample_id,
                    quality.age_seconds,
                )
                update = guarded_update(
                    learner,
                    ready_label.label_value,
                    runtime,
                    target=target_decision.target,
                    quality_decision=quality,
                )
                if update.applied:
                    save_state(
                        session,
                        learner,
                        cluster_id=effective_cluster_id,
                        host=host,
                        metric=metric,
                        learned_at=_utc(existing.observed_at),
                    )
                    existing.label = ready_label.label_value
                    existing.quality_status = quality.status
                    existing.quality_reason = quality.reason
                    existing.runtime_mode = runtime.mode
                    existing.runtime_reason = (
                        f"{runtime.reason}; target={target_decision.target}; "
                        f"target_reason={target_decision.reason}"
                    )
                    existing.update_applied = True
                    mark_consumed(ready_label)
                    session.commit()
                    return ConsumedSample(
                        sample_id=sample_id,
                        metric=metric,
                        quality=quality,
                        runtime_mode=runtime.mode,
                        update_applied=True,
                    )
                # Record every label attempt, applied or not: a sample
                # outside the canary scope passes quality but may not
                # update, and apply_ready_labels() must not pick it again.
                if not update.applied:
                    existing.label = ready_label.label_value
                    existing.quality_status = quality.status
                    existing.quality_reason = quality.reason
                    existing.runtime_mode = runtime.mode
                    existing.runtime_reason = (
                        f"{runtime.reason}; target={target_decision.target}; "
                        f"target_reason={target_decision.reason}"
                    )
                    session.commit()
                    return ConsumedSample(
                        sample_id=sample_id,
                        metric=metric,
                        quality=quality,
                        runtime_mode=runtime.mode,
                        update_applied=False,
                    )
            else:
                session.commit()
            return ConsumedSample(
                sample_id=sample_id,
                metric=metric,
                quality=OnlineLearningGateDecision(
                    existing.quality_status, existing.quality_status == "READY_TO_LEARN",
                    existing.quality_reason, sample_id, None,
                ),
                runtime_mode=existing.runtime_mode,
                update_applied=existing.update_applied,
            )

        latest = session.scalar(
            select(OnlineLearnerAudit)
            .where(
                OnlineLearnerAudit.cluster_key == cluster_key,
                OnlineLearnerAudit.host == host,
                OnlineLearnerAudit.metric == metric,
            )
            .order_by(desc(OnlineLearnerAudit.observed_at))
        )
        tracker = OnlineLearningInputTracker(
            last_observed_at=latest.observed_at if latest else None,
        )
        quality = evaluate_sample(
            OnlineLearningSample(
                value=value, observed_at=observed, label=label, sample_id=sample_id,
            ),
            tracker,
            now=datetime.now(timezone.utc),
            max_age_seconds=settings.online_learning_sample_max_age_seconds,
            max_forward_gap_seconds=effective_max_gap_seconds(settings),
            require_label=settings.online_learning_require_verified_label,
        )
        runtime = evaluate(
            session, effective_cluster_id, host=host, metric=metric,
        )
        if policy_paused:
            runtime = replace(
                runtime,
                can_update_shadow=False,
                can_update_active=False,
                reason="label poisoning guard is paused for the current rate-limit window",
            )
        target_decision = resolve_update_target(
            session,
            runtime,
            cluster_id=effective_cluster_id,
            host=host,
            metric=metric,
            algorithm=MODEL_ALGORITHM,
            model_version=MODEL_VERSION,
        )
        if not target_decision.allowed:
            runtime = replace(
                runtime,
                can_update_shadow=False,
                can_update_active=False,
                reason=f"{runtime.reason}; {target_decision.reason}",
            )
        learner, _state = load_or_reset_state(
            session,
            cluster_id=effective_cluster_id,
            host=host,
            metric=metric,
            model_version=MODEL_VERSION,
        )
        update = guarded_update(
            learner,
            value,
            runtime,
            target=target_decision.target,
            quality_decision=quality,
        )
        if update.applied:
            save_state(
                session,
                learner,
                cluster_id=effective_cluster_id,
                host=host,
                metric=metric,
                learned_at=observed,
            )
        backend = metadata_for_backend(learner)
        session.add(OnlineLearnerAudit(
            cluster_key=cluster_key,
            host=host,
            metric=metric,
            sample_id=sample_id,
            observed_at=observed.replace(tzinfo=None),
            value=float(value),
            label=float(label) if label is not None else None,
            quality_status=quality.status,
            quality_reason=quality.reason,
            runtime_mode=runtime.mode,
            runtime_reason=(
                f"{runtime.reason}; target={target_decision.target}; "
                f"target_reason={target_decision.reason}"
            ),
            update_applied=update.applied,
            model_version=MODEL_VERSION,
            backend_name=backend.backend_name,
            backend_version=backend.backend_version,
            feature_schema=backend.feature_schema,
        ))
        session.commit()
        return ConsumedSample(
            sample_id=sample_id,
            metric=metric,
            quality=quality,
            runtime_mode=runtime.mode,
            update_applied=update.applied,
        )


def consume_samples(samples: Iterable[dict]) -> list[ConsumedSample]:
    """Consume one bounded batch and return its audit decisions.

    The shared runner enforces the configured item and wall-clock budgets;
    the circuit breaker stops repeated DB/model failures from turning the
    Watcher scan into a hot retry loop.
    """

    if not settings.online_learning_enabled:
        return []
    results: list[ConsumedSample] = []
    bounded_samples = list(islice(
        samples, settings.online_learning_max_samples_per_cycle + 1,
    ))

    def update(sample: dict) -> None:
        results.append(_consume_one(**sample))

    result = run_bounded_updates(
        bounded_samples,
        update,
        max_samples=settings.online_learning_max_samples_per_cycle,
        timeout_seconds=settings.online_learning_timeout_seconds,
        circuit_breaker=_CIRCUIT_BREAKER,
    )
    if result.processed or result.failed:
        clusters = {str(item.get("cluster_id") or "__default__") for item in bounded_samples}
        hosts = {str(item.get("host") or "*") for item in bounded_samples}
        metrics = {str(item.get("metric") or "*").strip().lower() for item in bounded_samples}
        runtime_modes = {item.runtime_mode for item in results}
        with db.SessionLocal() as session:
            session.add(OnlineLearnerCycleAudit(
                cluster_key=next(iter(clusters)) if len(clusters) == 1 else "*",
                host=next(iter(hosts)) if len(hosts) == 1 else "*",
                metric=next(iter(metrics)) if len(metrics) == 1 else "mixed",
                processed=result.processed,
                applied=sum(item.update_applied for item in results),
                failed=result.failed,
                skipped=result.skipped,
                elapsed_ms=round(result.elapsed_seconds * 1000, 3),
                cpu_time_ms=round(result.cpu_time_seconds * 1000, 3),
                reason=result.reason,
                runtime_mode=(
                    next(iter(runtime_modes)) if len(runtime_modes) == 1 else "MIXED"
                ) if runtime_modes else "NO_RESULT",
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                feature_schema=DEFAULT_FEATURE_SCHEMA,
            ))
            session.commit()
    return results


def apply_ready_labels(limit: int | None = None) -> list[ConsumedSample]:
    """Feed verified labels back to the samples they verify (autonomy plan WP7).

    The watcher consumes each telemetry sample once, when it is observed,
    and the forecast evaluator writes that sample's verified label seconds
    later. The learning branch in _consume_one only runs when a sample is
    consumed again, which never happened: on 2026-10-05 all 1,592 READY
    labels matched a sample, none was consumed, and no update was ever
    applied. This sweep re-submits the oldest labelled samples that have
    not had a label attempt yet, in observation order; quality is judged at
    the sample's own observed_at, so a late sweep is not "stale".
    """
    if not settings.online_learning_enabled:
        return []
    limit = limit or settings.online_learning_max_samples_per_cycle
    query = (
        select(
            OnlineLearnerAudit.cluster_key, OnlineLearnerAudit.host, OnlineLearnerAudit.metric,
            OnlineLearnerAudit.value, OnlineLearnerAudit.observed_at, OnlineLearnerAudit.sample_id,
        )
        .join(OnlineLearnerLabel, and_(
            OnlineLearnerLabel.cluster_key == OnlineLearnerAudit.cluster_key,
            OnlineLearnerLabel.host == OnlineLearnerAudit.host,
            OnlineLearnerLabel.metric == OnlineLearnerAudit.metric,
            OnlineLearnerLabel.sample_id == OnlineLearnerAudit.sample_id,
        ))
        .where(
            OnlineLearnerLabel.status == READY,
            OnlineLearnerAudit.update_applied.is_(False),
            OnlineLearnerAudit.label.is_(None),
        )
    )
    if settings.online_learning_canary_enabled:
        # Only the canary scope may update. Without this the oldest labels,
        # mostly from other hosts, used each cycle's time budget (5 s, three
        # samples on 2026-10-05) and the canary's own labels were never reached.
        metrics = {
            normalize_metric(item.strip())
            for item in str(settings.online_learning_canary_metrics or "").split(",")
            if item.strip()
        }
        query = query.where(
            OnlineLearnerAudit.cluster_key == str(settings.online_learning_canary_cluster_id or "").strip(),
            OnlineLearnerAudit.host == str(settings.online_learning_canary_host or "").strip(),
            OnlineLearnerAudit.metric.in_(metrics),
        )
    with db.SessionLocal() as session:
        rows = [
            row for row in session.execute(
                query.order_by(OnlineLearnerAudit.observed_at).limit(limit)
            ).all()
            if canary_scope_allows(row.cluster_key, row.host, row.metric)
        ]
    return consume_samples(
        {
            "cluster_id": None if row.cluster_key == "__default__" else row.cluster_key,
            "host": row.host,
            "metric": row.metric,
            "value": row.value,
            "observed_at": row.observed_at,
            "sample_id": row.sample_id,
        }
        for row in rows
    ) if rows else []


def consume_sample(**sample) -> ConsumedSample | None:
    """Compatibility wrapper for one sample."""

    results = consume_samples([sample])
    return results[0] if results else None
