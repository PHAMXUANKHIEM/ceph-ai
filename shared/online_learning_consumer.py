"""Bounded Watcher consumer for labelled node-resource samples."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import desc, select

from config.settings import settings
from shared import db
from shared.clusters import get_default_cluster_id
from shared.learning_runtime import evaluate
from shared.models import OnlineLearnerAudit
from shared.online_learning import (
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
    evaluate_sample,
)
from shared.online_learning_labels import (
    enqueue_verified_outcomes,
    mark_consumed,
    normalize_metric,
    ready_label_for_sample,
)

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
    observed = _utc(observed_at)
    metric = normalize_metric(metric)
    with db.SessionLocal() as session:
        effective_cluster_id = cluster_id or get_default_cluster_id(session)
        cluster_key = _cluster_key(effective_cluster_id)
        # Forecast outcomes are evaluated asynchronously. Reconcile them
        # before reading this sample so a previously audited NO_LABEL row can
        # become learnable on a later Watcher cycle.
        enqueue_verified_outcomes(session, max_rows=100)
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
                runtime = evaluate(session, effective_cluster_id)
                learner, _state = load_or_reset_state(
                    session,
                    cluster_id=effective_cluster_id,
                    host=host,
                    metric=metric,
                    model_version=MODEL_VERSION,
                )
                quality = OnlineLearningGateDecision(
                    "READY_TO_LEARN",
                    True,
                    "verified forecast outcome attached to audited sample",
                    sample_id,
                    max(0.0, (datetime.now(timezone.utc) - _utc(existing.observed_at)).total_seconds()),
                )
                update = guarded_update(
                    learner,
                    existing.value,
                    runtime,
                    target="shadow",
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
                    existing.runtime_reason = runtime.reason
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
                session.commit()
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
            max_forward_gap_seconds=settings.online_learning_sample_max_gap_seconds,
            require_label=settings.online_learning_require_verified_label,
        )
        runtime = evaluate(session, effective_cluster_id)
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
            target="shadow",
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
            runtime_reason=runtime.reason,
            update_applied=update.applied,
            model_version=MODEL_VERSION,
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

    def update(sample: dict) -> None:
        results.append(_consume_one(**sample))

    run_bounded_updates(
        samples,
        update,
        max_samples=settings.online_learning_max_samples_per_cycle,
        timeout_seconds=settings.online_learning_timeout_seconds,
        circuit_breaker=_CIRCUIT_BREAKER,
    )
    return results


def consume_sample(**sample) -> ConsumedSample | None:
    """Compatibility wrapper for one sample."""

    results = consume_samples([sample])
    return results[0] if results else None
